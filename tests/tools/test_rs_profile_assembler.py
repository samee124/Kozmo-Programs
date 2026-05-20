"""Tests for rs_profile_assembler.py (Tool 5 P3)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cobalt.models.schemas.rs_schema import (
    ContractTerms,
    DocumentIntelligenceResult,
    RelationshipClassification,
    RelationshipSpendProfile,
    RawSpendRecord,
    SpendAggregationResult,
    SpendSummary,
    StructuredDataBundle,
)
from cobalt.tools.rs_profile_assembler import (
    _classify_profile_status,
    _compute_assembled_gap_severity,
    _compute_pcs_contribution,
    _reconcile_conflicts,
    assemble_rs_profile,
)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _summary(
    total_all_time: float | None = 200_000.0,
    total_ttm: float | None = 150_000.0,
    completeness: str = "FULL",
    confidence: str = "HIGH",
    invoice_count: int = 10,
) -> SpendSummary:
    return SpendSummary(
        total_usd_all_time=total_all_time,
        total_usd_ttm=total_ttm,
        total_usd_ytd=50_000.0,
        by_period={},
        by_category={},
        by_cost_centre={},
        invoice_count=invoice_count,
        po_count=8,
        payment_terms_days_avg=30,
        data_completeness=completeness,
        confidence=confidence,
    )


def _contract(
    total_value: float | None = 200_000.0,
    effective_date: str | None = "2023-01-01",
    currency: str | None = "USD",
) -> ContractTerms:
    today = date.today()
    return ContractTerms(
        document_id="doc1",
        document_type="CONTRACT",
        effective_date=effective_date,
        expiry_date=(today + timedelta(days=365)).isoformat(),
        auto_renews=True,
        notice_period_days=90,
        total_value=total_value,
        currency=currency,
        payment_terms_days=30,
        governing_law=None,
        termination_clauses=[],
        key_obligations=[],
        sla_summary=None,
        extraction_confidence="HIGH",
    )


def _classification(
    relationship_type: str = "STRATEGIC",
    dependency_tier: str = "HIGH",
    dependency_score: float = 0.75,
    renewal_urgency: str = "OK",
    classification_confidence: str = "HIGH",
    llm_used: bool = False,
) -> RelationshipClassification:
    return RelationshipClassification(
        vendor_id="V-001",
        relationship_type=relationship_type,
        dependency_score=dependency_score,
        dependency_tier=dependency_tier,
        single_source_risk=False,
        contract_coverage="COVERED",
        renewal_urgency=renewal_urgency,
        relationship_age_days=365,
        classification_confidence=classification_confidence,
        llm_used=llm_used,
        reasoning="test reasoning",
    )


def _doc_result(contracts: list[ContractTerms] | None = None) -> DocumentIntelligenceResult:
    return DocumentIntelligenceResult(
        vendor_id="V-001",
        documents_processed=1 if contracts else 0,
        documents_skipped=0,
        extracted_contracts=contracts or [],
        extraction_warnings=[],
    )


def _bundle() -> StructuredDataBundle:
    return StructuredDataBundle(
        vendor_id="V-001",
        programme_id="PROG-001",
        collected_at="2025-01-01T00:00:00+00:00",
        raw_spend_records=[],
        arrival_modes_used=["CHECK_IN"],
        connector_metadata={},
        upload_metadata={},
        checkin_metadata={},
        collection_warnings=[],
    )


def _aggregation(
    summary: SpendSummary | None = None,
    contracts: list[ContractTerms] | None = None,
) -> SpendAggregationResult:
    return SpendAggregationResult(
        vendor_id="V-001",
        summary=summary or _summary(),
        anomalies=[],
        data_quality_flags=[],
        aggregated_at="2025-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# _compute_pcs_contribution
# ---------------------------------------------------------------------------

def test_pcs_full_completeness_with_contract_and_high_confidence():
    s = _summary(completeness="FULL")
    cls = _classification(relationship_type="STRATEGIC", classification_confidence="HIGH")
    doc = _doc_result(contracts=[_contract()])
    contrib = _compute_pcs_contribution(s, cls, doc)
    # FULL(0.10) + contract(0.05) + classified(0.03) + high confidence(0.02) = 0.20
    assert contrib == pytest.approx(0.20)


def test_pcs_partial_completeness():
    s = _summary(completeness="PARTIAL")
    cls = _classification(relationship_type="STRATEGIC", classification_confidence="HIGH")
    doc = _doc_result(contracts=[_contract()])
    contrib = _compute_pcs_contribution(s, cls, doc)
    # PARTIAL(0.06) + contract(0.05) + classified(0.03) + high(0.02) = 0.16
    assert contrib == pytest.approx(0.16)


def test_pcs_none_completeness_no_contract():
    s = _summary(completeness="NONE", total_all_time=None, total_ttm=None)
    cls = _classification(relationship_type="UNKNOWN", classification_confidence="LOW")
    doc = _doc_result(contracts=[])
    contrib = _compute_pcs_contribution(s, cls, doc)
    # NONE(0) + no contract(0) + unknown(0) + low(0) = 0.0
    assert contrib == pytest.approx(0.0)


def test_pcs_capped_at_020():
    s = _summary(completeness="FULL")
    cls = _classification(relationship_type="STRATEGIC", classification_confidence="HIGH")
    doc = _doc_result(contracts=[_contract()])
    contrib = _compute_pcs_contribution(s, cls, doc)
    assert contrib <= 0.20


# ---------------------------------------------------------------------------
# _compute_assembled_gap_severity
# ---------------------------------------------------------------------------

def test_gap_severity_critical_elevation_major_plus_none():
    from cobalt.models.schemas.rs_schema import GapReport
    gr = GapReport(
        gap_severity="MAJOR",
        missing_fields=["spend_total_ttm_usd"],
        low_confidence_fields=[],
        stale_fields=[],
        recommended_actions=[],
    )
    result = _compute_assembled_gap_severity(gr, "NONE")
    assert result == "CRITICAL"


def test_gap_severity_no_elevation_major_partial():
    from cobalt.models.schemas.rs_schema import GapReport
    gr = GapReport(
        gap_severity="MAJOR",
        missing_fields=["spend_total_ttm_usd"],
        low_confidence_fields=[],
        stale_fields=[],
        recommended_actions=[],
    )
    result = _compute_assembled_gap_severity(gr, "PARTIAL")
    assert result == "MAJOR"


def test_gap_severity_none_stays_none():
    from cobalt.models.schemas.rs_schema import GapReport
    gr = GapReport(
        gap_severity="NONE",
        missing_fields=[],
        low_confidence_fields=[],
        stale_fields=[],
        recommended_actions=[],
    )
    result = _compute_assembled_gap_severity(gr, "NONE")
    assert result == "NONE"


# ---------------------------------------------------------------------------
# _reconcile_conflicts
# ---------------------------------------------------------------------------

def test_uncovered_spend_flag():
    s = _summary(invoice_count=5)
    flags = _reconcile_conflicts(s, [], _classification())
    assert "UNCOVERED_SPEND" in flags


def test_contract_deviation_flag():
    s = _summary(total_all_time=500_000.0)
    contracts = [_contract(total_value=100_000.0)]  # 400% deviation
    flags = _reconcile_conflicts(s, contracts, _classification())
    assert "CONTRACT_DEVIATION" in flags


def test_spend_below_contract_flag():
    s = _summary(total_all_time=None)
    contracts = [_contract(total_value=100_000.0)]
    flags = _reconcile_conflicts(s, contracts, _classification())
    assert "SPEND_BELOW_CONTRACT" in flags


def test_classification_incomplete_flag():
    s = _summary()
    cls = _classification(relationship_type="UNKNOWN")
    flags = _reconcile_conflicts(s, [], cls)
    assert "CLASSIFICATION_INCOMPLETE" in flags


def test_renewal_urgent_flag():
    s = _summary()
    cls = _classification(renewal_urgency="URGENT")
    flags = _reconcile_conflicts(s, [], cls)
    assert "CONTRACT_RENEWAL_URGENT" in flags


def test_no_flags_when_clean():
    s = _summary(total_all_time=200_000.0)
    contracts = [_contract(total_value=200_000.0)]
    cls = _classification(relationship_type="STRATEGIC", renewal_urgency="OK")
    flags = _reconcile_conflicts(s, contracts, cls)
    assert "CONTRACT_DEVIATION" not in flags
    assert "UNCOVERED_SPEND" not in flags
    assert "SPEND_BELOW_CONTRACT" not in flags
    assert "CLASSIFICATION_INCOMPLETE" not in flags
    assert "CONTRACT_RENEWAL_URGENT" not in flags


# ---------------------------------------------------------------------------
# _classify_profile_status
# ---------------------------------------------------------------------------

def test_profile_status_complete():
    s = _summary(completeness="FULL")
    cls = _classification(relationship_type="STRATEGIC")
    status = _classify_profile_status(s, cls, "NONE")
    assert status == "COMPLETE"


def test_profile_status_minimal_when_no_spend():
    s = _summary(completeness="NONE", total_all_time=None, total_ttm=None, invoice_count=0)
    cls = _classification(relationship_type="UNKNOWN")
    status = _classify_profile_status(s, cls, "CRITICAL")
    assert status == "MINIMAL"


def test_profile_status_partial_when_sparse():
    s = _summary(completeness="SPARSE")
    cls = _classification()
    status = _classify_profile_status(s, cls, "MINOR")
    assert status == "PARTIAL"


# ---------------------------------------------------------------------------
# assemble_rs_profile — integration tests (mocking writes)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_write_env(tmp_path, monkeypatch):
    """Set WORKSPACE_ROOT to tmp_path and mock sync_to_db."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    # Create vendor directory
    (tmp_path / "PROG-001" / "V-001").mkdir(parents=True)
    return tmp_path


def test_assemble_creates_profile(mock_write_env):
    """Happy path: assemble_rs_profile returns a RelationshipSpendProfile."""
    agg = _aggregation(_summary(completeness="FULL"))
    cls = _classification()
    doc = _doc_result(contracts=[_contract()])
    bun = _bundle()

    with patch("cobalt.db.sync_to_db.sync_to_db"):
        profile = assemble_rs_profile(
            vendor_id="V-001",
            programme_id="PROG-001",
            structured_bundle=bun,
            doc_intelligence=doc,
            spend_aggregation=agg,
            classification=cls,
            current_pcs=0.60,
        )

    assert isinstance(profile, RelationshipSpendProfile)
    assert profile.vendor_id == "V-001"
    assert profile.profile_status in ("COMPLETE", "PARTIAL", "MINIMAL")
    assert profile.pcs_contribution > 0
    assert profile.pcs_total > 0.60


def test_assemble_pcs_does_not_exceed_1(mock_write_env):
    agg = _aggregation(_summary(completeness="FULL"))
    cls = _classification(classification_confidence="HIGH")
    doc = _doc_result(contracts=[_contract()])
    bun = _bundle()

    with patch("cobalt.db.sync_to_db.sync_to_db"):
        profile = assemble_rs_profile(
            vendor_id="V-001",
            programme_id="PROG-001",
            structured_bundle=bun,
            doc_intelligence=doc,
            spend_aggregation=agg,
            classification=cls,
            current_pcs=0.95,
        )

    assert profile.pcs_total <= 1.0


def test_assemble_version_increments(mock_write_env, tmp_path):
    """Second call increments profile_version."""
    agg = _aggregation(_summary(completeness="FULL"))
    cls = _classification()
    doc = _doc_result(contracts=[_contract()])
    bun = _bundle()

    with patch("cobalt.db.sync_to_db.sync_to_db"):
        p1 = assemble_rs_profile(
            vendor_id="V-001",
            programme_id="PROG-001",
            structured_bundle=bun,
            doc_intelligence=doc,
            spend_aggregation=agg,
            classification=cls,
            current_pcs=0.5,
        )
        p2 = assemble_rs_profile(
            vendor_id="V-001",
            programme_id="PROG-001",
            structured_bundle=bun,
            doc_intelligence=doc,
            spend_aggregation=agg,
            classification=cls,
            current_pcs=0.5,
        )

    assert p2.profile_version == p1.profile_version + 1


def test_assemble_returns_failed_profile_on_error(mock_write_env):
    """When assembly raises (not LedgerWriteError), returns FAILED profile."""
    agg = _aggregation(_summary())
    cls = _classification()
    doc = _doc_result()
    bun = _bundle()

    with patch("cobalt.tools.rs_profile_assembler.gap_analyzer.analyse_gaps", side_effect=RuntimeError("boom")):
        with patch("cobalt.db.sync_to_db.sync_to_db"):
            profile = assemble_rs_profile(
                vendor_id="V-001",
                programme_id="PROG-001",
                structured_bundle=bun,
                doc_intelligence=doc,
                spend_aggregation=agg,
                classification=cls,
                current_pcs=0.5,
            )

    assert profile.profile_status == "FAILED"
    assert profile.pcs_contribution == 0.0
    assert "PROFILE_ASSEMBLY_FAILED" in profile.flags


def test_assemble_ledger_write_error_propagates(mock_write_env):
    """LedgerWriteError must propagate (HALT per Rule 4)."""
    from cobalt.core.exceptions import LedgerWriteError

    agg = _aggregation(_summary(completeness="FULL"))
    cls = _classification()
    doc = _doc_result(contracts=[_contract()])
    bun = _bundle()

    with patch("cobalt.db.sync_to_db.sync_to_db"):
        with patch("cobalt.tools.rs_profile_assembler.append_md", side_effect=LedgerWriteError("disk full")):
            with pytest.raises(LedgerWriteError):
                assemble_rs_profile(
                    vendor_id="V-001",
                    programme_id="PROG-001",
                    structured_bundle=bun,
                    doc_intelligence=doc,
                    spend_aggregation=agg,
                    classification=cls,
                    current_pcs=0.5,
                )


def test_assemble_contract_count_correct(mock_write_env):
    contracts = [_contract(), _contract(total_value=50_000.0, effective_date="2023-06-01")]
    doc = _doc_result(contracts=contracts)
    agg = _aggregation(_summary())
    cls = _classification()
    bun = _bundle()

    with patch("cobalt.db.sync_to_db.sync_to_db"):
        profile = assemble_rs_profile(
            vendor_id="V-001",
            programme_id="PROG-001",
            structured_bundle=bun,
            doc_intelligence=doc,
            spend_aggregation=agg,
            classification=cls,
            current_pcs=0.5,
        )

    assert profile.contract_count == 2
