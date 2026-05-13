"""Tests for WorkflowDefinition, WorkflowStep, and condition evaluator."""

import json
import pytest
from datetime import datetime, timezone
from pathlib import Path

from cobalt.core.exceptions import (
    InvalidConditionExpression,
    StepIdCollision,
    WorkflowParseError,
)
from cobalt.runtime.workflow_definition import (
    ReplanEvent,
    RetryPolicy,
    StepStatus,
    WorkflowDefinition,
    WorkflowStep,
    evaluate_condition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_step(step_id: str, status: StepStatus = StepStatus.PENDING) -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        step_type="WEB_RESEARCH_DEEP",
        status=status,
        depends_on=[],
        condition=None,
        retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=5),
        planning_rationale="test",
        added_in_version=1,
    )


def _make_workflow(**kwargs) -> WorkflowDefinition:
    defaults = dict(
        workflow_id="wf-test-001",
        programme_id="test-prog",
        vendor_key="acme",
        vendor_id=None,
        workflow_type="INTAKE_INVESTIGATION",
        created_by="planning_agent",
        created_at="2026-05-12T10:00:00Z",
        version=1,
        replanning_count=0,
        context={"entity_type": "COMPANY"},
        steps=[_make_step("s1"), _make_step("s2")],
        replanning_history=[],
    )
    defaults.update(kwargs)
    return WorkflowDefinition(**defaults)


# ---------------------------------------------------------------------------
# 1. Serialisation round trip (to_dict / from_dict via JSON)
# ---------------------------------------------------------------------------

def test_round_trip_serialisation():
    wf = _make_workflow()
    d = wf.to_dict()
    json_str = json.dumps(d)
    raw = json.loads(json_str)

    assert raw["workflow_id"] == "wf-test-001"
    assert raw["programme_id"] == "test-prog"
    assert raw["vendor_key"] == "acme"
    assert raw["version"] == 1
    assert raw["replanning_count"] == 0
    assert len(raw["steps"]) == 2
    assert raw["steps"][0]["step_id"] == "s1"
    assert raw["steps"][0]["status"] == "PENDING"
    assert raw["steps"][0]["retry_policy"]["max_attempts"] == 2
    assert raw["steps"][0]["retry_policy"]["backoff_seconds"] == 5
    assert raw["context"] == {"entity_type": "COMPANY"}
    assert raw["replanning_history"] == []


# ---------------------------------------------------------------------------
# 2. Save to disk + load produces equal object
# ---------------------------------------------------------------------------

def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _make_workflow()
    saved_path = wf.save()

    assert saved_path.exists()
    loaded = WorkflowDefinition.load("wf-test-001", "test-prog")

    assert loaded.workflow_id == wf.workflow_id
    assert loaded.programme_id == wf.programme_id
    assert loaded.vendor_key == wf.vendor_key
    assert loaded.vendor_id == wf.vendor_id
    assert loaded.workflow_type == wf.workflow_type
    assert loaded.version == wf.version
    assert loaded.replanning_count == wf.replanning_count
    assert loaded.context == wf.context
    assert len(loaded.steps) == len(wf.steps)
    assert loaded.steps[0].step_id == "s1"
    assert loaded.steps[0].status == StepStatus.PENDING
    assert loaded.steps[0].retry_policy.max_attempts == 2
    assert loaded.steps[1].step_id == "s2"


# ---------------------------------------------------------------------------
# 3. load raises FileNotFoundError when file is missing
# ---------------------------------------------------------------------------

def test_load_file_not_found_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        WorkflowDefinition.load("nonexistent", "test-prog")


# ---------------------------------------------------------------------------
# 4. load raises WorkflowParseError for malformed JSON
# ---------------------------------------------------------------------------

def test_load_malformed_json_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf_dir = tmp_path / "test-prog" / "workflows" / "wf-bad"
    wf_dir.mkdir(parents=True)
    (wf_dir / "workflow.json").write_text("{ not valid json }", encoding="utf-8")

    with pytest.raises(WorkflowParseError, match="Malformed JSON"):
        WorkflowDefinition.load("wf-bad", "test-prog")


# ---------------------------------------------------------------------------
# 5. apply_revision preserves DONE steps, replaces PENDING
# ---------------------------------------------------------------------------

def test_apply_revision_preserves_done_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _make_workflow(steps=[
        _make_step("s1", StepStatus.DONE),
        _make_step("s2", StepStatus.PENDING),
    ])
    new_steps = [_make_step("s3")]
    wf.apply_revision(new_steps, triggered_by="s1", trigger_reason="low_confidence", rationale="R")

    step_ids = [s.step_id for s in wf.steps]
    assert "s1" in step_ids
    assert "s2" not in step_ids
    assert "s3" in step_ids


# ---------------------------------------------------------------------------
# 6. apply_revision increments version and replanning_count
# ---------------------------------------------------------------------------

def test_apply_revision_increments_version(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _make_workflow()
    assert wf.version == 1
    assert wf.replanning_count == 0

    wf.apply_revision([_make_step("s3")], "s1", "low_confidence", "rationale")

    assert wf.version == 2
    assert wf.replanning_count == 1


# ---------------------------------------------------------------------------
# 7. apply_revision appends ReplanEvent to replanning_history
# ---------------------------------------------------------------------------

def test_apply_revision_appends_replan_history(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _make_workflow()
    wf.apply_revision([_make_step("s3")], "s1", "low_confidence", "reason A")

    assert len(wf.replanning_history) == 1
    event = wf.replanning_history[0]
    assert event.version == 2
    assert event.triggered_by == "s1"
    assert event.trigger_reason == "low_confidence"
    assert event.rationale == "reason A"
    assert "s3" in event.steps_added


# ---------------------------------------------------------------------------
# 8. replanning_history accumulates across multiple revisions
# ---------------------------------------------------------------------------

def test_replanning_history_accumulates_across_revisions(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _make_workflow(steps=[_make_step("s1", StepStatus.DONE)])
    wf.apply_revision([_make_step("s2")], "s1", "fraud_signals", "first replan")
    wf.apply_revision([_make_step("s3")], "s2", "low_confidence", "second replan")

    assert len(wf.replanning_history) == 2
    assert wf.replanning_history[0].version == 2
    assert wf.replanning_history[1].version == 3
    assert wf.version == 3
    assert wf.replanning_count == 2


# ---------------------------------------------------------------------------
# 9. apply_revision raises StepIdCollision when revised step matches completed
# ---------------------------------------------------------------------------

def test_step_id_collision_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _make_workflow(steps=[
        _make_step("s1", StepStatus.DONE),
        _make_step("s2", StepStatus.PENDING),
    ])
    # s1 is already DONE — cannot include it in revised_steps
    with pytest.raises(StepIdCollision):
        wf.apply_revision([_make_step("s1")], "s2", "test", "rationale")


# ---------------------------------------------------------------------------
# 10. apply_revision sets added_in_version on new steps
# ---------------------------------------------------------------------------

def test_apply_revision_sets_added_in_version():
    wf = _make_workflow()
    new_step = _make_step("s99")
    new_step.added_in_version = 1  # will be overwritten
    wf.apply_revision([new_step], "s1", "reason", "rationale")

    added = next(s for s in wf.steps if s.step_id == "s99")
    assert added.added_in_version == 2


# ---------------------------------------------------------------------------
# 11. Condition evaluator — comparison operators
# ---------------------------------------------------------------------------

def test_condition_evaluator_comparison_operators():
    results = {"s1": {"confidence": 0.88, "risk_level": "LOW", "count": 5}}

    assert evaluate_condition("s1.confidence >= 0.4", results) is True
    assert evaluate_condition("s1.confidence >= 0.9", results) is False
    assert evaluate_condition("s1.confidence > 0.8", results) is True
    assert evaluate_condition("s1.confidence < 0.5", results) is False
    assert evaluate_condition("s1.confidence <= 0.88", results) is True
    assert evaluate_condition("s1.count == 5", results) is True
    assert evaluate_condition("s1.count != 5", results) is False
    assert evaluate_condition("s1.risk_level == 'LOW'", results) is True


# ---------------------------------------------------------------------------
# 12. Condition evaluator — boolean operators
# ---------------------------------------------------------------------------

def test_condition_evaluator_boolean_operators():
    results = {
        "s1": {"matched": True, "confidence": 0.88},
        "s2": {"risk_level": "LOW"},
    }

    assert evaluate_condition("s1.matched == true and s1.confidence >= 0.5", results) is True
    assert evaluate_condition("s1.matched == true and s1.confidence >= 0.99", results) is False
    assert evaluate_condition("s1.matched == false or s2.risk_level == 'LOW'", results) is True
    assert evaluate_condition("not s1.matched == false", results) is True


# ---------------------------------------------------------------------------
# 13. Condition evaluator — null literal
# ---------------------------------------------------------------------------

def test_condition_evaluator_null_literal():
    results = {"s1": {"web_text": None, "value": 42}}

    assert evaluate_condition("s1.web_text == null", results) is True
    assert evaluate_condition("s1.web_text != null", results) is False
    assert evaluate_condition("s1.value != null", results) is True


# ---------------------------------------------------------------------------
# 14. Condition evaluator — missing step reference returns False
# ---------------------------------------------------------------------------

def test_condition_evaluator_missing_step_returns_false():
    results = {"s1": {"confidence": 0.88}}

    # Step s2 doesn't exist
    assert evaluate_condition("s2.confidence >= 0.4", results) is False
    # Field missing on existing step
    assert evaluate_condition("s1.nonexistent_field >= 0.4", results) is False


# ---------------------------------------------------------------------------
# 15. Condition evaluator — disallowed code is rejected
# ---------------------------------------------------------------------------

def test_condition_evaluator_disallowed_code_rejected():
    results = {}

    # Function calls are not allowed
    with pytest.raises(InvalidConditionExpression):
        evaluate_condition("__import__('os').system('rm -rf /')", results)

    # Subscript (dict/list access) not allowed
    with pytest.raises(InvalidConditionExpression):
        evaluate_condition("s1['confidence'] >= 0.4", results)

    # Lambda not allowed
    with pytest.raises(InvalidConditionExpression):
        evaluate_condition("(lambda: True)()", results)


# ---------------------------------------------------------------------------
# 16. Condition evaluator — syntax error raises InvalidConditionExpression
# ---------------------------------------------------------------------------

def test_condition_evaluator_syntax_error_raises():
    with pytest.raises(InvalidConditionExpression):
        evaluate_condition("s1.confidence >>=== garbage @@", {})


# ---------------------------------------------------------------------------
# 17. Workflow with null vendor_id serialises and loads correctly
# ---------------------------------------------------------------------------

def test_workflow_null_vendor_id_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _make_workflow(vendor_id=None, vendor_key=None)
    wf.save()
    loaded = WorkflowDefinition.load("wf-test-001", "test-prog")
    assert loaded.vendor_id is None
    assert loaded.vendor_key is None


# ---------------------------------------------------------------------------
# 18. Save uses explicit path when provided
# ---------------------------------------------------------------------------

def test_save_uses_explicit_path(tmp_path):
    wf = _make_workflow()
    explicit_path = tmp_path / "custom" / "workflow.json"
    saved = wf.save(path=explicit_path)
    assert saved == explicit_path
    assert explicit_path.exists()
    raw = json.loads(explicit_path.read_text())
    assert raw["workflow_id"] == "wf-test-001"
