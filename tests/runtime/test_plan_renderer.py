"""Tests for PlanRenderer — render_to_string and render (disk write)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cobalt.runtime.execution_state import ExecutionState, ExecutionStatus, StepRunRecord
from cobalt.runtime.plan_renderer import PlanRenderer, _summarise_result
from cobalt.runtime.workflow_definition import (
    ReplanEvent,
    RetryPolicy,
    StepStatus,
    WorkflowDefinition,
    WorkflowStep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _step(
    step_id: str,
    step_type: str = "WEB_RESEARCH_STANDARD",
    status: StepStatus = StepStatus.PENDING,
    depends_on: list[str] | None = None,
    rationale: str = "Test rationale",
) -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        step_type=step_type,
        status=status,
        depends_on=depends_on or [],
        condition=None,
        retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0),
        planning_rationale=rationale,
        added_in_version=1,
    )


def _workflow(
    steps: list[WorkflowStep] | None = None,
    workflow_id: str = "wf-render-001",
    programme_id: str = "prog-test",
    context: dict | None = None,
    replanning_history: list[ReplanEvent] | None = None,
    replanning_count: int = 0,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        programme_id=programme_id,
        vendor_key="acme",
        vendor_id=None,
        workflow_type="INTAKE_INVESTIGATION",
        created_by="planning_agent",
        created_at="2026-05-12T10:00:00Z",
        steps=steps or [],
        context=context or {},
        replanning_history=replanning_history or [],
        replanning_count=replanning_count,
    )


def _state(
    workflow_id: str = "wf-render-001",
    programme_id: str = "prog-test",
    status: ExecutionStatus = ExecutionStatus.IN_PROGRESS,
    current_step_id: str | None = None,
    completed_steps: dict | None = None,
    failed_steps: dict | None = None,
    skipped_steps: dict | None = None,
    accumulated_signals: dict | None = None,
    outcome: dict | None = None,
) -> ExecutionState:
    return ExecutionState(
        workflow_id=workflow_id,
        programme_id=programme_id,
        status=status,
        current_step_id=current_step_id,
        started_at="2026-05-12T10:00:00Z",
        last_updated=_now(),
        completed_steps=completed_steps or {},
        failed_steps=failed_steps or {},
        skipped_steps=skipped_steps or {},
        accumulated_signals=accumulated_signals or {},
        outcome=outcome,
    )


def _done_record(step_id: str, result: dict | None = None, attempts: int = 1) -> StepRunRecord:
    return StepRunRecord(
        step_id=step_id,
        status=StepStatus.DONE,
        attempts=attempts,
        started_at="2026-05-12T10:01:00Z",
        completed_at="2026-05-12T10:01:45Z",
        result=result or {"success": True},
        error=None,
    )


def _failed_record(step_id: str, attempts: int = 2) -> StepRunRecord:
    return StepRunRecord(
        step_id=step_id,
        status=StepStatus.FAILED,
        attempts=attempts,
        started_at="2026-05-12T10:01:00Z",
        completed_at="2026-05-12T10:01:45Z",
        result={},
        error="Network error",
    )


def _skipped_record(step_id: str) -> StepRunRecord:
    return StepRunRecord(
        step_id=step_id,
        status=StepStatus.SKIPPED,
        attempts=0,
        started_at="2026-05-12T10:01:00Z",
        completed_at="2026-05-12T10:01:00Z",
        result={"skip_reason": "condition not met"},
        error=None,
    )


renderer = PlanRenderer()


# ---------------------------------------------------------------------------
# 1. Empty workflow — all sections present with "no data" placeholders
# ---------------------------------------------------------------------------

def test_empty_workflow_all_sections_present():
    md = renderer.render_to_string(_workflow(), _state())
    assert "# Workflow — wf-render-001" in md
    assert "## Context" in md
    assert "## Planning Rationale" in md
    assert "_No rationale._" in md
    assert "## Steps" in md
    assert "## Execution Log" in md
    assert "## Accumulated Signals" in md
    assert "_No signals yet._" in md
    assert "## Replanning History" in md
    assert "_No replans yet._" in md
    assert "## Outcome" in md


# ---------------------------------------------------------------------------
# 2. Three PENDING steps → em dashes in execution log
# ---------------------------------------------------------------------------

def test_three_pending_steps_execution_log_em_dashes():
    steps = [_step("s1"), _step("s2"), _step("s3")]
    md = renderer.render_to_string(_workflow(steps=steps), _state())

    # All three step blocks appear
    assert "`s1`" in md
    assert "`s2`" in md
    assert "`s3`" in md

    # Timestamps are em dashes for pending steps
    assert "| `s1`" in md
    # At least two em dashes per pending row (started, completed)
    rows = [line for line in md.splitlines() if line.startswith("| `s")]
    assert len(rows) == 3
    for row in rows:
        assert "| — | — |" in row


# ---------------------------------------------------------------------------
# 3. DONE and PENDING mix → correct icons
# ---------------------------------------------------------------------------

def test_done_and_pending_icons():
    steps = [
        _step("s1", status=StepStatus.DONE),
        _step("s2", status=StepStatus.PENDING),
    ]
    state = _state(
        completed_steps={"s1": _done_record("s1")},
    )
    md = renderer.render_to_string(_workflow(steps=steps), state)
    assert "☑ `s1`" in md
    assert "☐ `s2`" in md


# ---------------------------------------------------------------------------
# 4. FAILED step → ✗ icon and "(attempts: N)" phrase
# ---------------------------------------------------------------------------

def test_failed_step_icon_and_attempts():
    steps = [_step("s1", status=StepStatus.FAILED)]
    state = _state(failed_steps={"s1": _failed_record("s1", attempts=3)})
    md = renderer.render_to_string(_workflow(steps=steps), state)
    assert "✗ `s1`" in md
    assert "failed (attempts: 3)" in md


# ---------------------------------------------------------------------------
# 5. SKIPPED step → ⊘ icon
# ---------------------------------------------------------------------------

def test_skipped_step_icon():
    steps = [_step("s1", status=StepStatus.SKIPPED)]
    state = _state(skipped_steps={"s1": _skipped_record("s1")})
    md = renderer.render_to_string(_workflow(steps=steps), state)
    assert "⊘ `s1`" in md
    assert "skipped" in md


# ---------------------------------------------------------------------------
# 6. RUNNING step → ▶ icon
# ---------------------------------------------------------------------------

def test_running_step_icon():
    steps = [_step("s1", status=StepStatus.RUNNING)]
    md = renderer.render_to_string(_workflow(steps=steps), _state(current_step_id="s1"))
    assert "▶ `s1`" in md
    assert "running" in md


# ---------------------------------------------------------------------------
# 7. Accumulated signals → alphabetical bullet list
# ---------------------------------------------------------------------------

def test_accumulated_signals_alphabetical():
    signals = {"web_confidence": 0.88, "fraud_risk_level": "LOW", "entity_confirmed": True}
    md = renderer.render_to_string(_workflow(), _state(accumulated_signals=signals))
    lines = md.splitlines()
    signal_lines = [l for l in lines if l.startswith("- `") and ":" in l]
    keys = [l.split("`")[1] for l in signal_lines]
    assert keys == sorted(keys)
    assert any("fraud_risk_level" in l for l in signal_lines)
    assert any("web_confidence" in l for l in signal_lines)


# ---------------------------------------------------------------------------
# 8. ReplanEvent renders version section with Added/Removed/Modified
# ---------------------------------------------------------------------------

def test_replan_event_renders_correctly():
    event = ReplanEvent(
        version=2,
        triggered_by="s1",
        trigger_reason="low_confidence",
        rationale="Multiple matches found.",
        steps_added=["s3"],
        steps_removed=["s2"],
        steps_modified=["s4"],
        replanned_at="2026-05-12T10:02:15Z",
    )
    wf = _workflow(replanning_history=[event])
    md = renderer.render_to_string(wf, _state())

    assert "### Version 2 — replanned at 2026-05-12T10:02:15Z" in md
    assert "**Triggered by:** s1" in md
    assert "**Reason:** low_confidence" in md
    assert "Multiple matches found." in md
    assert "- Added: `s3`" in md
    assert "- Removed: `s2`" in md
    assert "- Modified: `s4`" in md


# ---------------------------------------------------------------------------
# 9. COMPLETED outcome → Final step + bulleted result
# ---------------------------------------------------------------------------

def test_completed_outcome_final_step_and_result():
    state = _state(
        status=ExecutionStatus.COMPLETED,
        current_step_id="s3",
        outcome={"reason": "early_termination", "step_id": "s3"},
    )
    md = renderer.render_to_string(_workflow(), state)
    assert "**Status:** COMPLETED" in md
    assert "**Final step:** s3" in md
    assert "- reason: early_termination" in md
    assert "- step_id: s3" in md


# ---------------------------------------------------------------------------
# 10. BLOCKED outcome → Reason and Step lines
# ---------------------------------------------------------------------------

def test_blocked_outcome_reason_and_step():
    state = _state(
        status=ExecutionStatus.BLOCKED,
        outcome={"reason": "human_review_required", "step_id": "s5"},
    )
    md = renderer.render_to_string(_workflow(), state)
    assert "**Status:** BLOCKED" in md
    assert "**Reason:** human_review_required" in md
    assert "**Step:** s5" in md


# ---------------------------------------------------------------------------
# 11. _summarise_result: confidence present
# ---------------------------------------------------------------------------

def test_summarise_result_confidence():
    assert _summarise_result({"confidence": 0.88}) == "confidence=0.88"


# ---------------------------------------------------------------------------
# 12. _summarise_result: empty dict
# ---------------------------------------------------------------------------

def test_summarise_result_empty():
    assert _summarise_result({}) == "—"


# ---------------------------------------------------------------------------
# 13. render() writes plan.md to disk via atomic_write
# ---------------------------------------------------------------------------

def test_render_writes_plan_md(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    r = PlanRenderer(workspace_root=tmp_path)
    wf = _workflow()
    st = _state()
    r.render(wf, st)
    expected = tmp_path / "prog-test" / "workflows" / "wf-render-001" / "plan.md"
    assert expected.exists()


# ---------------------------------------------------------------------------
# 14. render() returns path; content matches render_to_string
# ---------------------------------------------------------------------------

def test_render_returns_path_content_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    r = PlanRenderer(workspace_root=tmp_path)
    wf = _workflow(steps=[_step("s1")])
    st = _state()

    path = r.render(wf, st)
    assert isinstance(path, Path)
    assert path.exists()

    on_disk = path.read_text(encoding="utf-8")
    expected = r.render_to_string(wf, st)
    assert on_disk == expected


# ---------------------------------------------------------------------------
# 15. Engine integration: plan.md exists after workflow run with PlanRenderer
# ---------------------------------------------------------------------------

def test_engine_integration_plan_md_written(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    from cobalt.runtime.runtime_engine import ReplanDecision, RuntimeEngine
    from cobalt.runtime.workflow_definition import WorkflowDefinition

    r = PlanRenderer(workspace_root=tmp_path)

    wf = WorkflowDefinition(
        workflow_id="wf-eng-test",
        programme_id="prog-eng",
        vendor_key="acme",
        vendor_id=None,
        workflow_type="INTAKE_INVESTIGATION",
        created_by="test",
        created_at=_now(),
        steps=[_step("s1"), _step("s2", depends_on=["s1"])],
    )
    wf.save()

    class _Planner:
        def evaluate_step(self, step, result, state):
            return ReplanDecision(action="CONTINUE", reason="ok")

        def replan(self, workflow, completed_step, step_result, signals):
            return []

    engine = RuntimeEngine(
        planner=_Planner(),
        step_registry={"WEB_RESEARCH_STANDARD": lambda wf, st, step: {"success": True}},
        plan_renderer=r,
    )
    engine.execute_workflow("wf-eng-test", "prog-eng")

    plan_path = tmp_path / "prog-eng" / "workflows" / "wf-eng-test" / "plan.md"
    assert plan_path.exists()
    content = plan_path.read_text(encoding="utf-8")
    assert "Workflow — wf-eng-test" in content
    assert "**Status:**" in content
