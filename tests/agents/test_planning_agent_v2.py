"""Tests for PlanningAgent Era 2 methods: create_workflow, evaluate_step, replan."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import cobalt.core.llm_call as llm_call_module
from cobalt.agents.planning_agent import PlanningAgent
from cobalt.brain.loader import invalidate_cache
from cobalt.core.exceptions import WorkflowCreationError
from cobalt.models.schemas.signal_profile_schema import (
    ApSignal,
    BrainHit,
    DedupResult,
    EntityType,
    ErpSignal,
    ScriptType,
    SignalProfile,
)
from cobalt.runtime.execution_state import ExecutionState, ExecutionStatus
from cobalt.runtime.runtime_engine import RuntimeEngine
from cobalt.runtime.workflow_definition import (
    RetryPolicy,
    StepStatus,
    WorkflowDefinition,
    WorkflowStep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_brain_cache():
    invalidate_cache()
    yield
    invalidate_cache()


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _no_hit() -> BrainHit:
    return BrainHit(
        matched=False, confidence=0.0, canonical=None, match_type=None,
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def _known_hit() -> BrainHit:
    return BrainHit(
        matched=True, confidence=0.99, canonical="Acme Corp", match_type="KNOWN_VENDOR",
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def make_profile(**overrides) -> SignalProfile:
    defaults = dict(
        raw="Acme Corp",
        cleaned="Acme Corp",
        script_type=ScriptType.LATIN,
        country_hint=None,
        normalized="acme corp",
        comparison_key="acme-corp",
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


def _step(
    step_id: str,
    step_type: str = "WEB_RESEARCH_STANDARD",
    status: StepStatus = StepStatus.PENDING,
    depends_on: list[str] | None = None,
) -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        step_type=step_type,
        status=status,
        depends_on=depends_on or [],
        condition=None,
        retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0),
        planning_rationale="test",
        added_in_version=1,
    )


def _workflow(
    steps: list[WorkflowStep],
    workflow_id: str = "wf-test",
    programme_id: str = "prog-test",
    replanning_count: int = 0,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        programme_id=programme_id,
        vendor_key="acme-corp",
        vendor_id=None,
        workflow_type="INTAKE_INVESTIGATION",
        created_by="planning_agent",
        created_at=_now(),
        steps=steps,
        replanning_count=replanning_count,
    )


def _state(
    workflow_id: str = "wf-test",
    programme_id: str = "prog-test",
    **kwargs,
) -> ExecutionState:
    return ExecutionState(
        workflow_id=workflow_id,
        programme_id=programme_id,
        status=ExecutionStatus.IN_PROGRESS,
        current_step_id=None,
        started_at=_now(),
        last_updated=_now(),
        **kwargs,
    )


agent = PlanningAgent()


# ---------------------------------------------------------------------------
# create_workflow — INTAKE_INVESTIGATION (5 tests)
# ---------------------------------------------------------------------------

def test_cw_intake_returns_workflow_definition(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(make_profile(brain_hit=_known_hit()), "INTAKE_INVESTIGATION", "prog-test")
    assert isinstance(wf, WorkflowDefinition)


def test_cw_intake_steps_not_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(make_profile(brain_hit=_known_hit()), "INTAKE_INVESTIGATION", "prog-test")
    assert len(wf.steps) > 0


def test_cw_intake_step_ids_are_unique(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(make_profile(brain_hit=_known_hit()), "INTAKE_INVESTIGATION", "prog-test")
    ids = [s.step_id for s in wf.steps]
    assert len(ids) == len(set(ids))


def test_cw_intake_workflow_id_pattern(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(make_profile(brain_hit=_known_hit()), "INTAKE_INVESTIGATION", "prog-test")
    assert wf.workflow_id.startswith("wf-intake-")


def test_cw_intake_file_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(make_profile(brain_hit=_known_hit()), "INTAKE_INVESTIGATION", "prog-test")
    expected = tmp_path / "prog-test" / "workflows" / wf.workflow_id / "workflow.json"
    assert expected.exists()


# ---------------------------------------------------------------------------
# create_workflow — ENRICHMENT (4 tests)
# ---------------------------------------------------------------------------

def test_cw_enrichment_succeeds_with_vendor_id(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(None, "ENRICHMENT", "prog-test", context_overrides={"vendor_id": "v-123"})
    assert isinstance(wf, WorkflowDefinition)
    assert wf.context["vendor_id"] == "v-123"


def test_cw_enrichment_has_exactly_4_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(None, "ENRICHMENT", "prog-test", context_overrides={"vendor_id": "v-123"})
    assert len(wf.steps) == 4


def test_cw_enrichment_step_ids_s1_to_s4(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(None, "ENRICHMENT", "prog-test", context_overrides={"vendor_id": "v-123"})
    assert [s.step_id for s in wf.steps] == ["s1", "s2", "s3", "s4"]


def test_cw_enrichment_s4_diamond_dependency(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = agent.create_workflow(None, "ENRICHMENT", "prog-test", context_overrides={"vendor_id": "v-123"})
    s4 = wf.steps[3]
    assert s4.step_id == "s4"
    assert sorted(s4.depends_on) == ["s2", "s3"]


# ---------------------------------------------------------------------------
# create_workflow — invalid type (1 test)
# ---------------------------------------------------------------------------

def test_cw_invalid_type_raises():
    with pytest.raises(WorkflowCreationError, match="UNSUPPORTED_TYPE"):
        agent.create_workflow(None, "UNSUPPORTED_TYPE", "prog-test")


# ---------------------------------------------------------------------------
# evaluate_step (8 tests)
# ---------------------------------------------------------------------------

def test_es_early_exit_blocked_terminate():
    d = agent.evaluate_step(_step("s1"), {"early_exit": True, "exit_status": "BLOCKED"}, _state())
    assert d.action == "TERMINATE"


def test_es_early_exit_triage_escalate_human():
    d = agent.evaluate_step(_step("s1"), {"early_exit": True, "exit_status": "TRIAGE_REQUIRED"}, _state())
    assert d.action == "ESCALATE_HUMAN"


def test_es_low_confidence_replan(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _workflow([_step("s1"), _step("s2", depends_on=["s1"])], replanning_count=0)
    wf.save()
    state = _state(workflow_id=wf.workflow_id, programme_id=wf.programme_id)
    d = agent.evaluate_step(_step("s1"), {"confidence": 0.20}, state)
    assert d.action == "REPLAN"
    assert d.reason == "low_confidence"


def test_es_normal_confidence_continue():
    d = agent.evaluate_step(_step("s1"), {"confidence": 0.50}, _state())
    assert d.action == "CONTINUE"


def test_es_new_fraud_signals_replan(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _workflow([_step("s1")], replanning_count=0)
    wf.save()
    state = _state(
        workflow_id=wf.workflow_id,
        programme_id=wf.programme_id,
        accumulated_signals={"known_fraud_signals": []},
    )
    d = agent.evaluate_step(_step("s1"), {"fraud_signals": ["shell_company", "round_numbers"]}, state)
    assert d.action == "REPLAN"
    assert d.reason == "new_fraud_signals"


def test_es_known_fraud_signals_continue():
    state = _state(accumulated_signals={"known_fraud_signals": ["shell_company"]})
    d = agent.evaluate_step(_step("s1"), {"fraud_signals": ["shell_company"]}, state)
    assert d.action == "CONTINUE"


def test_es_multiple_matches_replan(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _workflow([_step("s1")], replanning_count=0)
    wf.save()
    state = _state(workflow_id=wf.workflow_id, programme_id=wf.programme_id)
    d = agent.evaluate_step(_step("s1"), {"multiple_matches": True}, state)
    assert d.action == "REPLAN"
    assert d.reason == "entity_ambiguity"


def test_es_replan_limit_reached_continue(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _workflow([_step("s1")], replanning_count=3)
    wf.save()
    state = _state(workflow_id=wf.workflow_id, programme_id=wf.programme_id)
    # Low confidence would normally trigger REPLAN — but replanning_count=3 blocks it.
    d = agent.evaluate_step(_step("s1"), {"confidence": 0.10}, state)
    assert d.action == "CONTINUE"
    assert d.reason == "replan_limit_reached"


# ---------------------------------------------------------------------------
# replan (3 tests)
# ---------------------------------------------------------------------------

def test_replan_valid_response_returns_steps(monkeypatch):
    valid_payload = json.dumps([{
        "step_id": "s2",
        "step_type": "FRAUD_CHECK_BASIC",
        "depends_on": ["s1"],
        "condition": None,
        "max_attempts": 1,
        "backoff_seconds": 0,
        "planning_rationale": "Revised fraud check after new signals",
    }])

    monkeypatch.setattr(llm_call_module, "_call", lambda p, s, m: (valid_payload, 10, 5))

    wf = _workflow([
        _step("s1", status=StepStatus.DONE),
        _step("s2", depends_on=["s1"]),
    ])
    result = agent.replan(wf, wf.steps[0], {}, {})

    assert isinstance(result, list)
    assert len(result) == 1
    assert isinstance(result[0], WorkflowStep)
    assert result[0].step_id == "s2"
    assert result[0].step_type == "FRAUD_CHECK_BASIC"


def test_replan_non_list_response_route_to_human(monkeypatch):
    monkeypatch.setattr(llm_call_module, "_call", lambda p, s, m: ('{"not": "a_list"}', 10, 5))

    wf = _workflow([
        _step("s1", status=StepStatus.DONE),
        _step("s2", depends_on=["s1"]),
    ])
    result = agent.replan(wf, wf.steps[0], {}, {})

    assert len(result) == 1
    assert result[0].step_type == "ROUTE_TO_HUMAN"


def test_replan_llm_exception_route_to_human(monkeypatch):
    def _fail(p, s, m):
        raise RuntimeError("Network failure")

    monkeypatch.setattr(llm_call_module, "_call", _fail)

    wf = _workflow([
        _step("s1", status=StepStatus.DONE),
        _step("s2", depends_on=["s1"]),
    ])
    result = agent.replan(wf, wf.steps[0], {}, {})

    assert len(result) == 1
    assert result[0].step_type == "ROUTE_TO_HUMAN"


# ---------------------------------------------------------------------------
# Integration smoke test (1 test)
# ---------------------------------------------------------------------------

def test_integration_runtime_engine_completed(tmp_path, monkeypatch):
    """Real PlanningAgent + RuntimeEngine: KNOWN_VENDOR path completes without LLM."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    # R03 — KNOWN_VENDOR is a pure-rules path; no LLM call occurs.
    profile = make_profile(brain_hit=_known_hit())
    wf = agent.create_workflow(profile, "INTAKE_INVESTIGATION", "prog-smoke")

    def fraud_check_async(workflow, state, step):
        return {"success": True, "confidence": 0.99}

    engine = RuntimeEngine(
        planner=agent,
        step_registry={"FRAUD_CHECK_ASYNC": fraud_check_async},
    )
    outcome = engine.execute_workflow(wf.workflow_id, "prog-smoke")

    assert outcome.status == "COMPLETED"
