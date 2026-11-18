"""Code-First deterministic gold generators and domain validators."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Literal

RoundingMethod = Literal["floor", "half_up", "ceil"]

# Official 2026 (R8) Japanese National Holidays derived from Holiday Act (323AC) + NAOJ 2026 Rekiyoukou
HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 1): "元日",
    date(2026, 1, 12): "成人の日",
    date(2026, 2, 11): "建国記念の日",
    date(2026, 2, 23): "天皇誕生日",
    date(2026, 3, 20): "春分の日",
    date(2026, 4, 29): "昭和の日",
    date(2026, 5, 3): "憲法記念日",
    date(2026, 5, 4): "みどりの日",
    date(2026, 5, 5): "こどもの日",
    date(2026, 5, 6): "振替休日(憲法記念日)",
    date(2026, 7, 20): "海の日",
    date(2026, 8, 11): "山の日",
    date(2026, 9, 21): "敬老の日",
    date(2026, 9, 22): "国民の休日",
    date(2026, 9, 23): "秋分の日",
    date(2026, 10, 12): "スポーツの日",
    date(2026, 11, 3): "文化の日",
    date(2026, 11, 23): "勤労感謝の日",
}

KANJI_DIGIT_MAP = str.maketrans("〇一二三四五六七八九", "0123456789")


def _round_yen(amount: Decimal, method: RoundingMethod) -> int:
    rounding_map = {
        "floor": ROUND_FLOOR,
        "half_up": ROUND_HALF_UP,
        "ceil": ROUND_CEILING,
    }
    return int(amount.quantize(Decimal("1"), rounding=rounding_map[method]))


# ---------------------------------------------------------------------------
# Category 1: R8 Invoice Reform (7-5-3 Transitional Credit, 100M Cap, 3割特例)
# ---------------------------------------------------------------------------


def _invoice_transitional_rate(tx_date: date) -> Decimal:
    """Return R8 revised transitional credit rate for non-qualified supplier purchases."""
    if tx_date <= date(2026, 9, 30):
        return Decimal("0.80")
    if tx_date <= date(2028, 9, 30):
        return Decimal("0.70")
    if tx_date <= date(2030, 9, 30):
        return Decimal("0.50")
    if tx_date <= date(2031, 9, 30):
        return Decimal("0.30")
    return Decimal("0.00")


def calc_invoice_transitional_credit(
    *,
    tx_date_str: str,
    tax_period_start_str: str,
    prior_eligible_tax_incl_purchases_from_supplier: int,
    line_items_excl: list[dict[str, Any]],
    rounding_method: RoundingMethod = "floor",
    supplier_is_qualified: bool = False,
) -> dict[str, Any]:
    """Compute deductible transitional input tax under R8 invoice revision.

    Implements:
    - NTA Invoice Q&A Q57: one rounding per tax rate per invoice (not per line item)
    - NTA R8 Rev01: 80% (~2026-09-30), 70% (2026-10-01..2028-09-30), 50%, 30%, 0%
    - NTA R8 Rev01: per-supplier 100,000,000 JPY (tax-included) cap effective
      for taxable periods starting on or after 2026-10-01.
    """
    tx_date = date.fromisoformat(tx_date_str)
    period_start = date.fromisoformat(tax_period_start_str)

    if supplier_is_qualified:
        return {
            "applicable": False,
            "reason_code": "QUALIFIED_SUPPLIER_NOT_TRANSITIONAL",
        }

    rate = _invoice_transitional_rate(tx_date)
    if rate == Decimal("0.00"):
        return {
            "applicable": False,
            "reason_code": "TRANSITIONAL_PERIOD_EXPIRED",
        }

    base_by_rate: dict[str, int] = {"10": 0, "8": 0}
    for item in line_items_excl:
        tax_rate_key = str(item["tax_rate_pct"])
        if tax_rate_key not in base_by_rate:
            raise ValueError(f"Unsupported tax rate: {tax_rate_key}")
        qty = int(item.get("qty", 1))
        unit_excl = int(item["unit_price_excl"])
        base_by_rate[tax_rate_key] += unit_excl * qty

    gross_tax_10 = _round_yen(Decimal(base_by_rate["10"]) * Decimal("0.10"), rounding_method)
    gross_tax_8 = _round_yen(Decimal(base_by_rate["8"]) * Decimal("0.08"), rounding_method)
    gross_tax_total = gross_tax_10 + gross_tax_8
    invoice_tax_incl = base_by_rate["10"] + base_by_rate["8"] + gross_tax_total

    cap_applied = period_start >= date(2026, 10, 1)
    cap_limit = 100_000_000
    if cap_applied:
        remaining_cap = max(0, cap_limit - prior_eligible_tax_incl_purchases_from_supplier)
        capped_tax_incl = min(invoice_tax_incl, remaining_cap)
    else:
        capped_tax_incl = invoice_tax_incl

    if capped_tax_incl == 0:
        return {
            "applicable": False,
            "reason_code": "ONE_OKU_SUPPLIER_CAP_EXHAUSTED",
        }

    if capped_tax_incl < invoice_tax_incl:
        # Allocate capped tax-inclusive purchase proportionally by each rate bucket's tax-incl share
        incl_10 = base_by_rate["10"] + gross_tax_10
        ratio = Decimal(capped_tax_incl) / Decimal(invoice_tax_incl)
        eligible_tax_incl_10 = min(incl_10, _round_yen(Decimal(incl_10) * ratio, "floor"))
        eligible_tax_incl_8 = capped_tax_incl - eligible_tax_incl_10
        eligible_tax_10 = _round_yen(Decimal(eligible_tax_incl_10) * Decimal("10") / Decimal("110"), rounding_method)
        eligible_tax_8 = _round_yen(Decimal(eligible_tax_incl_8) * Decimal("8") / Decimal("108"), rounding_method)
        eligible_tax_base = eligible_tax_10 + eligible_tax_8
    else:
        eligible_tax_base = gross_tax_total

    deductible_tax = _round_yen(Decimal(eligible_tax_base) * rate, "floor")
    return {
        "applicable": True,
        "transitional_rate_pct": int(rate * 100),
        "cap_100m_active": cap_applied,
        "invoice_tax_incl_yen": invoice_tax_incl,
        "eligible_tax_incl_yen": capped_tax_incl,
        "invoice_gross_consumption_tax_yen": gross_tax_total,
        "deductible_input_tax_yen": deductible_tax,
    }


def check_sanwari_tokurei_and_compare(
    *,
    entity_type: Literal["individual", "corporation"],
    tax_year_r: int,
    base_period_taxable_sales_yen: int,
    specific_period_taxable_sales_yen: int,
    adjusted_fixed_assets_trigger: bool,
    shortened_tax_period_elected: bool,
    bullion_over_2m_trigger: bool,
    output_tax_yen: int,
    simplified_business_type: int | None = None,
) -> dict[str, Any]:
    """Check R8 '3割特例' (70% deemed deduction = 30% tax) vs simplified taxation.

    From NTA R8 Rev01 / QA 115-2 / QA 117-3:
    - Applies only to individuals (個人事業者) in taxable periods containing R9/R10
      (tax_year_r in {9, 10}). Disqualified for corporations.
    - Disqualified if base period > 10M, specific period > 10M, adjusted fixed assets,
      shortened tax period election, or bullion >= 2M.
    """
    if entity_type != "individual":
        return {"applicable": False, "reason_code": "CORPORATION_EXCLUDED_SANWARI"}
    if tax_year_r not in (9, 10):
        return {"applicable": False, "reason_code": "OUTSIDE_SANWARI_TAX_YEARS"}
    if base_period_taxable_sales_yen > 10_000_000:
        return {"applicable": False, "reason_code": "BASE_PERIOD_EXCEEDS_10M"}
    if specific_period_taxable_sales_yen > 10_000_000:
        return {"applicable": False, "reason_code": "SPECIFIC_PERIOD_EXCEEDS_10M"}
    if adjusted_fixed_assets_trigger:
        return {"applicable": False, "reason_code": "ADJUSTED_FIXED_ASSETS_DISQUALIFIED"}
    if shortened_tax_period_elected:
        return {"applicable": False, "reason_code": "SHORTENED_PERIOD_DISQUALIFIED"}
    if bullion_over_2m_trigger:
        return {"applicable": False, "reason_code": "BULLION_TRANSACTION_DISQUALIFIED"}

    sanwari_tax_payable = _round_yen(Decimal(output_tax_yen) * Decimal("0.30"), "floor")
    deemed_rates = {1: Decimal("0.90"), 2: Decimal("0.80"), 3: Decimal("0.70"), 4: Decimal("0.60"), 5: Decimal("0.50"), 6: Decimal("0.40")}
    simplified_tax_payable: int | None = None
    recommended_method = "sanwari_tokurei"
    if simplified_business_type is not None:
        deemed_rate = deemed_rates[simplified_business_type]
        simplified_tax_payable = _round_yen(Decimal(output_tax_yen) * (Decimal("1") - deemed_rate), "floor")
        if simplified_tax_payable < sanwari_tax_payable:
            recommended_method = "simplified_tax"
        elif simplified_tax_payable == sanwari_tax_payable:
            recommended_method = "equal"

    return {
        "applicable": True,
        "sanwari_deemed_input_rate_pct": 70,
        "sanwari_tax_payable_yen": sanwari_tax_payable,
        "simplified_tax_payable_yen": simplified_tax_payable,
        "recommended_method": recommended_method,
    }


# ---------------------------------------------------------------------------
# Category 2: Withholding (Art. 204), R8 Basic/Salary FAQ, MHLW Ikuji-Jitan, Labor Art. 37
# ---------------------------------------------------------------------------


def calc_art204_withholding(
    *,
    payee_type: Literal["individual", "domestic_corporation"],
    gross_fee_incl_tax_yen: int | None,
    fee_excl_tax_yen: int,
    consumption_tax_yen: int,
    tax_clearly_separated_on_invoice: bool,
    direct_travel_hotel_paid_by_client_yen: int = 0,
    statutory_stamp_tax_paid_directly_yen: int = 0,
    horse_racing_prize: bool = False,
) -> dict[str, Any]:
    """Calculate Income Tax Art. 204 withholding (NTA Tax Answers 2792 & 2798)."""
    if payee_type == "domestic_corporation" and not horse_racing_prize:
        return {"applicable": False, "reason_code": "DOMESTIC_CORPORATION_NOT_SUBJECT_ART204"}

    # Travel/hotel paid directly to carrier/hotel and statutory registration/stamp taxes paid
    # directly by payee/agency on behalf of client are excluded from withholding base.
    # Consumption tax is excluded only when clearly separated on the invoice.
    if tax_clearly_separated_on_invoice:
        withholding_base = fee_excl_tax_yen
    else:
        withholding_base = (
            gross_fee_incl_tax_yen
            if gross_fee_incl_tax_yen is not None
            else fee_excl_tax_yen + consumption_tax_yen
        )

    if withholding_base <= 1_000_000:
        tax_exact = Decimal(withholding_base) * Decimal("0.1021")
    else:
        tax_exact = Decimal(withholding_base - 1_000_000) * Decimal("0.2042") + Decimal("102100")

    withholding_tax = _round_yen(tax_exact, "floor")
    total_invoice_billed_to_client = (
        fee_excl_tax_yen
        + consumption_tax_yen
        + statutory_stamp_tax_paid_directly_yen
    )
    net_remittance = total_invoice_billed_to_client - withholding_tax
    return {
        "applicable": True,
        "withholding_base_yen": withholding_base,
        "withholding_tax_yen": withholding_tax,
        "net_remittance_yen": net_remittance,
        "excluded_direct_travel_yen": direct_travel_hotel_paid_by_client_yen,
        "excluded_statutory_tax_yen": statutory_stamp_tax_paid_directly_yen,
    }


def calc_r8_year_end_boundary(
    *,
    last_salary_or_quasi_filing_date_str: str,
    full_year_nonresident: bool,
    salary_revenue_yen: int,
    single_parent_claim: bool,
    tax_year_r: int = 8,
) -> dict[str, Any]:
    """Check R8 Income Tax revision boundary rules (NTA R8 Kiso QA).

    Verified facts from nta_r8_kiso_qa.txt:
    - Q1-1 / Q1-3 / Q6-1: R8 raised deductions apply from 2026-12-01.
      Last salary or quasi-final return on or before 2026-11-30 cannot apply raised deductions in YEA/initial filing.
    - Q2-3 / Q3-1: Salary deduction min raised 65万 -> 74万 (home worker 65万 -> 69万).
    - Q3-2: ひとり親控除 raise 35万 -> 38万 applies from R9 onward (NOT R8 year-end adjustment).
    - Q6-4: Full-year non-resident gets 62万 (<= 2,350万), NOT special resident addition.
    """
    cutoff = date(2026, 12, 1)
    event_date = date.fromisoformat(last_salary_or_quasi_filing_date_str)

    if full_year_nonresident:
        return {
            "applicable": True,
            "r8_raised_deduction_in_initial_proc": True,
            "basic_deduction_yen": 620_000,
            "resident_special_addition_eligible": False,
            "single_parent_deduction_yen": 380_000 if (single_parent_claim and tax_year_r >= 9) else (350_000 if single_parent_claim else 0),
        }

    r8_applied_now = event_date >= cutoff
    salary_deduction_floor = 740_000 if r8_applied_now else 650_000
    # For salary income <= 1,625,000 / simple low-income band verification: minimum floor applies
    salary_deduction_applied = min(salary_revenue_yen, salary_deduction_floor)
    single_parent_yen = (
        380_000 if (single_parent_claim and tax_year_r >= 9) else (350_000 if single_parent_claim else 0)
    )
    return {
        "applicable": True,
        "r8_raised_deduction_in_initial_proc": r8_applied_now,
        "salary_deduction_floor_yen": salary_deduction_floor,
        "salary_deduction_yen": salary_deduction_applied,
        "single_parent_deduction_yen": single_parent_yen,
        "requires_final_return_or_amendment": not r8_applied_now,
    }


def calc_ikuji_jitan_benefit(
    *,
    raw_base_wage_monthly_yen: int,
    target_month_wage_paid_yen: int,
    effective_period_before_2027_07_31: bool = True,
) -> dict[str, Any]:
    """Calculate MHLW Childcare Short-Time Work Benefit (育児時短就業給付金).

    Exact specification from mhlw_ikuji_jitan.txt (valid through 2027-07-31):
    - Base monthly wage clamped to [96,090, 496,200].
    - Wage ratio >= 100%: 0 payout.
    - Wage ratio <= 90%: 10% of target_month_wage_paid_yen.
    - 90% < ratio < 100%:
        X = round_half_up_2dp(target_month_wage_paid * 100 / capped_base)
        Y = round_half_up_2dp(9000 / X - 90)
        payout = floor(target_month_wage_paid * Y / 100)
    - Upper limit (支給限度額): 484,121 (if wage >= 484,121 -> 0; if wage + payout > 484,121 -> 484,121 - wage)
    - Lower threshold (最低限度額): 2,562 (if payout <= 2,562 -> 0)
    """
    if not effective_period_before_2027_07_31:
        return {"applicable": False, "reason_code": "POST_2027_08_ANNUAL_REVISION_UNKNOWN"}

    capped_base = max(96_090, min(496_200, raw_base_wage_monthly_yen))
    if target_month_wage_paid_yen >= capped_base:
        return {
            "applicable": True,
            "capped_base_wage_yen": capped_base,
            "wage_ratio_pct": "100.00" if target_month_wage_paid_yen == capped_base else f"{(Decimal(target_month_wage_paid_yen) * 100 / Decimal(capped_base)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}",
            "benefit_rate_pct": "0.00",
            "payout_yen": 0,
            "limit_reason": "WAGE_AT_OR_ABOVE_BASE",
        }

    x_exact = Decimal(target_month_wage_paid_yen) * Decimal("100") / Decimal(capped_base)
    x_rounded = x_exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if target_month_wage_paid_yen * 10 <= capped_base * 9:
        y_rounded = Decimal("10.00")
    else:
        y_exact = (Decimal("9000") / x_rounded) - Decimal("90")
        y_rounded = max(Decimal("0.00"), y_exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    raw_payout = _round_yen(Decimal(target_month_wage_paid_yen) * y_rounded / Decimal("100"), "floor")

    limit_reason = "NONE"
    max_limit = 484_121
    min_limit = 2_562

    if target_month_wage_paid_yen >= max_limit:
        final_payout = 0
        limit_reason = "MAX_CAP_EXCEEDED_BY_WAGE"
    elif target_month_wage_paid_yen + raw_payout > max_limit:
        final_payout = max_limit - target_month_wage_paid_yen
        limit_reason = "CAPPED_AT_484121"
    else:
        final_payout = raw_payout

    if final_payout <= min_limit:
        final_payout = 0
        if limit_reason == "NONE":
            limit_reason = "BELOW_MIN_THRESHOLD_2562"

    return {
        "applicable": True,
        "capped_base_wage_yen": capped_base,
        "wage_ratio_pct": f"{x_rounded:.2f}",
        "benefit_rate_pct": f"{y_rounded:.2f}",
        "payout_yen": final_payout,
        "limit_reason": limit_reason,
    }


def calc_labor_art37_overtime(
    *,
    base_monthly_salary_yen: int,
    family_allowance_yen: int,
    commuting_allowance_yen: int,
    housing_allowance_flat_yen: int,
    monthly_prescribed_hours: Decimal,
    weekday_overtime_hours: Decimal,
    statutory_holiday_hours: Decimal,
    night_hours_within_weekday_overtime_over_60: Decimal,
    night_hours_within_statutory_holiday: Decimal,
    alternative_leave_hours_granted: Decimal = Decimal("0"),
) -> dict[str, Any]:
    """Calculate Labor Standards Act Art. 37 overtime pay.

    Verified rules:
    - Excludes family_allowance and commuting_allowance from hourly wage base;
      flat housing allowance (not tied to rent amount) IS included.
    - Statutory holiday hours (35% premium) are EXCLUDED from the >60h overtime count.
    - Overtime <= 60h uses 25% premium (total 1.25x); overtime > 60h uses 50% premium (1.50x)
      minus hours covered by alternative leave (代替休暇) which fall back to 25% (0.25 extra only).
    - Night work (22:00-05:00) adds +25% independently across overtime/holiday work.
    """
    included_base_wage = base_monthly_salary_yen + housing_allowance_flat_yen
    # Exact hourly wage (floor or rational preserved until final yen flooring per category/total;
    # keep exact Decimal hourly_rate, round final overtime yen half_up or floor per standard MHLW practice;
    # here hourly_rate rounded half_up to nearest yen cleanly when specified).
    hourly_wage = (Decimal(included_base_wage) / monthly_prescribed_hours).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )

    overtime_le_60 = min(weekday_overtime_hours, Decimal("60"))
    overtime_gt_60 = max(Decimal("0"), weekday_overtime_hours - Decimal("60"))
    overtime_gt_60_leave = min(overtime_gt_60, alternative_leave_hours_granted)
    overtime_gt_60_cash = overtime_gt_60 - overtime_gt_60_leave

    pay_le_60 = hourly_wage * overtime_le_60 * Decimal("1.25")
    pay_gt_60_cash = hourly_wage * overtime_gt_60_cash * Decimal("1.50")
    pay_gt_60_leave_base = hourly_wage * overtime_gt_60_leave * Decimal("1.25")
    pay_holiday = hourly_wage * statutory_holiday_hours * Decimal("1.35")
    pay_night = hourly_wage * (
        night_hours_within_weekday_overtime_over_60 + night_hours_within_statutory_holiday
    ) * Decimal("0.25")

    total_extra_pay = _round_yen(
        pay_le_60 + pay_gt_60_cash + pay_gt_60_leave_base + pay_holiday + pay_night,
        "half_up",
    )
    return {
        "applicable": True,
        "hourly_base_wage_yen": int(hourly_wage),
        "weekday_overtime_gt60_hours_subject_to_50pct": float(overtime_gt_60_cash),
        "total_premium_wages_yen": total_extra_pay,
    }


# ---------------------------------------------------------------------------
# Category 3: Toriteki Act (2026-01-01) & Japanese Business Day Calendar
# ---------------------------------------------------------------------------


def is_jp_business_day_2026(d: date) -> bool:
    """Return True if date in 2026 is not Saturday, Sunday, or a statutory 2026 holiday."""
    if d.year != 2026:
        raise ValueError(f"Expected 2026 date, got {d}")
    if d.weekday() >= 5:
        return False
    return d not in HOLIDAYS_2026


def count_jp_business_days_2026(start_inclusive: date, end_inclusive: date) -> list[str]:
    """Return ISO list of 2026 Japanese business days in [start_inclusive, end_inclusive]."""
    days: list[str] = []
    cur = start_inclusive
    while cur <= end_inclusive:
        if is_jp_business_day_2026(cur):
            days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def calc_toriteki_compliance(
    *,
    contract_date_str: str,
    transaction_type: Literal[
        "manufacturing",
        "repair",
        "info_program",
        "info_design",
        "service_other",
        "specific_transport",
        "construction_work",
    ],
    client_capital_yen: int,
    client_employees: int,
    supplier_capital_yen: int,
    supplier_employees: int,
    receipt_date_str: str,
    scheduled_payment_date_str: str,
    actual_payment_date_str: str,
    unpaid_amount_yen: int,
    uses_promissory_note: bool = False,
) -> dict[str, Any]:
    """Evaluate Toriteki Act (effective 2026-01-01) applicability, 60-day term, and 14.6% interest."""
    contract_date = date.fromisoformat(contract_date_str)
    receipt_date = date.fromisoformat(receipt_date_str)
    actual_pay_date = date.fromisoformat(actual_payment_date_str)

    if transaction_type == "construction_work":
        return {"applicable": False, "reason_code": "CONSTRUCTION_WORK_EXCLUDED_ART2_4"}

    toriteki_effective = contract_date >= date(2026, 1, 1)

    group_300 = transaction_type in ("manufacturing", "repair", "info_program", "specific_transport")
    capital_covered = False
    if group_300:
        if (client_capital_yen > 300_000_000 and supplier_capital_yen <= 300_000_000) or (
            10_000_000 < client_capital_yen <= 300_000_000 and supplier_capital_yen <= 10_000_000
        ):
            capital_covered = True
    else:
        if (client_capital_yen > 50_000_000 and supplier_capital_yen <= 50_000_000) or (
            10_000_000 < client_capital_yen <= 50_000_000 and supplier_capital_yen <= 10_000_000
        ):
            capital_covered = True

    employee_covered = False
    if toriteki_effective:
        emp_threshold = 300 if group_300 else 100
        if client_employees > emp_threshold and supplier_employees <= emp_threshold:
            employee_covered = True

    # Under pre-2026 transition (附則第2条), employee-only & specific transport did not apply
    if not toriteki_effective and transaction_type == "specific_transport":
        return {"applicable": False, "reason_code": "PRE_2026_TRANSITION_NOT_APPLICABLE"}

    if not capital_covered and not employee_covered:
        return {"applicable": False, "reason_code": "BELOW_CAPITAL_AND_EMPLOYEE_THRESHOLDS"}

    # Statutory maximum payment deadline: day 60 counting receipt_date as day 1 => receipt_date + 59 days
    statutory_deadline = receipt_date + timedelta(days=59)
    # Art. 3(2): if agreed date exceeds 60 days, statutory deadline is receipt_date + 59 days
    scheduled_date = date.fromisoformat(scheduled_payment_date_str)
    term_exceeded_60_days = scheduled_date > statutory_deadline
    # Art. 6: delay interest runs from the day AFTER the 60th day (i.e. statutory_deadline + 1 day)
    # up to actual_pay_date (inclusive/per day count from 60-day elapsed day).
    # Let's match exact statutory phrasing: "受領した日から起算して六十日を経過した日から支払をする日までの期間"
    interest_start = receipt_date + timedelta(days=60)
    delay_days = max(0, (actual_pay_date - interest_start).days + 1) if actual_pay_date >= interest_start else 0

    delay_interest = _round_yen(
        Decimal(unpaid_amount_yen) * Decimal("0.146") * Decimal(delay_days) / Decimal("365"),
        "floor",
    )

    return {
        "applicable": True,
        "covered_by_employee_rule_only": (not capital_covered) and employee_covered,
        "violates_promissory_note_prohibition": uses_promissory_note,
        "term_exceeded_60_days": term_exceeded_60_days,
        "statutory_deadline": statutory_deadline.isoformat(),
        "delay_days": delay_days,
        "delay_interest_yen": delay_interest,
    }
