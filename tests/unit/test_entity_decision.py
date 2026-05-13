"""Tests for cobalt.tools.entity_decision_and_shell_creation — Tool 5."""

from decimal import Decimal

import pytest

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
from cobalt.tools.entity_decision_and_shell_creation import EntityDecision, decide_and_create


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
        ap_signal=ApSignal(invoice_count=0, flags=[], single_approver=False),
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
        reason="test",
    )
    defaults.update(overrides)
    return InvestigationPlan(**defaults)


def _blocked_result() -> StepResult:
    return StepResult(
        step="FRAUD_CHECK_BASIC", success=True, early_exit=True,
        exit_status="BLOCKED",
        data={"fraud_signals": ["SHELL_NO_WEB_PRESENCE"], "recommended_action": "BLOCK"},
        note="blocked",
    )


def _triage_result() -> StepResult:
    return StepResult(
        step="ROUTE_TO_HUMAN", success=True, early_exit=True,
        exit_status="TRIAGE_REQUIRED",
        data={"triage_question": "Is this real?", "triage_reason": "dedup"},
        note="triage",
    )


# ---------------------------------------------------------------------------
# Status determination
# ---------------------------------------------------------------------------

def test_merge_canonical_skip_depth_confirmed(tmp_workspace):
    plan = make_plan(depth=InvestigationDepth.SKIP, steps=["MERGE_CANONICAL"])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert result.status == "CONFIRMED"


def test_person_entity_skip_depth_discarded(tmp_workspace):
    plan = make_plan(depth=InvestigationDepth.SKIP, steps=[])
    result = decide_and_create(
        "John Smith",
        make_profile(entity_type=EntityType.PERSON),
        plan, [], "prog-1",
    )
    assert result.status == "DISCARDED"


def test_internal_entity_skip_depth_discarded(tmp_workspace):
    plan = make_plan(depth=InvestigationDepth.SKIP, steps=[])
    result = decide_and_create(
        "IT Dept",
        make_profile(entity_type=EntityType.INTERNAL),
        plan, [], "prog-1",
    )
    assert result.status == "DISCARDED"


def test_blocked_exit_status_blocked(tmp_workspace):
    plan = make_plan(steps=["FRAUD_CHECK_BASIC"])
    result = decide_and_create("Shady Co", make_profile(), plan, [_blocked_result()], "prog-1")
    assert result.status == "BLOCKED"


def test_triage_exit_status_triage_required(tmp_workspace):
    plan = make_plan(steps=["ROUTE_TO_HUMAN"])
    result = decide_and_create("Unclear Co", make_profile(), plan, [_triage_result()], "prog-1")
    assert result.status == "TRIAGE_REQUIRED"


def test_no_signals_confirmed(tmp_workspace):
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert result.status == "CONFIRMED"


# ---------------------------------------------------------------------------
# CONFIRMED — vendor_id and workspace
# ---------------------------------------------------------------------------

def test_confirmed_vendor_id_generated(tmp_workspace):
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(comparison_key="ibm"), plan, [], "prog-1")
    assert result.vendor_id is not None
    assert result.vendor_id == "ibm"


def test_confirmed_vendor_id_deterministic(tmp_workspace):
    plan = make_plan(steps=[])
    r1 = decide_and_create("IBM", make_profile(comparison_key="ibm"), plan, [], "prog-1")
    r2 = decide_and_create("IBM", make_profile(comparison_key="ibm"), plan, [], "prog-1")
    assert r1.vendor_id == r2.vendor_id


def test_confirmed_workspace_path_set(tmp_workspace):
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert result.workspace_path is not None


def test_confirmed_files_written(tmp_workspace):
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert len(result.files_written) > 0


def test_discarded_no_workspace(tmp_workspace):
    plan = make_plan(depth=InvestigationDepth.SKIP, steps=[])
    result = decide_and_create(
        "John",
        make_profile(entity_type=EntityType.PERSON),
        plan, [], "prog-1",
    )
    assert result.workspace_path is None
    assert result.vendor_id is None


# ---------------------------------------------------------------------------
# data_class
# ---------------------------------------------------------------------------

def test_data_class_b_erp_and_terms(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    terms = {"renewal_date": "2026-01-01", "confidence": 0.85}
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1",
                               extracted_terms=terms, erp_signal=erp)
    assert result.data_class == "CLASS_B"


def test_data_class_c_erp_only(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1", erp_signal=erp)
    assert result.data_class == "CLASS_C"


def test_data_class_d_no_erp(tmp_workspace):
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert result.data_class == "CLASS_D"


def test_data_class_d_erp_no_spend(tmp_workspace):
    erp = ErpSignal(exists=True, spend=None, category=None, vendor_ids=[])
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1", erp_signal=erp)
    assert result.data_class == "CLASS_D"


# ---------------------------------------------------------------------------
# initial_pcs
# ---------------------------------------------------------------------------

def test_initial_pcs_zero_no_signals(tmp_workspace):
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert result.initial_pcs == 0


def test_initial_pcs_increases_with_erp_spend(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("10000"), category=None, vendor_ids=[])
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1", erp_signal=erp)
    assert result.initial_pcs == 12


def test_initial_pcs_increases_with_contract_terms(tmp_workspace):
    erp = ErpSignal(exists=True, spend=Decimal("10000"), category=None, vendor_ids=[])
    terms = {
        "renewal_date": "2026-01-01",
        "auto_renewal": True,
        "contract_value": "100000",
        "price_escalation": "3%",
        "baa_present": True,
        "confidence": 0.85,
    }
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1",
                               extracted_terms=terms, erp_signal=erp)
    # +12 +15 +10 +8 +5 +5 = 55
    assert result.initial_pcs == 55


def test_initial_pcs_baa_true_only(tmp_workspace):
    terms = {"baa_present": True, "confidence": 0.70}
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1", extracted_terms=terms)
    assert result.initial_pcs == 5


def test_initial_pcs_baa_false_not_counted(tmp_workspace):
    terms = {"baa_present": False, "confidence": 0.70}
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1", extracted_terms=terms)
    assert result.initial_pcs == 0


# ---------------------------------------------------------------------------
# Exception → TRIAGE_REQUIRED, no raise
# ---------------------------------------------------------------------------

def test_exception_returns_triage_required(tmp_workspace, monkeypatch):
    import cobalt.tools.entity_decision_and_shell_creation as mod
    monkeypatch.setattr(mod, "build_workspace", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert result.status == "TRIAGE_REQUIRED"
    assert result.triage_question == "Processing error — manual review needed"


def test_exception_never_raises(tmp_workspace, monkeypatch):
    import cobalt.tools.entity_decision_and_shell_creation as mod
    monkeypatch.setattr(mod, "build_workspace", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash")))
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert isinstance(result, EntityDecision)


# ---------------------------------------------------------------------------
# confidence
# ---------------------------------------------------------------------------

def test_confidence_from_brain_hit_when_matched(tmp_workspace):
    hit = BrainHit(
        matched=True, confidence=0.97, canonical="IBM Corporation",
        match_type="KNOWN_VENDOR", rebrand_match=False, rebrand_target=None,
        alias_match=False, alias_target=None,
    )
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(brain_hit=hit), plan, [], "prog-1")
    assert result.confidence == pytest.approx(0.97)


def test_confidence_055_when_not_matched(tmp_workspace):
    plan = make_plan(steps=[])
    result = decide_and_create("IBM", make_profile(), plan, [], "prog-1")
    assert result.confidence == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# triage_question extracted from step_results
# ---------------------------------------------------------------------------

def test_triage_question_extracted_from_steps(tmp_workspace):
    plan = make_plan(steps=["ROUTE_TO_HUMAN"])
    result = decide_and_create("Unclear", make_profile(), plan, [_triage_result()], "prog-1")
    assert result.triage_question == "Is this real?"


def test_block_reason_set_when_blocked(tmp_workspace):
    plan = make_plan(steps=["FRAUD_CHECK_BASIC"])
    result = decide_and_create("Shady Co", make_profile(), plan, [_blocked_result()], "prog-1")
    assert result.block_reason == "FRAUD_DETECTED"
