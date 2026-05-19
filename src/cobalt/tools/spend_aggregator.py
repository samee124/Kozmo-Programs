"""Tool 3 (Process 3) — spend_aggregator.

Aggregate and normalise all raw spend records into decision-ready views.
No LLM. Pure calculation and rule-based anomaly detection.

Returns SpendAggregationResult in memory. Never raises.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from cobalt.models.schemas.rs_schema import (
    ContractTerms,
    RawSpendRecord,
    SpendAggregationResult,
    SpendSummary,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
SPIKE_MULTIPLIER     = 3.0
LARGE_INVOICE_USD    = 10_000
SPEND_GAP_QUARTERS   = 3
MIN_PERIODS_FOR_GAP  = 6


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> date:
    return date.today()


def _parse_date(iso_str: str | None) -> date | None:
    if not iso_str:
        return None
    try:
        return date.fromisoformat(iso_str[:10])
    except (ValueError, TypeError):
        return None


def _to_quarter(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{q}"


def _quarter_to_date_start(qstr: str) -> date | None:
    """Convert 'YYYY-QN' to first day of that quarter."""
    try:
        year_str, q_str = qstr.split("-Q")
        year = int(year_str)
        q = int(q_str)
        month = (q - 1) * 3 + 1
        return date(year, month, 1)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def _filter_matched(records: list[RawSpendRecord]) -> list[RawSpendRecord]:
    """Return only HIGH/MEDIUM confidence records with non-null amount_usd."""
    return [
        r for r in records
        if r.match_confidence in ("HIGH", "MEDIUM") and r.amount_usd is not None
    ]


# ---------------------------------------------------------------------------
# Period aggregation
# ---------------------------------------------------------------------------

def _get_record_date(r: RawSpendRecord) -> date | None:
    d = _parse_date(r.period_start)
    if d is None:
        d = _parse_date(r.period_end)
    return d


def _sum_by_period(records: list[RawSpendRecord]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for r in records:
        d = _get_record_date(r)
        if d is None:
            continue
        key = _to_quarter(d)
        totals[key] = totals.get(key, 0.0) + (r.amount_usd or 0.0)
    return dict(sorted(totals.items()))


def _compute_ttm(records: list[RawSpendRecord]) -> float | None:
    today = _today()
    year_ago = today.replace(year=today.year - 1)
    total = 0.0
    any_record = False
    for r in records:
        ref_date = _parse_date(r.period_end) or _parse_date(r.period_start)
        if ref_date and ref_date >= year_ago and r.amount_usd is not None:
            total += r.amount_usd
            any_record = True
    return total if any_record else None


def _compute_ytd(records: list[RawSpendRecord]) -> float | None:
    today = _today()
    year_start = date(today.year, 1, 1)
    total = 0.0
    any_record = False
    for r in records:
        d = _parse_date(r.period_start)
        if d and d >= year_start and r.amount_usd is not None:
            total += r.amount_usd
            any_record = True
    return total if any_record else None


# ---------------------------------------------------------------------------
# Dimension aggregation
# ---------------------------------------------------------------------------

def _sum_by_dimension(records: list[RawSpendRecord], dimension: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for r in records:
        val = getattr(r, dimension, None)
        if not val or (isinstance(val, str) and not val.strip()):
            val = "UNCATEGORISED"
        totals[val] = totals.get(val, 0.0) + (r.amount_usd or 0.0)
    return dict(sorted(totals.items(), key=lambda x: -x[1]))


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------

def _detect_anomalies(
    records: list[RawSpendRecord],
    by_period: dict[str, float],
) -> list[dict]:
    anomalies: list[dict] = []

    # SPEND_SPIKE
    all_quarters = sorted(by_period.keys())
    for i, qkey in enumerate(all_quarters):
        prior = all_quarters[:i]
        if len(prior) < 1:
            continue
        rolling_avg = sum(by_period[q] for q in prior[-4:]) / len(prior[-4:])
        if rolling_avg > 0 and by_period[qkey] > SPIKE_MULTIPLIER * rolling_avg:
            multiplier = by_period[qkey] / rolling_avg
            anomalies.append({
                "type":        "SPEND_SPIKE",
                "description": f"{qkey} spend ${by_period[qkey]:,.0f} is {multiplier:.1f}x rolling average of ${rolling_avg:,.0f}",
                "severity":    "MEDIUM",
                "period":      qkey,
                "value_usd":   by_period[qkey],
            })

    # DUPLICATE_INVOICE
    seen_invoice: dict[str, int] = {}
    for r in records:
        if r.invoice_ref:
            seen_invoice[r.invoice_ref] = seen_invoice.get(r.invoice_ref, 0) + 1
    for inv_ref, count in seen_invoice.items():
        if count >= 2:
            anomalies.append({
                "type":        "DUPLICATE_INVOICE",
                "description": f"Invoice {inv_ref!r} appears {count} times",
                "severity":    "HIGH",
                "invoice_ref": inv_ref,
                "count":       count,
            })

    # MISSING_PO
    for r in records:
        if (
            r.amount_usd is not None
            and r.amount_usd >= LARGE_INVOICE_USD
            and not r.po_number
        ):
            anomalies.append({
                "type":        "MISSING_PO",
                "description": f"Invoice ${r.amount_usd:,.0f} has no PO number",
                "severity":    "LOW",
                "invoice_ref": r.invoice_ref,
                "value_usd":   r.amount_usd,
            })

    # SPEND_GAP
    if len(all_quarters) >= MIN_PERIODS_FOR_GAP:
        # Build full quarter range
        first_d = _quarter_to_date_start(all_quarters[0])
        last_d = _quarter_to_date_start(all_quarters[-1])
        if first_d and last_d:
            all_q_range = _generate_quarters(first_d, last_d)
            gap_start: str | None = None
            gap_len = 0
            for qkey in all_q_range:
                if qkey not in by_period or by_period[qkey] == 0:
                    if gap_start is None:
                        gap_start = qkey
                    gap_len += 1
                else:
                    if gap_len >= SPEND_GAP_QUARTERS:
                        anomalies.append({
                            "type":        "SPEND_GAP",
                            "description": f"No spend for {gap_len} quarters from {gap_start} to {qkey}",
                            "severity":    "LOW",
                            "gap_start":   gap_start,
                            "gap_end":     qkey,
                        })
                    gap_start = None
                    gap_len = 0
            # check trailing gap
            if gap_len >= SPEND_GAP_QUARTERS and gap_start:
                anomalies.append({
                    "type":        "SPEND_GAP",
                    "description": f"No spend for {gap_len} quarters starting {gap_start}",
                    "severity":    "LOW",
                    "gap_start":   gap_start,
                    "gap_end":     all_quarters[-1],
                })

    return anomalies


def _generate_quarters(start: date, end: date) -> list[str]:
    """Generate all YYYY-QN keys between start and end dates (inclusive)."""
    quarters: list[str] = []
    d = start.replace(day=1)
    month_start = ((d.month - 1) // 3) * 3 + 1
    d = d.replace(month=month_start)
    while d <= end:
        quarters.append(_to_quarter(d))
        # Advance one quarter
        if d.month >= 10:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 3)
    return quarters


def _contract_deviation_check(
    total_all_time: float | None,
    contract_terms: list[ContractTerms],
) -> list[dict]:
    anomalies: list[dict] = []
    if total_all_time is None:
        return anomalies

    contract_values = [ct.total_value for ct in contract_terms if ct.total_value is not None]
    if not contract_values:
        return anomalies

    contract_sum = sum(contract_values)
    if contract_sum <= 0:
        return anomalies

    deviation = (total_all_time - contract_sum) / contract_sum
    if abs(deviation) > 0.20:
        if deviation > 0:
            pct = int(deviation * 100)
            anomalies.append({
                "type":        "CONTRACT_DEVIATION",
                "description": f"Actual spend {pct}% above contract total",
                "severity":    "HIGH",
                "actual_usd":  total_all_time,
                "contract_usd": contract_sum,
            })
        else:
            pct = int(abs(deviation) * 100)
            anomalies.append({
                "type":        "CONTRACT_DEVIATION",
                "description": f"Actual spend {pct}% below contract total",
                "severity":    "MEDIUM",
                "actual_usd":  total_all_time,
                "contract_usd": contract_sum,
            })

    return anomalies


# ---------------------------------------------------------------------------
# Data completeness & quality
# ---------------------------------------------------------------------------

def _assess_completeness(
    matched_records: list[RawSpendRecord],
    all_records: list[RawSpendRecord],
) -> str:
    if not all_records:
        return "NONE"
    if len(matched_records) < 3:
        return "SPARSE"

    # Check for null amount_usd
    null_amount = any(r.amount_usd is None for r in matched_records)
    if null_amount:
        return "PARTIAL"

    # Check date presence
    date_missing = sum(
        1 for r in matched_records
        if r.period_start is None and r.period_end is None
    )
    if date_missing / len(matched_records) > 0.10:
        return "PARTIAL"

    # Check distinct periods
    periods = {_to_quarter(d) for r in matched_records if (d := _get_record_date(r)) is not None}
    if len(periods) < 2:
        return "PARTIAL"

    return "FULL"


def _compute_summary_confidence(
    data_completeness: str,
    records: list[RawSpendRecord],
) -> str:
    if data_completeness == "NONE":
        return "NONE"
    if data_completeness == "SPARSE":
        return "LOW"

    has_official = any(r.trust_level in ("OFFICIAL", "SYSTEM_EXPORT") for r in records)

    if data_completeness == "FULL" and has_official:
        return "HIGH"
    return "MEDIUM"


def _compute_quality_flags(
    all_records: list[RawSpendRecord],
    matched_records: list[RawSpendRecord],
    anomalies: list[dict],
) -> list[str]:
    flags: list[str] = []

    if all_records and all(r.amount_usd is None for r in all_records):
        flags.append("NO_CURRENCY_DATA")

    low_or_unmatched = [
        r for r in all_records
        if r.match_confidence in ("LOW", "UNMATCHED")
    ]
    if low_or_unmatched:
        flags.append("UNMATCHED_RECORDS_PRESENT")

    # HIGH_VALUE_UNMATCHED — unmatched records with large amount
    for r in low_or_unmatched:
        if r.match_confidence == "UNMATCHED":
            try:
                val = float(r.amount_raw.replace(",", "").replace("£", "").replace("€", "").replace("$", "").strip())
                if val > LARGE_INVOICE_USD:
                    flags.append("HIGH_VALUE_UNMATCHED")
                    break
            except (ValueError, AttributeError):
                pass

    # DATES_MISSING
    no_dates = sum(
        1 for r in all_records
        if r.period_start is None and r.period_end is None
    )
    if all_records and no_dates / len(all_records) > 0.50:
        flags.append("DATES_MISSING")

    # DUPLICATE_INVOICES_FOUND
    if any(a["type"] == "DUPLICATE_INVOICE" for a in anomalies):
        flags.append("DUPLICATE_INVOICES_FOUND")

    # NO_PO_COVERAGE
    po_count = sum(1 for r in all_records if r.po_number)
    if po_count == 0 and all_records:
        flags.append("NO_PO_COVERAGE")

    return flags


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def aggregate_spend(
    vendor_id: str,
    raw_records: list[RawSpendRecord],
    contract_terms: list[ContractTerms],
) -> SpendAggregationResult:
    """Aggregate raw spend records into a structured summary with anomalies.

    Never raises.
    """
    # Filter to HIGH/MEDIUM confidence records with amount_usd
    matched = _filter_matched(raw_records)

    # Aggregations
    by_period = _sum_by_period(matched)
    by_category = _sum_by_dimension(matched, "category_raw")
    by_cost_centre = _sum_by_dimension(matched, "cost_centre")

    total_all_time = sum(r.amount_usd for r in matched) if matched else None
    ttm = _compute_ttm(matched)
    ytd = _compute_ytd(matched)

    invoice_count = len(raw_records)
    po_count = sum(1 for r in raw_records if r.po_number)

    pt_values = [
        r.payment_terms_days
        for r in matched
        if r.payment_terms_days is not None
    ]
    payment_terms_days_avg = round(sum(pt_values) / len(pt_values)) if pt_values else None

    # Completeness & confidence
    completeness = _assess_completeness(matched, raw_records)
    confidence = _compute_summary_confidence(completeness, raw_records)

    # Anomalies
    anomalies = _detect_anomalies(matched, by_period)
    anomalies.extend(_contract_deviation_check(total_all_time, contract_terms))

    # Quality flags
    quality_flags = _compute_quality_flags(raw_records, matched, anomalies)

    summary = SpendSummary(
        total_usd_all_time=total_all_time,
        total_usd_ttm=ttm,
        total_usd_ytd=ytd,
        by_period=by_period,
        by_category=by_category,
        by_cost_centre=by_cost_centre,
        invoice_count=invoice_count,
        po_count=po_count,
        payment_terms_days_avg=payment_terms_days_avg,
        data_completeness=completeness,
        confidence=confidence,
    )

    return SpendAggregationResult(
        vendor_id=vendor_id,
        summary=summary,
        anomalies=anomalies,
        data_quality_flags=quality_flags,
        aggregated_at=_now_iso(),
    )
