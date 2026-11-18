"""Deterministic v2 scorer with All-pass, anti-hedging, abstention, and continuous diagnostics."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

import duckdb

from jp_bizdata_eval.schema import (
    BenchmarkReport,
    CategorySummary,
    EvalItemResult,
    EvalTask,
)

_FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|PRAGMA|COPY|CALL|MERGE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

_HEDGING_TEXT_PATTERN = re.compile(
    r"(あるいは|または[、,]|場合によっては|解釈によっては|どちらか|alternatively|depending on)",
    re.IGNORECASE,
)


def _strip_single_fence_strict(raw_text: str, lang_hint: str = "") -> tuple[str, bool]:
    """Strip reasoning <thought> tags and at most one markdown fence; flag outside prose/hedging."""
    cleaned = re.sub(r"<thought>[\s\S]*?</thought>", "", raw_text, flags=re.IGNORECASE).strip()
    stripped = cleaned
    if not stripped:
        return "", False

    fence_matches = list(re.finditer(r"```(?:json|sql)?\s*([\s\S]*?)\s*```", stripped, re.IGNORECASE))
    if len(fence_matches) > 1:
        return fence_matches[0].group(1).strip(), True
    if len(fence_matches) == 1:
        m = fence_matches[0]
        outside = (stripped[: m.start()] + stripped[m.end() :]).strip()
        hedging = bool(outside) or bool(_HEDGING_TEXT_PATTERN.search(outside))
        return m.group(1).strip(), hedging

    hedging = bool(_HEDGING_TEXT_PATTERN.search(stripped))
    return stripped, hedging


def extract_json_object_strict(raw_text: str) -> tuple[dict[str, Any], bool]:
    """Extract a single JSON object and detect outside commentary or hedging."""
    candidate, hedging = _strip_single_fence_strict(raw_text, "json")
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        brace_match = re.search(r"\{[\s\S]*\}", candidate)
        if not brace_match:
            raise
        outside = (candidate[: brace_match.start()] + candidate[brace_match.end() :]).strip()
        if outside:
            hedging = True
        parsed = json.loads(brace_match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Extracted JSON must be an object/dict.")
    return parsed, hedging


def extract_sql_query_strict(raw_text: str) -> tuple[str, bool]:
    """Extract a single read-only SQL query and reject extra prose or unsafe DDL/DML."""
    candidate, hedging = _strip_single_fence_strict(raw_text, "sql")
    sql = candidate.rstrip(";").strip()
    if not sql:
        raise ValueError("Empty SQL query.")
    if ";" in sql:
        raise ValueError("Multiple SQL statements are not allowed.")
    if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError("SQL query must start with SELECT or WITH.")
    if _FORBIDDEN_SQL_PATTERN.search(sql):
        raise ValueError("Forbidden write or DDL SQL keyword detected.")
    if re.search(r"\b(または|あるいは|参考|注記|補足)\b", sql):
        hedging = True
    return sql, hedging


def _values_equal(expected: Any, actual: Any, float_tol: float = 1e-4) -> bool:
    if expected is None and actual is None:
        return True
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(float(expected), float(actual), rel_tol=0.0, abs_tol=float_tol)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip() == actual.strip()
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        return all(_values_equal(e, a, float_tol) for e, a in zip(expected, actual))
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected.keys()) != set(actual.keys()):
            return False
        return all(_values_equal(expected[k], actual[k], float_tol) for k in expected)
    return expected == actual


def _normalize_cell(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, Decimal):
        return round(float(val), 4)
    if isinstance(val, (int, float)):
        return round(float(val), 4) if isinstance(val, float) else val
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    if isinstance(val, str):
        return val.strip()
    return str(val)


def _execute_readonly_sql(sql: str, db_target: str, db_dir: Path) -> list[tuple[Any, ...]]:
    clean_sql, _ = extract_sql_query_strict(sql)
    if db_target == "edinet_duckdb":
        db_path = db_dir / "edinet_pit_sample.duckdb"
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute(clean_sql).fetchall()
        finally:
            con.close()
    elif db_target == "jp_opendata_sqlite":
        db_path = db_dir / "jp_opendata_sample.sqlite"
        uri = f"file:{db_path.resolve()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            rows = con.execute(clean_sql).fetchall()
        finally:
            con.close()
    else:
        raise ValueError(f"Unsupported db_target: {db_target}")
    return [tuple(_normalize_cell(cell) for cell in row) for row in rows]


def _rows_equal(
    expected_rows: list[tuple[Any, ...]],
    actual_rows: list[tuple[Any, ...]],
    order_sensitive: bool,
    float_tol: float,
) -> tuple[bool, float]:
    if len(expected_rows) == 0 and len(actual_rows) == 0:
        return True, 1.0
    if not expected_rows or not actual_rows or len(expected_rows[0]) != len(actual_rows[0]):
        return False, 0.0

    def row_match(r1: tuple[Any, ...], r2: tuple[Any, ...]) -> bool:
        return all(_values_equal(c1, c2, float_tol) for c1, c2 in zip(r1, r2))

    if order_sensitive:
        matched = sum(1 for r1, r2 in zip(expected_rows, actual_rows) if row_match(r1, r2))
        ratio = matched / max(len(expected_rows), len(actual_rows))
        return len(expected_rows) == len(actual_rows) and matched == len(expected_rows), round(ratio, 4)

    key_fn = lambda r: tuple("" if v is None else str(v) for v in r)
    exp_sorted = sorted(expected_rows, key=key_fn)
    act_sorted = sorted(actual_rows, key=key_fn)
    matched = sum(1 for r1, r2 in zip(exp_sorted, act_sorted) if row_match(r1, r2))
    ratio = matched / max(len(expected_rows), len(actual_rows))
    return len(expected_rows) == len(actual_rows) and matched == len(expected_rows), round(ratio, 4)


def score_task(
    task: EvalTask,
    raw_response: str,
    db_dir: Path,
    *,
    latency_ms: float | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> EvalItemResult:
    """Evaluate a raw model response deterministically with All-pass and anti-hedging."""
    split = task.audit.split
    abst_exp = task.audit.abstention_expected
    allow_hedging = task.audit.grading_policy.allow_hedging

    # Path 1: JSON task OR abstention task across any category (including Cat-4 SQL abstention)
    if task.output_type == "json" or abst_exp:
        if task.expected_json is None:
            raise ValueError(f"Task {task.task_id} missing expected_json")
        try:
            actual, hedging = extract_json_object_strict(raw_response)
        except Exception as exc:
            return EvalItemResult(
                task_id=task.task_id,
                category=task.category,
                split=split,
                passed=False,
                score=0.0,
                field_match_ratio=0.0,
                abstention_expected=abst_exp,
                error_reason=f"json_parse_error: {exc}",
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        abstained = actual.get("applicable") is False
        matched_fields = sum(
            1
            for k, exp_val in task.expected_json.items()
            if k in actual and _values_equal(exp_val, actual[k], task.float_tolerance)
        )
        field_ratio = round(matched_fields / len(task.expected_json), 4) if task.expected_json else 0.0

        missing_keys = [k for k in task.expected_json if k not in actual]
        unexpected_keys = [k for k in actual if k not in task.expected_json]
        mismatched = {
            k: {"expected": exp_val, "actual": actual[k]}
            for k, exp_val in task.expected_json.items()
            if k in actual and not _values_equal(exp_val, actual[k], task.float_tolerance)
        }

        if hedging and not allow_hedging:
            return EvalItemResult(
                task_id=task.task_id,
                category=task.category,
                split=split,
                passed=False,
                score=0.0,
                field_match_ratio=field_ratio,
                hedging_detected=True,
                abstained=abstained,
                abstention_expected=abst_exp,
                error_reason="hedging_or_extra_prose_rejected",
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        if not missing_keys and not unexpected_keys and not mismatched:
            return EvalItemResult(
                task_id=task.task_id,
                category=task.category,
                split=split,
                passed=True,
                score=1.0,
                field_match_ratio=1.0,
                hedging_detected=hedging,
                abstained=abstained,
                abstention_expected=abst_exp,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        return EvalItemResult(
            task_id=task.task_id,
            category=task.category,
            split=split,
            passed=False,
            score=0.0,
            field_match_ratio=field_ratio,
            hedging_detected=hedging,
            abstained=abstained,
            abstention_expected=abst_exp,
            error_reason="value_or_schema_mismatch",
            detail={
                "missing_keys": missing_keys,
                "unexpected_keys": unexpected_keys,
                "mismatched": mismatched,
            },
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    # Path 2: Executable read-only SQL task (Cat-4 non-abstention)
    if task.output_type == "sql_or_abstention":
        if not task.db_target or not task.reference_sql:
            raise ValueError(f"SQL task {task.task_id} missing db_target or reference_sql")

        # Detect false abstention JSON on answerable SQL task
        try:
            maybe_json, _ = extract_json_object_strict(raw_response)
            if maybe_json.get("applicable") is False:
                return EvalItemResult(
                    task_id=task.task_id,
                    category=task.category,
                    split=split,
                    passed=False,
                    score=0.0,
                    field_match_ratio=0.0,
                    abstained=True,
                    abstention_expected=False,
                    error_reason="false_abstention_on_answerable_task",
                    latency_ms=latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
        except Exception:
            pass

        try:
            expected_rows = _execute_readonly_sql(task.reference_sql, task.db_target, db_dir)
        except Exception as exc:
            raise RuntimeError(f"Reference SQL failed for {task.task_id}: {exc}") from exc

        try:
            clean_sql, hedging = extract_sql_query_strict(raw_response)
            if hedging and not allow_hedging:
                return EvalItemResult(
                    task_id=task.task_id,
                    category=task.category,
                    split=split,
                    passed=False,
                    score=0.0,
                    field_match_ratio=0.0,
                    hedging_detected=True,
                    abstention_expected=False,
                    error_reason="hedging_or_extra_prose_rejected",
                    latency_ms=latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            actual_rows = _execute_readonly_sql(clean_sql, task.db_target, db_dir)
        except Exception as exc:
            return EvalItemResult(
                task_id=task.task_id,
                category=task.category,
                split=split,
                passed=False,
                score=0.0,
                field_match_ratio=0.0,
                abstention_expected=False,
                error_reason=f"sql_execution_error: {exc}",
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

        exact_ok, row_ratio = _rows_equal(expected_rows, actual_rows, task.order_sensitive, task.float_tolerance)
        if exact_ok:
            return EvalItemResult(
                task_id=task.task_id,
                category=task.category,
                split=split,
                passed=True,
                score=1.0,
                field_match_ratio=1.0,
                hedging_detected=hedging,
                abstention_expected=False,
                detail={"row_count": len(expected_rows)},
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        return EvalItemResult(
            task_id=task.task_id,
            category=task.category,
            split=split,
            passed=False,
            score=0.0,
            field_match_ratio=row_ratio,
            hedging_detected=hedging,
            abstention_expected=False,
            error_reason="sql_result_mismatch",
            detail={
                "expected_rows_count": len(expected_rows),
                "actual_rows_count": len(actual_rows),
                "expected_preview": expected_rows[:3],
                "actual_preview": actual_rows[:3],
            },
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    raise ValueError(f"Unknown output_type: {task.output_type}")


def summarize_results(
    model_name: str,
    results: list[EvalItemResult],
    *,
    closed_book: bool = False,
) -> BenchmarkReport:
    """Aggregate strict All-pass, continuous field match, abstention accuracy, and token usage."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    avg_fmr = round(sum(r.field_match_ratio for r in results) / total, 4) if total else 0.0

    abst_items = [r for r in results if r.abstention_expected]
    abst_total = len(abst_items)
    abst_acc = round(sum(1 for r in abst_items if r.passed) / abst_total, 4) if abst_total else 0.0
    hedging_failures = sum(1 for r in results if r.hedging_detected and not r.passed)

    by_category: list[CategorySummary] = []
    for cat in (
        "cat1_invoice_r8",
        "cat2_withholding_labor",
        "cat3_toriteki_bizdays",
        "cat4_edinet_grounded_sql",
    ):
        sub = [r for r in results if r.category == cat]
        if not sub:
            continue
        cat_pass = sum(1 for r in sub if r.passed)
        by_category.append(
            CategorySummary(
                category=cat,
                total=len(sub),
                passed=cat_pass,
                strict_pass_rate=round(cat_pass / len(sub), 4),
                avg_field_match_ratio=round(sum(r.field_match_ratio for r in sub) / len(sub), 4),
            )
        )

    return BenchmarkReport(
        model_name=model_name,
        closed_book=closed_book,
        total_tasks=total,
        passed_tasks=passed,
        strict_pass_rate=round(passed / total, 4) if total else 0.0,
        avg_field_match_ratio=avg_fmr,
        abstention_total=abst_total,
        abstention_accuracy=abst_acc,
        hedging_failures=hedging_failures,
        total_prompt_tokens=sum(r.prompt_tokens or 0 for r in results),
        total_completion_tokens=sum(r.completion_tokens or 0 for r in results),
        by_category=by_category,
        results=results,
    )
