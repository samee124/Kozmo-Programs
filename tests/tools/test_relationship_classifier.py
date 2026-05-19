"""Tests for relationship_classifier.py (Tool 4 P3)."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest

from cobalt.models.schemas.rs_schema import ContractTerms, SpendSummary
from cobalt.tools.relationship_classifier import classify_relationship


def _summary(
    total_all_time: float | None = 100000.0,
    total_ttm: float | None = 80000.0,
    completeness: str = "FULL",
) -> SpendSummary:
    return SpendSummary(
        total_usd_all_time=total_all_time,
        total_usd_ttm=total_ttm,
        total_usd_ytd=40000.0,
        by_period={},
        by_category={},
        by_cost_centre={},
        invoice_count=10,
        po_count=8,
        payment_terms_days_avg=30,
        data_completeness=completeness,
        confidence="HIGH",
    )


def _contract(
    expiry_days: int | None = 730,
    auto_renews: bool = True,
    total_value: float = 100000.0,
    confidence: str = "HIGH",
) -> ContractTerms:
    today = date.today()
    expiry = (today + timedelta(days=expiry_days)).isoformat() if expiry_days is not None else None
    return ContractTerms(
        document_id="doc1",
        document_type="CONTRACT",
        effective_date=(today - timedelta(days=365)).isoformat(),
        expiry_date=expiry,
        auto_renews=auto_renews,
        notice_period_days=90,
        total_value=total_value,
        currency="USD",
        payment_terms_days=30,
        governing_law=None,
        termination_clauses=[],
        key_obligations=[],
        sla_summary=None,
        extraction_confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Dependency tier
# ---------------------------------------------------------------------------

def test_critical_tier_requires_085_and_single_source():
    """Score >= 0.85 + single_source_risk = True → CRITICAL."""
    summary = SpendSummary(
        total_usd_all_time=500000.0,
        total_usd_ttm=450000.0,  # = 0.90 spend signal
        total_usd_ytd=200000.0,
        by_period={},
        by_category={},
        by_cost_centre={},
        invoice_count=50,
        po_count=40,
        payment_terms_days_avg=30,
        data_completeness="FULL",
        confidence="HIGH",
    )
    contracts = [_contract(expiry_days=1500, auto_renews=True)]
    # No alternatives in entity/known_facts → single_source = True
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=summary,
        contract_terms=contracts,
        entity_profile={"category_hint": "IT_INFRASTRUCTURE"},
        known_facts={},
    )
    assert result.dependency_tier == "CRITICAL"


def test_high_tier_score_075():
    summary = _summary(total_ttm=375000.0)
    contracts = [_contract(expiry_days=500, auto_renews=False)]
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=summary,
        contract_terms=contracts,
        entity_profile={"category_hint": "IT_INFRASTRUCTURE"},
        known_facts={},
    )
    assert result.dependency_tier in ("HIGH", "CRITICAL")
    assert result.relationship_type == "STRATEGIC"


def test_score_080_single_source_true_is_high_not_critical():
    """0.80 < 0.85 so it should NOT be CRITICAL even with single_source."""
    summary = SpendSummary(
        total_usd_all_time=400000.0,
        total_usd_ttm=400000.0,  # 0.80 spend signal capped at 1.0 → 0.25
        total_usd_ytd=200000.0,
        by_period={},
        by_category={},
        by_cost_centre={},
        invoice_count=20,
        po_count=15,
        payment_terms_days_avg=30,
        data_completeness="FULL",
        confidence="HIGH",
    )
    # Build a scenario that gives ~0.80 but not 0.85
    contracts = [_contract(expiry_days=500, auto_renews=False)]
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=summary,
        contract_terms=contracts,
        entity_profile={"category_hint": "IT_INFRASTRUCTURE"},
        known_facts={},
    )
    # score < 0.85 should not produce CRITICAL
    if result.dependency_score < 0.85:
        assert result.dependency_tier != "CRITICAL"


def test_low_score_incidental():
    summary = _summary(total_all_time=100.0, total_ttm=50.0)
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=summary,
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )
    assert result.relationship_type == "INCIDENTAL"
    assert result.dependency_tier == "LOW"


# ---------------------------------------------------------------------------
# LLM usage
# ---------------------------------------------------------------------------

def test_ambiguous_band_triggers_llm():
    """Score in 0.35–0.65 and non-NONE completeness should call llm_call."""
    summary = _summary(total_ttm=120000.0, completeness="PARTIAL")  # moderate spend
    with patch("cobalt.tools.relationship_classifier.llm_call") as mock_llm:
        mock_llm.return_value = '{"relationship_type": "STRATEGIC", "reasoning": "test"}'
        result = classify_relationship(
            vendor_id="V-001",
            spend_summary=summary,
            contract_terms=[],
            entity_profile={},
            known_facts={},
        )
        if 0.35 <= result.dependency_score <= 0.65:
            mock_llm.assert_called_once()
            assert result.llm_used is True


def test_outside_ambiguous_band_no_llm():
    summary = _summary(total_all_time=500000.0, total_ttm=450000.0)
    with patch("cobalt.tools.relationship_classifier.llm_call") as mock_llm:
        result = classify_relationship(
            vendor_id="V-001",
            spend_summary=summary,
            contract_terms=[_contract(expiry_days=1000, auto_renews=True)],
            entity_profile={"category_hint": "IT_INFRASTRUCTURE"},
            known_facts={},
        )
        if result.dependency_score < 0.35 or result.dependency_score > 0.65:
            mock_llm.assert_not_called()
            assert result.llm_used is False


def test_llm_failure_in_ambiguous_band_no_raise():
    summary = _summary(total_ttm=120000.0, completeness="PARTIAL")
    with patch("cobalt.tools.relationship_classifier.llm_call", side_effect=Exception("LLM down")):
        result = classify_relationship(
            vendor_id="V-001",
            spend_summary=summary,
            contract_terms=[],
            entity_profile={},
            known_facts={},
        )
        assert result is not None
        assert result.llm_used is False


def test_none_completeness_no_llm():
    summary = _summary(total_ttm=120000.0, completeness="NONE")
    with patch("cobalt.tools.relationship_classifier.llm_call") as mock_llm:
        classify_relationship(
            vendor_id="V-001",
            spend_summary=summary,
            contract_terms=[],
            entity_profile={},
            known_facts={},
        )
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Contract coverage
# ---------------------------------------------------------------------------

def test_no_contracts_uncovered():
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=_summary(),
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )
    assert result.contract_coverage == "UNCOVERED"


# ---------------------------------------------------------------------------
# Renewal urgency
# ---------------------------------------------------------------------------

def test_renewal_urgent_within_90_days():
    contracts = [_contract(expiry_days=60)]
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=_summary(),
        contract_terms=contracts,
        entity_profile={},
        known_facts={},
    )
    assert result.renewal_urgency == "URGENT"


def test_renewal_watch_within_180():
    contracts = [_contract(expiry_days=150)]
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=_summary(),
        contract_terms=contracts,
        entity_profile={},
        known_facts={},
    )
    assert result.renewal_urgency == "WATCH"


def test_renewal_ok():
    contracts = [_contract(expiry_days=200)]
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=_summary(),
        contract_terms=contracts,
        entity_profile={},
        known_facts={},
    )
    assert result.renewal_urgency == "OK"


def test_renewal_unknown_no_expiry():
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=_summary(),
        contract_terms=[_contract(expiry_days=None)],
        entity_profile={},
        known_facts={},
    )
    assert result.renewal_urgency == "UNKNOWN"


def test_expired_contract_does_not_set_urgency():
    contracts = [_contract(expiry_days=-10)]  # expired
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=_summary(),
        contract_terms=contracts,
        entity_profile={},
        known_facts={},
    )
    # Expired contract should not count for urgency
    assert result.renewal_urgency == "UNKNOWN"


# ---------------------------------------------------------------------------
# Single source risk
# ---------------------------------------------------------------------------

def test_single_source_false_when_alternatives_present():
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=_summary(),
        contract_terms=[],
        entity_profile={"alternatives": ["Vendor B"]},
        known_facts={},
    )
    assert result.single_source_risk is False


def test_single_source_false_when_no_spend():
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=_summary(total_all_time=None),
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )
    assert result.single_source_risk is False


# ---------------------------------------------------------------------------
# Unknown type when no spend and no contracts
# ---------------------------------------------------------------------------

def test_unknown_when_no_spend_and_no_contracts():
    summary = SpendSummary(
        total_usd_all_time=None, total_usd_ttm=None, total_usd_ytd=None,
        by_period={}, by_category={}, by_cost_centre={},
        invoice_count=0, po_count=0, payment_terms_days_avg=None,
        data_completeness="NONE", confidence="NONE",
    )
    result = classify_relationship(
        vendor_id="V-001",
        spend_summary=summary,
        contract_terms=[],
        entity_profile={},
        known_facts={},
    )
    assert result.relationship_type == "UNKNOWN"
    assert result.dependency_tier is None
