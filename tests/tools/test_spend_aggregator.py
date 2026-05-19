"""Tests for spend_aggregator.py (Tool 3 P3)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cobalt.models.schemas.rs_schema import ContractTerms, RawSpendRecord
from cobalt.tools.spend_aggregator import aggregate_spend


def _today_iso() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _rec(
    amount_usd: float | None = 1000.0,
    period_start: str | None = None,
    period_end: str | None = None,
    match_confidence: str = "HIGH",
    invoice_ref: str | None = None,
    po_number: str | None = None,
    category_raw: str | None = "IT",
    cost_centre: str | None = "CC-001",
    payment_terms_days: int | None = None,
) -> RawSpendRecord:
    return RawSpendRecord(
        source_id="src",
        arrival_mode="FILE_UPLOAD",
        trust_level="OFFICIAL",
        period_start=period_start,
        period_end=period_end,
        amount_raw=str(amount_usd) if amount_usd else "0",
        currency_raw="USD",
        amount_usd=amount_usd,
        category_raw=category_raw,
        cost_centre=cost_centre,
        po_number=po_number,
        invoice_ref=invoice_ref,
        matched_vendor_id="V-0001",
        match_confidence=match_confidence,
        payment_terms_days=payment_terms_days,
    )


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------

def test_full_completeness():
    today = date.today()
    records = [
        _rec(1000.0, f"{today.year}-01-01", f"{today.year}-03-31"),
        _rec(2000.0, f"{today.year}-04-01", f"{today.year}-06-30"),
        _rec(1500.0, f"{today.year}-07-01", f"{today.year}-09-30"),
        _rec(1200.0, f"{today.year-1}-01-01", f"{today.year-1}-03-31"),
        _rec(800.0, f"{today.year-1}-04-01", f"{today.year-1}-06-30"),
        _rec(900.0, f"{today.year-1}-07-01", f"{today.year-1}-09-30"),
    ]
    result = aggregate_spend("V-0001", records, [])
    assert result.summary.data_completeness == "FULL"


def test_sparse_completeness():
    records = [_rec(1000.0, "2025-01-01", "2025-03-31"), _rec(2000.0, "2025-04-01", "2025-06-30")]
    result = aggregate_spend("V-0001", records, [])
    assert result.summary.data_completeness == "SPARSE"


def test_none_completeness():
    result = aggregate_spend("V-0001", [], [])
    assert result.summary.data_completeness == "NONE"
    # Should not raise


# ---------------------------------------------------------------------------
# TTM / YTD
# ---------------------------------------------------------------------------

def test_ttm_only_last_12_months():
    today = date.today()
    new_rec = _rec(5000.0, period_end=(today - timedelta(days=30)).isoformat())
    old_rec = _rec(3000.0, period_end=(today - timedelta(days=400)).isoformat())
    result = aggregate_spend("V-0001", [new_rec, old_rec], [])
    assert result.summary.total_usd_ttm == pytest.approx(5000.0)


def test_ytd_excludes_prior_year():
    today = date.today()
    current_rec = _rec(4000.0, period_start=f"{today.year}-01-15")
    prior_rec = _rec(3000.0, period_start=f"{today.year - 1}-06-01")
    result = aggregate_spend("V-0001", [current_rec, prior_rec], [])
    assert result.summary.total_usd_ytd == pytest.approx(4000.0)


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------

def test_duplicate_invoice_anomaly():
    records = [
        _rec(1000.0, "2025-01-01", invoice_ref="INV-001"),
        _rec(1000.0, "2025-02-01", invoice_ref="INV-001"),
    ]
    result = aggregate_spend("V-0001", records, [])
    types = [a["type"] for a in result.anomalies]
    assert "DUPLICATE_INVOICE" in types


def test_missing_po_anomaly():
    records = [_rec(15000.0, "2025-01-01", po_number=None)]
    result = aggregate_spend("V-0001", records, [])
    types = [a["type"] for a in result.anomalies]
    assert "MISSING_PO" in types


def test_no_currency_data_flag():
    records = [_rec(None, "2025-01-01")]
    result = aggregate_spend("V-0001", records, [])
    assert "NO_CURRENCY_DATA" in result.data_quality_flags


def test_null_period_records_in_all_time_only():
    rec = _rec(500.0, None, None)
    result = aggregate_spend("V-0001", [rec], [])
    assert result.summary.total_usd_all_time == pytest.approx(500.0)
    assert result.summary.by_period == {}


# ---------------------------------------------------------------------------
# Category aggregation
# ---------------------------------------------------------------------------

def test_null_category_grouped_as_uncategorised():
    records = [_rec(1000.0, "2025-01-01", category_raw=None)]
    result = aggregate_spend("V-0001", records, [])
    assert "UNCATEGORISED" in result.summary.by_category


# ---------------------------------------------------------------------------
# Payment terms average
# ---------------------------------------------------------------------------

def test_payment_terms_avg():
    records = [
        _rec(1000.0, "2025-01-01", payment_terms_days=30),
        _rec(2000.0, "2025-02-01", payment_terms_days=60),
    ]
    result = aggregate_spend("V-0001", records, [])
    assert result.summary.payment_terms_days_avg == 45


def test_payment_terms_avg_none_when_no_records_have_it():
    records = [_rec(1000.0, "2025-01-01", payment_terms_days=None)]
    result = aggregate_spend("V-0001", records, [])
    assert result.summary.payment_terms_days_avg is None


# ---------------------------------------------------------------------------
# Contract deviation
# ---------------------------------------------------------------------------

def test_contract_deviation_over():
    records = [
        _rec(155000.0, "2024-01-01"),
        _rec(0.0, "2024-07-01"),
    ]
    # First record has amount_usd=155000, second has 0
    recs = [
        RawSpendRecord(
            source_id="s", arrival_mode="FILE_UPLOAD", trust_level="OFFICIAL",
            period_start="2024-01-01", period_end="2024-12-31",
            amount_raw="155000", currency_raw="USD", amount_usd=155000.0,
            category_raw="IT", cost_centre=None, po_number=None, invoice_ref=None,
            matched_vendor_id="V-001", match_confidence="HIGH",
        )
    ]
    contracts = [ContractTerms(
        document_id="doc1", document_type="CONTRACT",
        effective_date="2024-01-01", expiry_date="2025-12-31",
        auto_renews=False, notice_period_days=None, total_value=100000.0,
        currency="USD", payment_terms_days=None, governing_law=None,
        termination_clauses=[], key_obligations=[], sla_summary=None,
        extraction_confidence="HIGH",
    )]
    result = aggregate_spend("V-001", recs, contracts)
    types = [a["type"] for a in result.anomalies]
    assert "CONTRACT_DEVIATION" in types
    dev_a = next(a for a in result.anomalies if a["type"] == "CONTRACT_DEVIATION")
    assert dev_a["severity"] == "HIGH"


def test_no_contract_total_value_deviation_skipped():
    recs = [_rec(50000.0, "2024-01-01")]
    contracts = [ContractTerms(
        document_id="doc1", document_type="CONTRACT",
        effective_date=None, expiry_date=None,
        auto_renews=None, notice_period_days=None, total_value=None,
        currency=None, payment_terms_days=None, governing_law=None,
        termination_clauses=[], key_obligations=[], sla_summary=None,
        extraction_confidence="LOW",
    )]
    result = aggregate_spend("V-001", recs, contracts)
    types = [a["type"] for a in result.anomalies]
    assert "CONTRACT_DEVIATION" not in types


# ---------------------------------------------------------------------------
# HIGH_VALUE_UNMATCHED
# ---------------------------------------------------------------------------

def test_high_value_unmatched_flag():
    rec = RawSpendRecord(
        source_id="s", arrival_mode="FILE_UPLOAD", trust_level="USER_SUBMITTED",
        period_start="2025-01-01", period_end=None,
        amount_raw="25000", currency_raw="USD", amount_usd=25000.0,
        category_raw=None, cost_centre=None, po_number=None, invoice_ref=None,
        matched_vendor_id=None, match_confidence="UNMATCHED",
    )
    result = aggregate_spend("V-001", [rec], [])
    assert "HIGH_VALUE_UNMATCHED" in result.data_quality_flags
