"""Tests for cobalt.intake.steps — StepResult and all individual step modules."""

from decimal import Decimal

import pytest

from cobalt.intake.steps import StepResult
from cobalt.intake.steps import (
    _confirm_rebrand_step,
    _document_extraction_step,
    _fraud_check_step,
    _hr_overlap_step,
    _merge_canonical_step,
    _registry_lookup_step,
    _route_to_human_step,
    _sanctions_check_step,
    _web_research_step,
)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_hit() -> BrainHit:
    return BrainHit(
        matched=False, confidence=0.0, canonical=None, match_type=None,
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def _rebrand_hit(target: str = "New Corp") -> BrainHit:
    return BrainHit(
        matched=True, confidence=0.95, canonical=target, match_type="REBRAND",
        rebrand_match=True, rebrand_target=target, alias_match=False, alias_target=None,
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


def make_plan(**overrides) -> InvestigationPlan:
    defaults = dict(
        depth=InvestigationDepth.FAST,
        steps=[],
        require_human_gate=False,
        require_legal_gate=False,
        fraud_risk=FraudRisk.LOW,
        resolving_question=None,
        reason="test plan",
    )
    defaults.update(overrides)
    return InvestigationPlan(**defaults)


def _web_result(text: str = "IBM is a technology company") -> StepResult:
    return StepResult(
        step="WEB_RESEARCH_FAST",
        success=True,
        early_exit=False,
        exit_status=None,
        data={"research_text": text, "confidence": 0.60 if text else 0.15},
        note="",
    )


# ---------------------------------------------------------------------------
# StepResult
# ---------------------------------------------------------------------------

def test_step_result_instantiates():
    sr = StepResult(
        step="TEST", success=True, early_exit=False,
        exit_status=None, data={"key": "val"}, note="ok",
    )
    assert sr.step == "TEST"
    assert sr.success is True
    assert sr.early_exit is False
    assert sr.exit_status is None
    assert sr.data["key"] == "val"


# ---------------------------------------------------------------------------
# _web_research_step
# ---------------------------------------------------------------------------

class _MockRA:
    def web_research(self, vendor_name, tier, country_hint=None):
        return "IBM is a major tech company based in New York."


class _EmptyRA:
    def web_research(self, vendor_name, tier, country_hint=None):
        return ""


class _RaisingRA:
    def web_research(self, vendor_name, tier, country_hint=None):
        raise RuntimeError("search provider down")


def test_web_research_confidence_high():
    result = _web_research_step.run("IBM", "ibm", "FAST", _MockRA())
    assert result.data["confidence"] == pytest.approx(0.60)
    assert result.data["research_text"] != ""
    assert result.success is True


def test_web_research_confidence_low():
    result = _web_research_step.run("IBM", "ibm", "FAST", _EmptyRA())
    assert result.data["confidence"] == pytest.approx(0.15)
    assert result.success is True


def test_web_research_exception_no_raise():
    result = _web_research_step.run("IBM", "ibm", "FAST", _RaisingRA())
    assert result.success is False
    assert result.early_exit is False
    assert "web_research failed" in result.note


def test_web_research_step_name_matches_tier():
    result = _web_research_step.run("IBM", "ibm", "STANDARD", _MockRA())
    assert result.step == "WEB_RESEARCH_STANDARD"


# ---------------------------------------------------------------------------
# _fraud_check_step — run_basic
# ---------------------------------------------------------------------------

def test_fraud_basic_no_signals():
    result = _fraud_check_step.run_basic(make_profile())
    assert result.data["risk_level"] == "LOW"
    assert result.data["fraud_signals"] == []
    assert result.early_exit is False


def test_fraud_basic_round_numbers_fires():
    ap = ApSignal(invoice_count=5, flags=["ROUND_NUMBERS"], single_approver=False)
    result = _fraud_check_step.run_basic(make_profile(ap_signal=ap))
    assert "INVOICE_ROUND_NUMBERS" in result.data["fraud_signals"]
    assert result.data["risk_score"] == pytest.approx(0.15)


def test_fraud_basic_threshold_avoidance_fires():
    ap = ApSignal(invoice_count=5, flags=["THRESHOLD_AVOIDANCE"], single_approver=False)
    result = _fraud_check_step.run_basic(make_profile(ap_signal=ap))
    assert "INVOICE_THRESHOLD_AVOIDANCE" in result.data["fraud_signals"]


def test_fraud_basic_shell_no_web_fires():
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    result = _fraud_check_step.run_basic(make_profile(erp_signal=erp), web_result=_web_result(""))
    assert "SHELL_NO_WEB_PRESENCE" in result.data["fraud_signals"]


def test_fraud_basic_single_approver_fires():
    ap = ApSignal(invoice_count=5, flags=[], single_approver=True)
    erp = ErpSignal(exists=True, spend=Decimal("30000"), category=None, vendor_ids=[])
    result = _fraud_check_step.run_basic(make_profile(ap_signal=ap, erp_signal=erp))
    assert "INVOICE_SINGLE_APPROVER" in result.data["fraud_signals"]


def test_fraud_basic_critical_early_exit():
    # All four signals: total = 0.30 + 0.15 + 0.25 + 0.15 = 0.85 → CRITICAL
    ap = ApSignal(invoice_count=10, flags=["ROUND_NUMBERS", "THRESHOLD_AVOIDANCE"], single_approver=True)
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    result = _fraud_check_step.run_basic(
        make_profile(ap_signal=ap, erp_signal=erp),
        web_result=_web_result(""),
    )
    assert result.data["risk_level"] == "CRITICAL"
    assert result.early_exit is True
    assert result.exit_status == "BLOCKED"
    assert result.data["recommended_action"] == "BLOCK"


def test_fraud_basic_high_triage_no_early_exit():
    # SHELL_NO_WEB_PRESENCE(0.30) + THRESHOLD_AVOIDANCE(0.25) + ROUND_NUMBERS(0.15) = 0.70 → HIGH
    ap = ApSignal(invoice_count=5, flags=["ROUND_NUMBERS", "THRESHOLD_AVOIDANCE"], single_approver=False)
    erp = ErpSignal(exists=True, spend=Decimal("20000"), category=None, vendor_ids=[])
    result = _fraud_check_step.run_basic(
        make_profile(ap_signal=ap, erp_signal=erp),
        web_result=_web_result(""),
    )
    assert result.data["risk_level"] == "HIGH"
    assert result.early_exit is False
    assert result.data["recommended_action"] == "TRIAGE"


# ---------------------------------------------------------------------------
# _fraud_check_step — run_deep
# ---------------------------------------------------------------------------

def test_fraud_deep_shell_indicators_fires():
    # Indicators: web_empty + spend>10k, single_approver, ROUND_NUMBERS → 3 → fires
    ap = ApSignal(invoice_count=5, flags=["ROUND_NUMBERS"], single_approver=True)
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    result = _fraud_check_step.run_deep(
        make_profile(ap_signal=ap, erp_signal=erp),
        web_result=_web_result(""),
    )
    assert "VENDOR_SHELL_INDICATORS" in result.data["fraud_signals"]


def test_fraud_deep_no_shell_indicators_below_threshold():
    # Only 2 indicators: single_approver + ROUND_NUMBERS (no web result, no spend > 10k)
    ap = ApSignal(invoice_count=3, flags=["ROUND_NUMBERS"], single_approver=True)
    result = _fraud_check_step.run_deep(make_profile(ap_signal=ap))
    assert "VENDOR_SHELL_INDICATORS" not in result.data["fraud_signals"]


def test_fraud_deep_step_name():
    result = _fraud_check_step.run_deep(make_profile())
    assert result.step == "FRAUD_CHECK_DEEP"


# ---------------------------------------------------------------------------
# _merge_canonical_step
# ---------------------------------------------------------------------------

def test_merge_canonical_returns_match_key():
    dedup = DedupResult(status="AUTO_MERGE", match_key="microsoft", match_name="microsoft", similarity=0.97)
    result = _merge_canonical_step.run(make_profile(dedup_result=dedup))
    assert result.data["merged_into"] == "microsoft"
    assert result.data["similarity"] == pytest.approx(0.97)
    assert result.early_exit is False


# ---------------------------------------------------------------------------
# _confirm_rebrand_step
# ---------------------------------------------------------------------------

def test_confirm_rebrand_returns_names():
    profile = make_profile(
        normalized="blackboard",
        brain_hit=_rebrand_hit("Anthology Inc"),
    )
    result = _confirm_rebrand_step.run(profile)
    assert result.data["former_name"] == "blackboard"
    assert result.data["canonical_name"] == "Anthology Inc"
    assert result.early_exit is False


# ---------------------------------------------------------------------------
# _route_to_human_step
# ---------------------------------------------------------------------------

def test_route_to_human_early_exit_triage():
    plan = make_plan(
        resolving_question="Are 'Amazon' and 'Amzon' the same vendor?",
        reason="Dedup candidate",
    )
    result = _route_to_human_step.run(make_profile(), plan)
    assert result.early_exit is True
    assert result.exit_status == "TRIAGE_REQUIRED"
    assert result.data["triage_question"] == "Are 'Amazon' and 'Amzon' the same vendor?"
    assert result.data["triage_reason"] == "Dedup candidate"


# ---------------------------------------------------------------------------
# V1 stubs
# ---------------------------------------------------------------------------

def test_stub_sanctions_check():
    result = _sanctions_check_step.run(make_profile())
    assert result.success is True
    assert result.early_exit is False
    assert result.data["configured"] is False
    assert result.step == "SANCTIONS_CHECK"


def test_stub_registry_lookup():
    result = _registry_lookup_step.run(make_profile())
    assert result.success is True
    assert result.early_exit is False
    assert result.data["configured"] is False


def test_stub_hr_overlap():
    result = _hr_overlap_step.run(make_profile())
    assert result.success is True
    assert result.early_exit is False
    assert result.data["configured"] is False


def test_stub_document_extraction():
    result = _document_extraction_step.run(make_profile())
    assert result.success is True
    assert result.early_exit is False
    assert result.data["configured"] is False
