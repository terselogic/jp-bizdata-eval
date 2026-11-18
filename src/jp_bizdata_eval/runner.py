"""CLI runner for jp-bizdata-eval public 10-task benchmark sample."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from jp_bizdata_eval.schema import EvalTask
from jp_bizdata_eval.scorer import score_task, summarize_results

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_DIR = REPO_ROOT / "sample" / "dbs"
DEFAULT_DATASET_PATH = REPO_ROOT / "sample" / "jp_bizdata_eval_teaser_10.jsonl"


def load_all_tasks(dataset_path: Path = DEFAULT_DATASET_PATH) -> list[EvalTask]:
    """Load the 10 curated public evaluation tasks from JSONL."""
    tasks: list[EvalTask] = []
    for line in dataset_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            tasks.append(EvalTask.model_validate_json(line))
    return tasks


def build_reference_response(task: EvalTask) -> str:
    """Build gold reference string response for verifying scorer soundness."""
    if task.output_type == "json" or task.audit.abstention_expected:
        return json.dumps(task.expected_json, ensure_ascii=False)
    if task.output_type == "sql_or_abstention":
        assert task.reference_sql is not None
        return task.reference_sql
    raise ValueError(f"Unsupported output_type: {task.output_type}")


def format_user_prompt(task: EvalTask, *, closed_book: bool = False) -> str:
    """Assemble prompt with primary-source context unless running closed-book baseline."""
    if closed_book:
        return task.prompt
    return f"### 根拠資料 (context_document)\n{task.context_document}\n\n### 設問\n{task.prompt}"


def cmd_verify_reference(db_dir: Path, dataset_path: Path) -> int:
    tasks = load_all_tasks(dataset_path)
    results = [score_task(t, build_reference_response(t), db_dir) for t in tasks]
    report = summarize_results("gold-reference-teaser-10", results)
    print(report.model_dump_json(indent=2))
    return 0 if report.passed_tasks == report.total_tasks else 1


def cmd_eval_predictions(
    pred_path: Path, model_name: str, db_dir: Path, dataset_path: Path, closed_book: bool
) -> int:
    tasks = load_all_tasks(dataset_path)
    preds: dict[str, dict] = {}
    for line in pred_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        preds[obj["task_id"]] = obj

    results = []
    for task in tasks:
        rec = preds.get(task.task_id, {})
        results.append(
            score_task(
                task,
                rec.get("response", ""),
                db_dir,
                latency_ms=rec.get("latency_ms"),
                prompt_tokens=rec.get("prompt_tokens"),
                completion_tokens=rec.get("completion_tokens"),
            )
        )

    report = summarize_results(model_name, results, closed_book=closed_book)
    print(report.model_dump_json(indent=2))
    return 0


def cmd_run_openai_compat(
    model: str,
    base_url: str,
    api_key_env: str,
    out_pred: Path,
    db_dir: Path,
    dataset_path: Path,
    closed_book: bool,
    out_report: Path | None = None,
) -> int:
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        print(f"Error: environment variable {api_key_env} is not set.", file=sys.stderr)
        return 2

    import httpx

    tasks = load_all_tasks(dataset_path)
    out_pred.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict] = {}
    if out_pred.exists():
        for line in out_pred.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if rec.get("task_id") and not str(rec.get("response", "")).startswith("__API_ERROR__"):
                    existing[rec["task_id"]] = rec
            except json.JSONDecodeError:
                continue

    results = []
    with httpx.Client(timeout=90.0) as client, out_pred.open("w", encoding="utf-8") as f:
        for task in tasks:
            if task.task_id in existing:
                rec = existing[task.task_id]
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                results.append(
                    score_task(
                        task,
                        rec.get("response", ""),
                        db_dir,
                        latency_ms=rec.get("latency_ms"),
                        prompt_tokens=rec.get("prompt_tokens"),
                        completion_tokens=rec.get("completion_tokens"),
                    )
                )
                continue

            content = ""
            p_tok = None
            c_tok = None
            latency_ms = 0.0
            last_err: str | None = None
            timeout_count = 0
            for attempt in range(4):
                t0 = time.perf_counter()
                try:
                    resp = client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "temperature": 0,
                            "messages": [
                                {"role": "system", "content": task.system_prompt},
                                {"role": "user", "content": format_user_prompt(task, closed_book=closed_book)},
                            ],
                        },
                    )
                    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                        time.sleep(min(30, 3 * (2**attempt)))
                        continue
                    resp.raise_for_status()
                    body = resp.json()
                    content = body["choices"][0]["message"]["content"] or ""
                    usage = body.get("usage", {})
                    p_tok = usage.get("prompt_tokens")
                    c_tok = usage.get("completion_tokens")
                    last_err = None
                    break
                except httpx.TimeoutException as exc:
                    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                    last_err = f"{type(exc).__name__}: {exc}"
                    timeout_count += 1
                    if timeout_count < 2:
                        time.sleep(2)
                        continue
                    break
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                    last_err = f"{type(exc).__name__}: {exc}"
                    if attempt < 3:
                        time.sleep(min(30, 3 * (2**attempt)))
                        continue
            if last_err is not None and not content:
                content = f"__API_ERROR__: {last_err}"

            record = {
                "task_id": task.task_id,
                "response": content,
                "latency_ms": latency_ms,
                "prompt_tokens": p_tok,
                "completion_tokens": c_tok,
                "closed_book": closed_book,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            results.append(
                score_task(
                    task,
                    content,
                    db_dir,
                    latency_ms=latency_ms,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                )
            )

    report = summarize_results(model, results, closed_book=closed_book)
    report_json = report.model_dump_json(indent=2)
    if out_report is not None:
        out_report.parent.mkdir(parents=True, exist_ok=True)
        out_report.write_text(report_json + "\n", encoding="utf-8")
    print(report_json)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="jp-eval")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ver = sub.add_parser("verify-reference", help="Verify all 10 public teaser tasks pass 100% with gold references")
    p_ver.add_argument("--db-dir", type=Path, default=DEFAULT_DB_DIR)
    p_ver.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)

    p_eval = sub.add_parser("eval-predictions", help="Score a JSONL file of model predictions")
    p_eval.add_argument("pred_path", type=Path)
    p_eval.add_argument("--model-name", default="unknown-model")
    p_eval.add_argument("--db-dir", type=Path, default=DEFAULT_DB_DIR)
    p_eval.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    p_eval.add_argument("--closed-book", action="store_true")

    p_run = sub.add_parser("run-api", help="Run benchmark against an OpenAI-compatible chat completions endpoint")
    p_run.add_argument("--model", required=True)
    p_run.add_argument("--base-url", default="https://api.openai.com/v1")
    p_run.add_argument("--api-key-env", default="OPENAI_API_KEY")
    p_run.add_argument("--out-pred", type=Path, required=True)
    p_run.add_argument("--out-report", type=Path, default=None)
    p_run.add_argument("--db-dir", type=Path, default=DEFAULT_DB_DIR)
    p_run.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    p_run.add_argument("--closed-book", action="store_true")

    args = parser.parse_args()
    if args.cmd == "verify-reference":
        sys.exit(cmd_verify_reference(args.db_dir, args.dataset))
    elif args.cmd == "eval-predictions":
        sys.exit(cmd_eval_predictions(args.pred_path, args.model_name, args.db_dir, args.dataset, args.closed_book))
    elif args.cmd == "run-api":
        sys.exit(
            cmd_run_openai_compat(
                args.model,
                args.base_url,
                args.api_key_env,
                args.out_pred,
                args.db_dir,
                args.dataset,
                args.closed_book,
                args.out_report,
            )
        )


if __name__ == "__main__":
    main()
