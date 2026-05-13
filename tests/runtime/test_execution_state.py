"""Tests for ExecutionState persistence and mutation methods."""

import json
import pytest
from pathlib import Path

from cobalt.core.exceptions import WorkflowParseError
from cobalt.runtime.execution_state import ExecutionState, ExecutionStatus, StepRunRecord
from cobalt.runtime.workflow_definition import StepStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> ExecutionState:
    defaults = dict(
        workflow_id="wf-exec-001",
        programme_id="test-prog",
        status=ExecutionStatus.NOT_STARTED,
        current_step_id=None,
        started_at=None,
        last_updated="2026-05-12T10:00:00Z",
        pending_steps=["s1", "s2", "s3"],
    )
    defaults.update(kwargs)
    return ExecutionState(**defaults)


# ---------------------------------------------------------------------------
# 1. Save → load round trip preserves all fields
# ---------------------------------------------------------------------------

def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state(
        status=ExecutionStatus.IN_PROGRESS,
        current_step_id="s2",
        started_at="2026-05-12T10:01:00Z",
        accumulated_signals={"fraud_risk_level": "LOW", "entity_confirmed": True},
        outcome=None,
    )
    state.save()

    loaded = ExecutionState.load("wf-exec-001", "test-prog")
    assert loaded.workflow_id == "wf-exec-001"
    assert loaded.programme_id == "test-prog"
    assert loaded.status == ExecutionStatus.IN_PROGRESS
    assert loaded.current_step_id == "s2"
    assert loaded.started_at == "2026-05-12T10:01:00Z"
    assert loaded.pending_steps == ["s1", "s2", "s3"]
    assert loaded.accumulated_signals["fraud_risk_level"] == "LOW"
    assert loaded.accumulated_signals["entity_confirmed"] is True


# ---------------------------------------------------------------------------
# 2. load returns NOT_STARTED when state.json does not exist
# ---------------------------------------------------------------------------

def test_load_returns_not_started_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = ExecutionState.load("wf-no-file", "test-prog")
    assert state.status == ExecutionStatus.NOT_STARTED
    assert state.workflow_id == "wf-no-file"
    assert state.current_step_id is None
    assert state.started_at is None


# ---------------------------------------------------------------------------
# 3. load raises WorkflowParseError for malformed JSON
# ---------------------------------------------------------------------------

def test_load_raises_for_malformed_json(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state_dir = tmp_path / "test-prog" / "workflows" / "wf-bad"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text("not valid { json }", encoding="utf-8")

    with pytest.raises(WorkflowParseError, match="Malformed JSON"):
        ExecutionState.load("wf-bad", "test-prog")


# ---------------------------------------------------------------------------
# 4. record_step_start updates current_step_id and sets IN_PROGRESS
# ---------------------------------------------------------------------------

def test_record_step_start_updates_current_step_id(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.record_step_start("s1")

    assert state.current_step_id == "s1"
    assert state.status == ExecutionStatus.IN_PROGRESS
    assert state.started_at is not None

    # Verify persisted
    loaded = ExecutionState.load("wf-exec-001", "test-prog")
    assert loaded.current_step_id == "s1"
    assert loaded.status == ExecutionStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# 5. record_step_start does not overwrite started_at when already set
# ---------------------------------------------------------------------------

def test_record_step_start_preserves_started_at(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state(started_at="2026-05-12T09:00:00Z")
    state.record_step_start("s2")

    assert state.started_at == "2026-05-12T09:00:00Z"


# ---------------------------------------------------------------------------
# 6. record_step_complete removes step from pending_steps
# ---------------------------------------------------------------------------

def test_record_step_complete_removes_from_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.record_step_complete("s1", result={"confidence": 0.88}, attempts=1)

    assert "s1" not in state.pending_steps
    assert "s1" in state.completed_steps
    assert state.completed_steps["s1"].status == StepStatus.DONE
    assert state.completed_steps["s1"].attempts == 1


# ---------------------------------------------------------------------------
# 7. record_step_complete accumulates signals from result
# ---------------------------------------------------------------------------

def test_record_step_complete_accumulates_signals(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.record_step_complete("s1", result={"confidence": 0.88, "entity_type": "COMPANY"})

    assert state.accumulated_signals["confidence"] == 0.88
    assert state.accumulated_signals["entity_type"] == "COMPANY"


# ---------------------------------------------------------------------------
# 8. record_step_failure adds to failed_steps and sets FAILED status
# ---------------------------------------------------------------------------

def test_record_step_failure_adds_to_failed_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.record_step_failure("s1", error="Timeout after 30s", attempts=3)

    assert "s1" in state.failed_steps
    assert state.failed_steps["s1"].status == StepStatus.FAILED
    assert state.failed_steps["s1"].error == "Timeout after 30s"
    assert state.failed_steps["s1"].attempts == 3
    assert state.status == ExecutionStatus.FAILED
    assert "s1" not in state.pending_steps


# ---------------------------------------------------------------------------
# 9. record_step_skip adds to skipped_steps, does not affect completed count
# ---------------------------------------------------------------------------

def test_record_step_skip_does_not_affect_completed_count(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.record_step_skip("s2", reason="condition not met")

    assert "s2" in state.skipped_steps
    assert state.skipped_steps["s2"].status == StepStatus.SKIPPED
    assert len(state.completed_steps) == 0
    assert state.status == ExecutionStatus.NOT_STARTED  # skip doesn't change overall status
    assert "s2" not in state.pending_steps


# ---------------------------------------------------------------------------
# 10. accumulate_signals — numeric uses max()
# ---------------------------------------------------------------------------

def test_accumulate_signals_numeric_max(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.accumulate_signals({"confidence": 0.5, "score": 10})
    state.accumulate_signals({"confidence": 0.9, "score": 7})

    assert state.accumulated_signals["confidence"] == 0.9
    assert state.accumulated_signals["score"] == 10


# ---------------------------------------------------------------------------
# 11. accumulate_signals — list deduplication
# ---------------------------------------------------------------------------

def test_accumulate_signals_list_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.accumulate_signals({"fraud_signals": ["shell_company", "unusual_address"]})
    state.accumulate_signals({"fraud_signals": ["unusual_address", "new_signal"]})

    signals = state.accumulated_signals["fraud_signals"]
    assert "shell_company" in signals
    assert "unusual_address" in signals
    assert "new_signal" in signals
    # Deduplicated — unusual_address appears once
    assert signals.count("unusual_address") == 1


# ---------------------------------------------------------------------------
# 12. accumulate_signals — boolean OR
# ---------------------------------------------------------------------------

def test_accumulate_signals_boolean_or(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.accumulate_signals({"entity_confirmed": False, "fraud_detected": False})
    state.accumulate_signals({"entity_confirmed": True, "fraud_detected": False})

    assert state.accumulated_signals["entity_confirmed"] is True
    assert state.accumulated_signals["fraud_detected"] is False


# ---------------------------------------------------------------------------
# 13. accumulate_signals — string overwrite (latest wins)
# ---------------------------------------------------------------------------

def test_accumulate_signals_string_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.accumulate_signals({"risk_level": "LOW"})
    state.accumulate_signals({"risk_level": "HIGH"})

    assert state.accumulated_signals["risk_level"] == "HIGH"


# ---------------------------------------------------------------------------
# 14. Crash recovery — RUNNING step on reload means re-execute
# ---------------------------------------------------------------------------

def test_running_step_survives_reload(tmp_path, monkeypatch):
    """A step left RUNNING after a crash should still be present in current_step_id on reload."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.record_step_start("s1")  # marks s1 as RUNNING and persists

    # Simulate crash + reload
    reloaded = ExecutionState.load("wf-exec-001", "test-prog")
    assert reloaded.current_step_id == "s1"
    assert reloaded.status == ExecutionStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# 15. round trip preserves completed_steps with nested StepRunRecord
# ---------------------------------------------------------------------------

def test_round_trip_with_completed_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state()
    state.record_step_complete("s1", result={"confidence": 0.88, "text": "hello"}, attempts=2)

    loaded = ExecutionState.load("wf-exec-001", "test-prog")
    rec = loaded.completed_steps["s1"]
    assert rec.step_id == "s1"
    assert rec.status == StepStatus.DONE
    assert rec.attempts == 2
    assert rec.result["confidence"] == 0.88
    assert rec.error is None


# ---------------------------------------------------------------------------
# 16. programme_id survives save → load round trip  [S16 amendment]
# ---------------------------------------------------------------------------

def test_programme_id_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state = _make_state(programme_id="prog-roundtrip")
    state.save()
    loaded = ExecutionState.load("wf-exec-001", "prog-roundtrip")
    assert loaded.programme_id == "prog-roundtrip"
