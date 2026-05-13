"""Tests for RuntimeEngine — execution loop, crash recovery, retry, replanning."""

import pytest
from datetime import datetime, timezone
from pathlib import Path

from cobalt.core.exceptions import StepRegistryMiss
from cobalt.runtime.execution_state import ExecutionState, ExecutionStatus, StepRunRecord
from cobalt.runtime.runtime_engine import (
    ReplanDecision,
    RuntimeEngine,
    StepFatal,
    StepRetryable,
    WorkflowOutcome,
)
from cobalt.runtime.workflow_definition import (
    RetryPolicy,
    StepStatus,
    WorkflowDefinition,
    WorkflowStep,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _step(
    step_id: str,
    step_type: str = "FAKE",
    status: StepStatus = StepStatus.PENDING,
    depends_on: list[str] | None = None,
    condition: str | None = None,
    max_attempts: int = 1,
    backoff: int = 0,
) -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        step_type=step_type,
        status=status,
        depends_on=depends_on or [],
        condition=condition,
        retry_policy=RetryPolicy(max_attempts=max_attempts, backoff_seconds=backoff),
        planning_rationale="test",
        added_in_version=1,
    )


def _workflow(
    steps: list[WorkflowStep],
    workflow_id: str = "wf-test",
    programme_id: str = "prog-test",
    **kwargs,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        programme_id=programme_id,
        vendor_key="acme",
        vendor_id=None,
        workflow_type="INTAKE_INVESTIGATION",
        created_by="planning_agent",
        created_at=_now(),
        steps=steps,
        **kwargs,
    )


def _save_workflow(wf: WorkflowDefinition, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf.save()


def _save_state(state: ExecutionState, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    state.save()


class _AlwaysContinuePlanner:
    """Minimal planner that always says CONTINUE."""
    def evaluate_step(self, step, result, state):
        return ReplanDecision(action="CONTINUE", reason="ok")

    def replan(self, workflow, completed_step, step_result, accumulated_signals):
        return []


class _RecordingPlanner:
    """Planner that returns a pre-configured sequence of decisions."""
    def __init__(self, decisions: list[ReplanDecision], replan_fn=None):
        self.decisions = decisions
        self.replan_fn = replan_fn
        self.evaluate_calls = 0
        self.replan_calls = 0

    def evaluate_step(self, step, result, state):
        d = (
            self.decisions[self.evaluate_calls]
            if self.evaluate_calls < len(self.decisions)
            else ReplanDecision(action="CONTINUE", reason="default")
        )
        self.evaluate_calls += 1
        return d

    def replan(self, workflow, completed_step, step_result, accumulated_signals):
        self.replan_calls += 1
        if self.replan_fn:
            return self.replan_fn(workflow)
        return []


def _make_engine(planner=None, registry=None, renderer=None) -> RuntimeEngine:
    if planner is None:
        planner = _AlwaysContinuePlanner()
    if registry is None:
        registry = {"FAKE": lambda wf, st, step: {"success": True, "confidence": 0.9}}
    return RuntimeEngine(planner=planner, step_registry=registry, plan_renderer=renderer)


# ---------------------------------------------------------------------------
# 1. 3-step linear workflow runs to COMPLETED
# ---------------------------------------------------------------------------

def test_three_step_linear_workflow_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _workflow([_step("s1"), _step("s2"), _step("s3")])
    wf.save()

    engine = _make_engine()
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "COMPLETED"
    state = ExecutionState.load("wf-test", "prog-test")
    assert "s1" in state.completed_steps
    assert "s2" in state.completed_steps
    assert "s3" in state.completed_steps
    assert state.status == ExecutionStatus.COMPLETED


# ---------------------------------------------------------------------------
# 2. Step with depends_on=[] runs first regardless of list position
# ---------------------------------------------------------------------------

def test_no_dep_step_runs_before_dependent(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    order = []
    registry = {
        "FAKE": lambda wf, st, step: (order.append(step.step_id), {"success": True})[1],
    }
    # s2 is listed first but depends on s1; s1 has no deps.
    wf = _workflow([_step("s2", depends_on=["s1"]), _step("s1")])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "COMPLETED"
    assert order.index("s1") < order.index("s2")


# ---------------------------------------------------------------------------
# 3. Step with depends_on=[s1, s2] waits until BOTH are DONE
# ---------------------------------------------------------------------------

def test_step_waits_for_all_dependencies(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    order = []
    registry = {
        "FAKE": lambda wf, st, step: (order.append(step.step_id), {"success": True})[1],
    }
    wf = _workflow([
        _step("s1"),
        _step("s2"),
        _step("s3", depends_on=["s1", "s2"]),
    ])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "COMPLETED"
    assert order.index("s3") > order.index("s1")
    assert order.index("s3") > order.index("s2")


# ---------------------------------------------------------------------------
# 4. Step result signals merge into accumulated_signals
# ---------------------------------------------------------------------------

def test_step_result_merges_into_accumulated_signals(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    registry = {
        "FAKE": lambda wf, st, step: {"confidence": 0.88, "entity_type": "COMPANY"},
    }
    wf = _workflow([_step("s1")])
    wf.save()

    engine = _make_engine(registry=registry)
    engine.execute_workflow("wf-test", "prog-test")

    state = ExecutionState.load("wf-test", "prog-test")
    assert state.accumulated_signals["confidence"] == 0.88
    assert state.accumulated_signals["entity_type"] == "COMPANY"


# ---------------------------------------------------------------------------
# 5. Step condition met → step runs
# ---------------------------------------------------------------------------

def test_condition_true_step_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    registry = {
        "FAKE": lambda wf, st, step: {"confidence": 0.7, "success": True},
    }
    wf = _workflow([
        _step("s1"),
        _step("s2", condition="s1.confidence >= 0.4"),
    ])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    state = ExecutionState.load("wf-test", "prog-test")
    assert outcome.status == "COMPLETED"
    assert "s2" in state.completed_steps


# ---------------------------------------------------------------------------
# 6. Step condition False → SKIPPED, next step runs
# ---------------------------------------------------------------------------

def test_condition_false_step_skipped_next_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    registry = {
        "FAKE": lambda wf, st, step: {"confidence": 0.2, "success": True},
    }
    wf = _workflow([
        _step("s1"),
        _step("s2", condition="s1.confidence >= 0.9"),  # False — skipped
        _step("s3"),
    ])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    state = ExecutionState.load("wf-test", "prog-test")
    assert outcome.status == "COMPLETED"
    assert "s2" in state.skipped_steps
    assert "s3" in state.completed_steps


# ---------------------------------------------------------------------------
# 7. Condition references missing step → SKIPPED
# ---------------------------------------------------------------------------

def test_condition_missing_step_reference_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    wf = _workflow([
        _step("s1", condition="s99.confidence >= 0.4"),  # s99 never ran
    ])
    wf.save()

    engine = _make_engine()
    outcome = engine.execute_workflow("wf-test", "prog-test")

    state = ExecutionState.load("wf-test", "prog-test")
    assert "s1" in state.skipped_steps


# ---------------------------------------------------------------------------
# 8. StepRetryable twice, succeeds on attempt 3 (max_attempts=3)
# ---------------------------------------------------------------------------

def test_step_retryable_succeeds_on_third_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    call_count = {"n": 0}

    def flaky(wf, st, step):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise StepRetryable("transient error")
        return {"success": True}

    registry = {"FAKE": flaky}
    wf = _workflow([_step("s1", max_attempts=3, backoff=0)])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    state = ExecutionState.load("wf-test", "prog-test")
    assert outcome.status == "COMPLETED"
    assert "s1" in state.completed_steps
    assert state.completed_steps["s1"].attempts == 3


# ---------------------------------------------------------------------------
# 9. StepRetryable exhausts max_attempts → step FAILED, workflow continues
# ---------------------------------------------------------------------------

def test_step_retryable_exhausted_step_fails_workflow_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def always_retryable(wf, st, step):
        raise StepRetryable("always transient")

    registry = {
        "FAKE": always_retryable,
        "NEXT": lambda wf, st, step: {"success": True},
    }
    wf = _workflow([
        _step("s1", step_type="FAKE", max_attempts=2, backoff=0),
        _step("s2", step_type="NEXT"),  # s2 has no dep on s1
    ])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    state = ExecutionState.load("wf-test", "prog-test")
    assert "s1" in state.failed_steps
    assert state.failed_steps["s1"].attempts == 2
    # s2 has no dep on s1, so it can still run
    assert "s2" in state.completed_steps


# ---------------------------------------------------------------------------
# 10. StepFatal → step FAILED, workflow aborts with status=FAILED
# ---------------------------------------------------------------------------

def test_step_fatal_aborts_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def fatal_step(wf, st, step):
        raise StepFatal("permanent failure")

    registry = {
        "FAKE": fatal_step,
        "NEXT": lambda wf, st, step: {"success": True},
    }
    wf = _workflow([_step("s1", step_type="FAKE"), _step("s2", step_type="NEXT")])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "FAILED"
    state = ExecutionState.load("wf-test", "prog-test")
    assert "s1" in state.failed_steps
    assert "s2" not in state.completed_steps  # never ran


# ---------------------------------------------------------------------------
# 11. Unexpected exception → FAILED with "unexpected:" prefix
# ---------------------------------------------------------------------------

def test_unexpected_exception_aborts_with_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def buggy(wf, st, step):
        raise ValueError("oops something weird")

    registry = {"FAKE": buggy}
    wf = _workflow([_step("s1")])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "FAILED"
    state = ExecutionState.load("wf-test", "prog-test")
    assert "s1" in state.failed_steps
    assert state.failed_steps["s1"].error.startswith("unexpected:")


# ---------------------------------------------------------------------------
# 12. Crash recovery — s1=DONE in state.json → s1 callable never re-invoked
# ---------------------------------------------------------------------------

def test_crash_recovery_skips_completed_step(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    s1_call_count = {"n": 0}

    def count_s1(wf, st, step):
        s1_call_count["n"] += 1
        return {"success": True}

    registry = {
        "S1_STEP": count_s1,
        "S2_STEP": lambda wf, st, step: {"success": True},
    }

    wf = _workflow([
        _step("s1", step_type="S1_STEP"),
        _step("s2", step_type="S2_STEP"),
    ])
    wf.save()

    # Simulate crash: s1 is already DONE in state.json
    state = ExecutionState(
        workflow_id="wf-test",
        programme_id="prog-test",
        status=ExecutionStatus.IN_PROGRESS,
        current_step_id="s1",
        started_at=_now(),
        last_updated=_now(),
        pending_steps=["s2"],
        completed_steps={
            "s1": StepRunRecord(
                step_id="s1",
                status=StepStatus.DONE,
                attempts=1,
                started_at=_now(),
                completed_at=_now(),
                result={"success": True},
                error=None,
            )
        },
    )
    state.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "COMPLETED"
    # s1 callable was NEVER called — crash recovery confirmed
    assert s1_call_count["n"] == 0, (
        f"s1 was called {s1_call_count['n']} times — crash recovery FAILED"
    )
    final_state = ExecutionState.load("wf-test", "prog-test")
    assert "s2" in final_state.completed_steps


# ---------------------------------------------------------------------------
# 13. Crash during s2 (RUNNING on disk) → s2 re-executed from scratch
# ---------------------------------------------------------------------------

def test_crash_mid_step_reruns_running_step(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    s2_calls = {"n": 0}

    def count_s2(wf, st, step):
        s2_calls["n"] += 1
        return {"success": True}

    registry = {
        "S1_STEP": lambda wf, st, step: {"success": True},
        "S2_STEP": count_s2,
    }

    wf = _workflow([
        _step("s1", step_type="S1_STEP"),
        _step("s2", step_type="S2_STEP"),
    ])
    wf.save()

    # Simulate crash mid-s2: s1 done, s2 left as current_step_id (RUNNING), not in completed
    state = ExecutionState(
        workflow_id="wf-test",
        programme_id="prog-test",
        status=ExecutionStatus.IN_PROGRESS,
        current_step_id="s2",
        started_at=_now(),
        last_updated=_now(),
        pending_steps=["s2"],
        completed_steps={
            "s1": StepRunRecord(
                step_id="s1",
                status=StepStatus.DONE,
                attempts=1,
                started_at=_now(),
                completed_at=_now(),
                result={"success": True},
                error=None,
            )
        },
    )
    state.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "COMPLETED"
    assert s2_calls["n"] == 1  # re-executed exactly once


# ---------------------------------------------------------------------------
# 14. COMPLETED state → engine returns immediately, no steps run
# ---------------------------------------------------------------------------

def test_completed_state_returns_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    call_count = {"n": 0}

    def counting(wf, st, step):
        call_count["n"] += 1
        return {"success": True}

    registry = {"FAKE": counting}
    wf = _workflow([_step("s1")])
    wf.save()

    state = ExecutionState(
        workflow_id="wf-test",
        programme_id="prog-test",
        status=ExecutionStatus.COMPLETED,
        current_step_id=None,
        started_at=_now(),
        last_updated=_now(),
    )
    state.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "COMPLETED"
    assert call_count["n"] == 0  # no steps executed


# ---------------------------------------------------------------------------
# 15. BLOCKED state → engine returns immediately, no steps run
# ---------------------------------------------------------------------------

def test_blocked_state_returns_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    call_count = {"n": 0}
    registry = {"FAKE": lambda wf, st, step: (call_count.update({"n": call_count["n"] + 1}), {})[1]}

    wf = _workflow([_step("s1")])
    wf.save()

    state = ExecutionState(
        workflow_id="wf-test",
        programme_id="prog-test",
        status=ExecutionStatus.BLOCKED,
        current_step_id=None,
        started_at=_now(),
        last_updated=_now(),
        outcome={"reason": "pre_blocked"},
    )
    state.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "BLOCKED"
    assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# 16. Planner returns REPLAN → apply_revision called, version increments
# ---------------------------------------------------------------------------

def test_replan_triggers_workflow_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    extra_step_added = {"done": False}

    def make_revised_steps(workflow):
        # Return one new pending step
        extra_step_added["done"] = True
        return [
            WorkflowStep(
                step_id="s_extra",
                step_type="FAKE",
                status=StepStatus.PENDING,
                depends_on=[],
                condition=None,
                retry_policy=RetryPolicy(),
                planning_rationale="added by replan",
            )
        ]

    planner = _RecordingPlanner(
        decisions=[ReplanDecision(action="REPLAN", reason="low_confidence", rationale="need more data")],
        replan_fn=make_revised_steps,
    )
    wf = _workflow([_step("s1")])
    wf.save()

    engine = _make_engine(planner=planner, registry={"FAKE": lambda wf, st, step: {"confidence": 0.3}})
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "COMPLETED"
    assert extra_step_added["done"] is True

    reloaded_wf = WorkflowDefinition.load("wf-test", "prog-test")
    assert reloaded_wf.version == 2
    assert reloaded_wf.replanning_count == 1

    final_state = ExecutionState.load("wf-test", "prog-test")
    assert "s_extra" in final_state.completed_steps


# ---------------------------------------------------------------------------
# 17. Planner returns REPLAN 4× → first 3 honoured, 4th downgraded to CONTINUE
# ---------------------------------------------------------------------------

def test_replan_limit_three_honoured_fourth_downgraded(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    step_counter = {"n": 0}

    def make_extra_step(workflow):
        step_counter["n"] += 1
        sid = f"s_extra_{step_counter['n']}"
        return [
            WorkflowStep(
                step_id=sid,
                step_type="FAKE",
                status=StepStatus.PENDING,
                depends_on=[],
                condition=None,
                retry_policy=RetryPolicy(),
                planning_rationale="replan step",
            )
        ]

    # 4 REPLAN decisions
    planner = _RecordingPlanner(
        decisions=[ReplanDecision(action="REPLAN", reason="signals", rationale="r")] * 4,
        replan_fn=make_extra_step,
    )
    wf = _workflow([_step("s1")])
    wf.save()

    engine = _make_engine(planner=planner, registry={"FAKE": lambda wf, st, step: {"success": True}})
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "COMPLETED"
    reloaded_wf = WorkflowDefinition.load("wf-test", "prog-test")
    # Exactly 3 replans honoured, 4th downgraded to CONTINUE
    assert reloaded_wf.replanning_count == 3, (
        f"Expected replanning_count=3, got {reloaded_wf.replanning_count}"
    )


# ---------------------------------------------------------------------------
# 18. ESCALATE_HUMAN → status=BLOCKED, outcome reason=human_review_required
# ---------------------------------------------------------------------------

def test_escalate_human_sets_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    planner = _RecordingPlanner(
        decisions=[ReplanDecision(action="ESCALATE_HUMAN", reason="needs_review")]
    )
    wf = _workflow([_step("s1"), _step("s2")])
    wf.save()

    engine = _make_engine(planner=planner)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "BLOCKED"
    assert outcome.outcome["reason"] == "human_review_required"
    assert outcome.outcome["step_id"] == "s1"

    state = ExecutionState.load("wf-test", "prog-test")
    assert state.status == ExecutionStatus.BLOCKED
    # s2 was not executed
    assert "s2" not in state.completed_steps


# ---------------------------------------------------------------------------
# 19. max_steps=2 with 5-step workflow → BLOCKED, reason=max_steps_reached
# ---------------------------------------------------------------------------

def test_max_steps_ceiling_blocks_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    wf = _workflow([_step(f"s{i}") for i in range(1, 6)])
    wf.save()

    engine = _make_engine()
    outcome = engine.execute_workflow("wf-test", "prog-test", max_steps=2)

    assert outcome.status == "BLOCKED"
    assert outcome.outcome["reason"] == "max_steps_reached"

    state = ExecutionState.load("wf-test", "prog-test")
    assert len(state.completed_steps) == 2


# ---------------------------------------------------------------------------
# 20. Step depends on FAILED step → deadlock → BLOCKED
# ---------------------------------------------------------------------------

def test_deadlock_on_failed_dependency(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def fail_s1(wf, st, step):
        raise StepRetryable("always fails")

    registry = {
        "FAIL": fail_s1,
        "FAKE": lambda wf, st, step: {"success": True},
    }
    wf = _workflow([
        _step("s1", step_type="FAIL", max_attempts=1),
        _step("s2", step_type="FAKE", depends_on=["s1"]),  # s2 blocked by s1 failure
    ])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    assert outcome.status == "BLOCKED"
    state = ExecutionState.load("wf-test", "prog-test")
    assert "s1" in state.failed_steps
    assert "s2" not in state.completed_steps
    assert "s2" not in state.failed_steps


# ---------------------------------------------------------------------------
# 21. StepRegistryMiss → step recorded as failed, next runnable step continues
# ---------------------------------------------------------------------------

def test_registry_miss_records_failure_workflow_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    registry = {
        # "UNKNOWN_TYPE" is NOT registered — will trigger StepRegistryMiss
        "FAKE": lambda wf, st, step: {"success": True},
    }
    wf = _workflow([
        _step("s1", step_type="UNKNOWN_TYPE"),
        _step("s2", step_type="FAKE"),  # no dep on s1
    ])
    wf.save()

    engine = _make_engine(registry=registry)
    outcome = engine.execute_workflow("wf-test", "prog-test")

    state = ExecutionState.load("wf-test", "prog-test")
    assert "s1" in state.failed_steps
    # Workflow is not aborted — s2 can still run since it has no dep on s1
    assert "s2" in state.completed_steps


# ---------------------------------------------------------------------------
# 22. execute_workflow stamps state.programme_id from its argument  [S16 amendment]
# ---------------------------------------------------------------------------

def test_engine_stamps_programme_id_on_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    wf = _workflow([_step("s1")])
    wf.save()

    engine = _make_engine()
    engine.execute_workflow("wf-test", "prog-test")

    state = ExecutionState.load("wf-test", "prog-test")
    assert state.programme_id == "prog-test"
