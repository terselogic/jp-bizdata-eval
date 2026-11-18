# jp-bizdata-eval (Public 10-Task Sample)

**Primary-source-grounded, Code-First deterministic AI evaluation benchmark for 2026 Japanese regulatory calculation, abstention, and rule-grounded corporate SQL.**

[![Homepage](https://img.shields.io/badge/Docs-%20terselogic.com%2Fdatasets%2Fjp--bizdata--eval-1d4ed8)](https://terselogic.com/datasets/jp-bizdata-eval/?utm_source=github&utm_medium=readme&utm_campaign=jp_bizdata_eval)
[![Hugging Face](https://img.shields.io/badge/HuggingFace-jp--bizdata--eval--sample-fbbf24)](https://huggingface.co/datasets/terselogic/jp-bizdata-eval-sample)

> **日本語概要**: 2026年（令和8年）時点の最新日本法規・税制・労務（インボイスR8見直し7割控除・1億円上限、204条源泉徴収、年末調整11/30境界、厚労省 育児時短就業給付金、2026年1月施行「取適法」、2026年国民の休日9/22）および実DB（EDINET DuckDB / 法人OpenData SQLite）を用いた **AI評価ベンチマーク＋自動採点ハーネス（公開サンプル10問）** です。

## Why This Benchmark Exists

Most LLM evals leak memorized rules into pre-training or allow models to hedge in prose. `jp-bizdata-eval` enforces:

1. **Zero Formula Prompts + Verified Primary-Source `context_document`**: Real excerpts from e-Gov Law API v2, NTA R8 Tax Revision PDFs/FAQs, JFTC Toriteki Act materials, MHLW guidelines, and NAOJ calendars (PDL 1.0 attributed).
2. **Code-First Deterministic Gold Generation (`validators.py`)**: Exact `Decimal` arithmetic (`ROUND_FLOOR` / `ROUND_HALF_UP`) and Japanese holiday logic with `sha256` `gold_hash` verification.
3. **Strict All-Pass + Anti-Hedging (`allow_hedging: false`)**: Any unrequested prose commentary or multi-option hedging outside raw JSON/SQL fails strict All-Pass, while `field_match_ratio` tracks partial diagnostics.
4. **Abstention Tasks (`{"applicable": false, "reason_code": "..."}`)**: Tests whether models refuse non-applicable regimes (corporations under 3割特例) or unpopulated DB attributes (`flood_hazard_rank` 100% NULL) instead of hallucinating answers.

## Baseline Results on Curated 10-Task Public Sample (`temperature=0`, 2026-11-19)

| Model | Mode | Strict All-Pass | Avg Field Match | Abstention (2) | Cat-1 Invoice R8 (2) | Cat-2 Tax/Labor (3) | Cat-3 Toriteki/Cal (2) | Cat-4 Grounded SQL (3) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `gemini-3.8-flash` | Closed-book (No Context) | **20.0%** (2/10) | 0.4881 | 0.0% | 0/2 | 0/3 | 2/2 | 0/3 |
| `gemini-3.5-flash-lite` | Open-book (With Context) | **30.0%** (3/10) | 0.5024 | 100.0% | 1/2 | 0/3 | 0/2 | 2/3 |
| `gemma-4-31b-it` | Open-book (With Context) | **80.0%** (8/10) | 0.9457 | 100.0% | 1/2 | 3/3 | 1/2 | 3/3 |
| `gemini-3.1-pro-preview` | Open-book (With Context) | **80.0%** (8/10) | 0.8714 | 100.0% | 1/2 | 3/3 | 2/2 | 2/3 |
| `gemini-3.8-flash` | Open-book (With Context) | **100.0%** (10/10) | 1.0000 | 100.0% | 2/2 | 3/3 | 2/2 | 3/3 |

*All 5 runs recorded 0 hedging failures after strict prompt formatting (`allow_hedging: false`).*

## Curated 10 Tasks (`sample/jp_bizdata_eval_teaser_10.jsonl`)

1. `cat1_t01_inv_r8_70pct_rounding` — R8 70% transitional credit, per-supplier 100M JPY cap allocation, per-rate invoice rounding
2. `cat1_t03_inv_r8_abstention_corp_sanwari` — **Abstention**: corporate exclusion from R8 3割特例
3. `cat2_t01_withholding_judicial_scrivener` — Art. 204 two-tier withholding (`10.21%` / `20.42%`), separated consumption tax, statutory registration tax & direct travel exclusion
4. `cat2_t02_r8_yea_nov30_and_single_parent` — R8 `2026-11-30` year-end adjustment cutoff + R9 effective date for raised single-parent deduction
5. `cat2_t03_mhlw_ikuji_jitan_exact` — MHLW Childcare Short-Time Work Benefit (`496,200` base clamp, exact 2-decimal `ROUND_HALF_UP`, `484,121` upper cap)
6. `cat3_t01_toriteki_employee_and_60day` — 2026 Toriteki Act (`>300` employee criterion, 60-day payment term, `14.6%` delay interest)
7. `cat3_t02_bizdays_2026_gw_and_citizen_holiday` — 2026 JP banking business days (`2026-05-06` substitute holiday & `2026-09-22` Citizen's Holiday)
8. `cat4_t02_edinet_yoy_exclude_amendments` — EDINET DuckDB: rule-grounded YoY net sales growth excluding amendment filings (`is_amendment = TRUE`)
9. `cat4_p01_opendata_multi_permit_filter` — JP OpenData SQLite: multi-category permits restricted to `tier1_explicit_corp_num` / `tier2_exact_name_street`
10. `cat4_p03_opendata_abstention_null_zoning` — **Abstention**: governance refusal on 100% unpopulated `flood_hazard_rank` column

## Quick Start

```bash
uv sync --extra dev
uv run pytest -v
uv run jp-eval verify-reference
```

Evaluate your own model (OpenAI-compatible endpoint or saved JSONL predictions):

```bash
# Score saved JSONL predictions ({"task_id": "...", "response": "..."})
uv run jp-eval eval-predictions predictions/my_model.jsonl --model-name my-model

# Run against any OpenAI-compatible API
OPENAI_API_KEY=... uv run jp-eval run-api \
  --model gpt-4.1-mini \
  --out-pred predictions/gpt41mini.jsonl \
  --out-report predictions/report_gpt41mini.json

# Run closed-book contamination check (strips context_document)
OPENAI_API_KEY=... uv run jp-eval run-api \
  --model gpt-4.1-mini \
  --closed-book \
  --out-pred predictions/gpt41mini_closed.jsonl
```

## Full Suite & Custom Domain Eval Packs

Need a 60–100+ task evaluation suite with multi-seed private splits, domain-specific RAG ground truths, or custom Japanese compliance rules?

- Product & Early Access Page: [terselogic.com/datasets/jp-bizdata-eval/](https://terselogic.com/datasets/jp-bizdata-eval/?utm_source=github&utm_medium=readme&utm_campaign=jp_bizdata_eval)
- Contact: `support@terselogic.com`

## License & Attributions

See [LICENSE_ATTRIBUTIONS.txt](./LICENSE_ATTRIBUTIONS.txt) and [sample/dbs/LICENSE_ATTRIBUTIONS.txt](./sample/dbs/LICENSE_ATTRIBUTIONS.txt).
