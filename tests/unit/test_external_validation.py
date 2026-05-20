"""Tests for cobalt.tools.external_validation — Tool 4."""

from decimal import Decimal

import pytest

from cobalt.agents.research_agent import ResearchAgent
from cobalt.intake.steps import StepResult
from cobalt.models.schemas.investigation_plan_schema import (
    FraudRisk,
    InvestigationDepth,
    InvestigationPlan,
)
from cobalt.models.schemas.signal_profile_schema import (
    ApSignal,
    BrainHit,
    DedupResult,
    EntityType,
    ErpSignal,
    ScriptType,
    SignalProfile,
)
from cobalt.tools.external_validation import (
    ValidationResult,
    execute_plan,
    validate,
    validate_all,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_hit() -> BrainHit:
    return BrainHit(
        matched=False, confidence=0.0, canonical=None, match_type=None,
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def make_profile(**overrides) -> SignalProfile:
    defaults = dict(
        raw="Test Corp",
        cleaned="Test Corp",
        script_type=ScriptType.LATIN,
        country_hint=None,
        normalized="test corp",
        comparison_key="test corp",
        brain_hit=_no_hit(),
        erp_signal=ErpSignal(exists=False, spend=None, category=None, vendor_ids=[]),
        ap_signal=ApSignal(invoice_count=0, flags=[], single_approver=False, approver_id=None),
        linked_doc_ids=[],
        spend_hint=None,
        category_hint=None,
        entity_type=EntityType.COMPANY,
        dedup_result=DedupResult(status="UNIQUE", match_key=None, match_name=None, similarity=0.0),
    )
    defaults.update(overrides)
    return SignalProfile(**defaults)


def make_plan(steps: list[str], **overrides) -> InvestigationPlan:
    defaults = dict(
        depth=InvestigationDepth.FAST,
        steps=steps,
        require_human_gate=False,
        require_legal_gate=False,
        fraud_risk=FraudRisk.LOW,
        resolving_question=None,
        reason="test",
    )
    defaults.update(overrides)
    return InvestigationPlan(**defaults)


class _StubRA(ResearchAgent):
    """Stub that always returns empty web results (no API keys in test env)."""
    def web_research(self, vendor_name, tier="STANDARD", country_hint=None):
        return ""


_ra = _StubRA()


# ---------------------------------------------------------------------------
# execute_plan — routing
# ---------------------------------------------------------------------------

def test_execute_plan_route_to_human_triage():
    plan = make_plan(
        steps=["ROUTE_TO_HUMAN"],
        resolving_question="Are they the same?",
        reason="Dedup candidate",
        require_human_gate=True,
    )
    results, final_status, exit_reason = execute_plan("Test Corp", make_profile(), plan, _ra)
    assert final_status == "TRIAGE_REQUIRED"
    assert exit_reason == "TRIAGE_REQUIRED"
    assert len(results) == 1


def test_execute_plan_route_to_human_stops_execution():
    # ROUTE_TO_HUMAN early-exits; FRAUD_CHECK_BASIC should never run
    plan = make_plan(steps=["ROUTE_TO_HUMAN", "FRAUD_CHECK_BASIC"])
    results, final_status, _ = execute_plan("Test Corp", make_profile(), plan, _ra)
    step_names = [r.step for r in results]
    assert "ROUTE_TO_HUMAN" in step_names
    assert "FRAUD_CHECK_BASIC" not in step_names


def test_execute_plan_fraud_basic_no_signals_confirmed():
    plan = make_plan(steps=["FRAUD_CHECK_BASIC"])
    results, final_status, exit_reason = execute_plan("Test Corp", make_profile(), plan, _ra)
    assert final_status == "CONFIRMED"
    assert exit_reason is None
    assert len(results) == 1


def test_execute_plan_fraud_basic_critical_blocked():
    ap = ApSignal(
        invoice_count=10,
        flags=["ROUND_NUMBERS", "THRESHOLD_AVOIDANCE"],
        single_approver=True,
    )
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    profile = make_profile(ap_signal=ap, erp_signal=erp)
    # WEB_RESEARCH_FAST first so web is empty → SHELL_NO_WEB_PRESENCE fires
    plan = make_plan(steps=["WEB_RESEARCH_FAST", "FRAUD_CHECK_BASIC"])
    results, final_status, exit_reason = execute_plan("Test Corp", profile, plan, _ra)
    assert final_status == "BLOCKED"
    assert exit_reason == "BLOCKED"


def test_execute_plan_web_and_fraud_two_results():
    plan = make_plan(steps=["WEB_RESEARCH_FAST", "FRAUD_CHECK_BASIC"])
    results, final_status, _ = execute_plan("IBM", make_profile(), plan, _ra)
    assert len(results) == 2
    step_names = [r.step for r in results]
    assert "WEB_RESEARCH_FAST" in step_names
    assert "FRAUD_CHECK_BASIC" in step_names


def test_execute_plan_unknown_step_skipped():
    plan = make_plan(steps=["NONEXISTENT_STEP_XYZ", "FRAUD_CHECK_BASIC"])
    results, final_status, _ = execute_plan("Test Corp", make_profile(), plan, _ra)
    step_names = [r.step for r in results]
    assert "NONEXISTENT_STEP_XYZ" not in step_names
    assert "FRAUD_CHECK_BASIC" in step_names


def test_execute_plan_legal_gate_exits_triage():
    plan = make_plan(steps=["SANCTIONS_CHECK", "LEGAL_GATE", "FRAUD_CHECK_BASIC"])
    results, final_status, exit_reason = execute_plan("Test Corp", make_profile(), plan, _ra)
    assert final_status == "TRIAGE_REQUIRED"
    step_names = [r.step for r in results]
    assert "FRAUD_CHECK_BASIC" not in step_names


def test_execute_plan_empty_steps_confirmed():
    plan = make_plan(steps=[])
    results, final_status, exit_reason = execute_plan("Test Corp", make_profile(), plan, _ra)
    assert results == []
    assert final_status == "CONFIRMED"
    assert exit_reason is None


# ---------------------------------------------------------------------------
# validate / validate_all
# ---------------------------------------------------------------------------

def test_validate_returns_validation_result():
    plan = make_plan(steps=["FRAUD_CHECK_BASIC"])
    result = validate("IBM", make_profile(), plan, _ra)
    assert isinstance(result, ValidationResult)
    assert result.entry == "IBM"
    assert result.final_status in ("CONFIRMED", "TRIAGE_REQUIRED", "BLOCKED")


def test_validate_never_raises():
    # Even with an unusual profile, validate must not raise
    plan = make_plan(steps=["FRAUD_CHECK_BASIC", "MERGE_CANONICAL"])
    dedup = DedupResult(status="AUTO_MERGE", match_key="ibm", match_name="ibm", similarity=0.99)
    profile = make_profile(dedup_result=dedup)
    result = validate("IBM", profile, plan, _ra)
    assert isinstance(result, ValidationResult)


def test_validate_all_same_length():
    entries = [
        ("IBM", make_profile(), make_plan(steps=["FRAUD_CHECK_BASIC"])),
        ("Aramark", make_profile(), make_plan(steps=["WEB_RESEARCH_FAST"])),
        ("John Smith", make_profile(entity_type=EntityType.COMPANY), make_plan(steps=[])),
    ]
    results = validate_all(entries, _ra)
    assert len(results) == len(entries)


def test_validate_all_all_are_validation_results():
    entries = [
        ("IBM", make_profile(), make_plan(steps=[])),
        ("Test", make_profile(), make_plan(steps=["FRAUD_CHECK_BASIC"])),
    ]
    for r in validate_all(entries, _ra):
        assert isinstance(r, ValidationResult)


def test_validate_all_never_raises():
    entries = [
        ("X", make_profile(), make_plan(steps=["UNKNOWN_STEP_THAT_DOES_NOT_EXIST"])),
    ]
    results = validate_all(entries, _ra)
    assert len(results) == 1
