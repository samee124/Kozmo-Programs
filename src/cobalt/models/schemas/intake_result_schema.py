"""Intake result schema — final output per candidate from the intake pipeline."""

from dataclasses import dataclass, field
from decimal import Decimal

from cobalt.models.schemas.signal_profile_schema import IntakeStatus
from cobalt.models.schemas.investigation_plan_schema import (
    InvestigationDepth,
    InvestigationPlan,
    FraudRisk,
)


@dataclass
class IntakeResult:
    raw_input: str
    canonical_name: str | None
    vendor_id: str | None
    status: IntakeStatus
    confidence: float
    resolution_method: str
    country_code: str | None
    erp_spend: Decimal | None
    erp_category: str | None
    data_class: str
    entity_type: str
    triage_reason: str | None
    triage_question: str | None
    fraud_signals: list[str]
    fraud_risk: str
    block_reason: str | None
    aliases: list[str]
    linked_doc_ids: list[str]
    extracted_terms: dict | None
    investigation_plan: InvestigationPlan


@dataclass
class IntakeSummary:
    total: int
    confirmed: int
    triage: int
    discarded: int
    blocked: int


def make_discarded(raw: str, reason: str) -> IntakeResult:
    """Build a fully-populated DISCARDED IntakeResult."""
    return IntakeResult(
        raw_input=raw,
        canonical_name=None,
        vendor_id=None,
        status=IntakeStatus.DISCARDED,
        confidence=0.0,
        resolution_method="DISCARDED",
        country_code=None,
        erp_spend=None,
        erp_category=None,
        data_class="CLASS_D",
        entity_type="AMBIGUOUS",
        triage_reason=reason,
        triage_question=None,
        fraud_signals=[],
        fraud_risk="LOW",
        block_reason=None,
        aliases=[],
        linked_doc_ids=[],
        extracted_terms=None,
        investigation_plan=InvestigationPlan(
            depth=InvestigationDepth.SKIP,
            steps=[],
            require_human_gate=False,
            require_legal_gate=False,
            fraud_risk=FraudRisk.LOW,
            resolving_question=None,
            reason=reason,
        ),
    )


def make_triage(raw: str, reason: str, question: str) -> IntakeResult:
    """Build a fully-populated TRIAGE_REQUIRED IntakeResult."""
    return IntakeResult(
        raw_input=raw,
        canonical_name=None,
        vendor_id=None,
        status=IntakeStatus.TRIAGE_REQUIRED,
        confidence=0.0,
        resolution_method="TRIAGE",
        country_code=None,
        erp_spend=None,
        erp_category=None,
        data_class="CLASS_D",
        entity_type="AMBIGUOUS",
        triage_reason=reason,
        triage_question=question,
        fraud_signals=[],
        fraud_risk="LOW",
        block_reason=None,
        aliases=[],
        linked_doc_ids=[],
        extracted_terms=None,
        investigation_plan=InvestigationPlan(
            depth=InvestigationDepth.TRIAGE,
            steps=[],
            require_human_gate=True,
            require_legal_gate=False,
            fraud_risk=FraudRisk.LOW,
            resolving_question=question,
            reason=reason,
        ),
    )
