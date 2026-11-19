"""Public release tests for jp-bizdata-eval 10-task sample and strict deterministic scorer."""

from __future__ import annotations

from datetime import date
import json

import pytest

from jp_bizdata_eval.runner import (
    DEFAULT_DB_DIR,
    build_reference_response,
    format_user_prompt,
    load_all_tasks,
)
from jp_bizdata_eval.scorer import extract_sql_query_strict, score_task, summarize_results
from jp_bizdata_eval.validators import calc_ikuji_jitan_benefit, is_jp_business_day_2026


def test_public_teaser_task_counts_and_audit_metadata() -> None:
    tasks = load_all_tasks()
    assert len(tasks) == 10
    assert len({t.task_id for t in tasks}) == 10
    assert all(t.audit.split == "public_teaser_10" for t in tasks)
    assert {t.category for t in tasks} == {
        "cat1_invoice_r8",
        "cat2_withholding_labor",
        "cat3_toriteki_bizdays",
        "cat4_edinet_grounded_sql",
    }
    assert sum(1 for t in tasks if t.audit.abstention_expected) == 2

    for t in tasks:
        assert t.context_document.strip()
        assert t.audit.canary_string.startswith("jp-bizdata-eval:v2:canary:public:")
        assert t.audit.gold_hash.startswith("sha256:")
        assert t.audit.human_audit.status == "verified"
        assert len(t.audit.source_attributions) >= 1
        assert t.audit.grading_policy.mode == "all_pass"
        assert t.audit.grading_policy.allow_hedging is False


def test_all_reference_answers_pass_100_percent() -> None:
    tasks = load_all_tasks()
    results = [score_task(t, build_reference_response(t), DEFAULT_DB_DIR) for t in tasks]
    report = summarize_results("gold-reference-teaser-10", results)
    assert report.passed_tasks == 10
    assert report.total_tasks == 10
    assert report.strict_pass_rate == 1.0
    assert report.avg_field_match_ratio == 1.0
    assert report.abstention_total == 2
    assert report.abstention_accuracy == 1.0
    for r in results:
        assert r.passed, f"{r.task_id} failed: {r.error_reason} {r.detail}"
        if r.category == "cat4_edinet_grounded_sql" and not r.abstention_expected:
            assert r.detail["row_count"] > 0


def test_mhlw_ikuji_jitan_official_spot_checks() -> None:
    ex2 = calc_ikuji_jitan_benefit(raw_base_wage_monthly_yen=300_000, target_month_wage_paid_yen=280_000)
    assert ex2["wage_ratio_pct"] == "93.33"
    assert ex2["benefit_rate_pct"] == "6.43"
    assert ex2["payout_yen"] == 18_004

    ex3 = calc_ikuji_jitan_benefit(raw_base_wage_monthly_yen=510_000, target_month_wage_paid_yen=445_000)
    assert ex3["capped_base_wage_yen"] == 496_200
    assert ex3["payout_yen"] == 39_121
    assert ex3["limit_reason"] == "CAPPED_AT_484121"


def test_anti_hedging_abstention_and_sql_safety() -> None:
    tasks = {t.task_id: t for t in load_all_tasks()}

    t_inv = tasks["cat1_t01_inv_r8_70pct_rounding"]
    gold_str = json.dumps(t_inv.expected_json, ensure_ascii=False)

    res_hedge = score_task(t_inv, f"以下が結果です。\n```json\n{gold_str}\n```", DEFAULT_DB_DIR)
    assert not res_hedge.passed
    assert res_hedge.hedging_detected is True
    assert res_hedge.error_reason == "hedging_or_extra_prose_rejected"

    res_thought = score_task(t_inv, f"<thought>Step 1</thought>{gold_str}", DEFAULT_DB_DIR)
    assert res_thought.passed
    assert res_thought.hedging_detected is False

    t_abs = tasks["cat4_p03_opendata_abstention_null_zoning"]
    res_abs_fail = score_task(
        t_abs,
        "SELECT flood_hazard_rank, AVG(net_sales) FROM edinet_links GROUP BY 1",
        DEFAULT_DB_DIR,
    )
    assert not res_abs_fail.passed

    t_sql = tasks["cat4_t02_edinet_yoy_exclude_amendments"]
    res_false_abs = score_task(t_sql, '{"applicable": false, "reason_code": "UNSURE"}', DEFAULT_DB_DIR)
    assert not res_false_abs.passed
    assert res_false_abs.error_reason == "false_abstention_on_answerable_task"

    for bad_sql in ["DROP TABLE pit_metrics", "SELECT 1; DELETE FROM permits", "PRAGMA table_info(corporates)"]:
        with pytest.raises(ValueError):
            extract_sql_query_strict(bad_sql)


def test_2026_holidays_and_closed_book_prompt_stripping() -> None:
    assert not is_jp_business_day_2026(date(2026, 5, 6))
    assert not is_jp_business_day_2026(date(2026, 9, 22))
    assert is_jp_business_day_2026(date(2026, 9, 24))

    t = load_all_tasks()[0]
    assert "### 根拠資料 (context_document)" in format_user_prompt(t, closed_book=False)
    assert "### 根拠資料 (context_document)" not in format_user_prompt(t, closed_book=True)
