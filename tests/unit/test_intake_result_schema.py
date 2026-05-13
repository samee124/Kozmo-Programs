"""Tests for cobalt.models.schemas.intake_result_schema."""

from decimal import Decimal

from cobalt.models.schemas.investigation_plan_schema import InvestigationDepth, FraudRisk, InvestigationPlan
from cobalt.models.schemas.signal_profile_schema import IntakeStatus
from cobalt.models.schemas.intake_result_schema import (
    IntakeResult,
    IntakeSummary,
    make_discarded,
    make_triage,
)


def _make_plan() -> InvestigationPlan:
    return InvestigationPlan(
        depth=InvestigationDepth.STANDARD,
        steps=["search web", "check sanctions"],
        require_human_gate=False,
        require_legal_gate=False,
        fraud_risk=FraudRisk.LOW,
        resolving_question=None,
        reason="standard intake",
    )


def test_make_discarded_returns_discarded_status():
    result = make_discarded("bad vendor", "gibberish input")
    assert result.status == IntakeStatus.DISCARDED


def test_make_discarded_investigation_plan_depth_skip():
    result = make_discarded("bad vendor", "gibberish input")
    assert result.investigation_plan.depth == InvestigationDepth.SKIP


def test_make_triage_returns_triage_required_status():
    result = make_triage("mystery co", "ambiguous name", "Is this a company?")
    assert result.status == IntakeStatus.TRIAGE_REQUIRED


def test_make_triage_has_triage_question_set():
    result = make_triage("mystery co", "ambiguous name", "Is this a company?")
    assert result.triage_question == "Is this a company?"


def test_intake_result_instantiates_with_all_fields():
    result = IntakeResult(
        raw_input="Acme Ltd",
        canonical_name="Acme Limited",
        vendor_id="v-acme-001",
        status=IntakeStatus.CONFIRMED,
        confidence=0.95,
        resolution_method="BRAIN_MATCH",
        country_code="GB",
        erp_spend=Decimal("15000.00"),
        erp_category="IT_SAAS_SOFTWARE",
        data_class="CLASS_B",
        entity_type="COMPANY",
        triage_reason=None,
        triage_question=None,
        fraud_signals=[],
        fraud_risk="LOW",
        block_reason=None,
        aliases=["acme", "acme uk"],
        linked_doc_ids=["doc-001"],
        extracted_terms={"payment_terms": "net30"},
        investigation_plan=_make_plan(),
    )
    assert result.raw_input == "Acme Ltd"
    assert result.status == IntakeStatus.CONFIRMED
    assert result.confidence == 0.95


def test_intake_summary_instantiates():
    summary = IntakeSummary(total=10, confirmed=6, triage=2, discarded=1, blocked=1)
    assert summary.total == 10
    assert summary.confirmed + summary.triage + summary.discarded + summary.blocked == 10


def test_make_discarded_nullable_fields_are_none_or_empty():
    result = make_discarded("x", "reason")
    assert result.canonical_name is None
    assert result.vendor_id is None
    assert result.fraud_signals == []
    assert result.aliases == []
    assert result.linked_doc_ids == []
    assert result.extracted_terms is None
