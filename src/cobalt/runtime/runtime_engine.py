"""Runtime layer — RuntimeEngine: deterministic execution loop with crash recovery."""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from cobalt.core.exceptions import StepRegistryMiss
from cobalt.runtime.execution_state import ExecutionState, ExecutionStatus
from cobalt.runtime.workflow_definition import (
    StepStatus,
    WorkflowDefinition,
    WorkflowStep,
    evaluate_condition,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step-level exceptions (execution-flow signals, not core errors)
# ---------------------------------------------------------------------------

class StepRetryable(Exception):
    """Raised by a step callable for transient failures (network, rate limit).
    The engine will retry up to retry_policy.max_attempts times.
    """


class StepFatal(Exception):
    """Raised by a step callable for permanent failures (invalid input, missing
    dependency). The engine records the failure and aborts the workflow run.
    """


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ReplanDecision:
    action:    str        # CONTINUE / REPLAN / ESCALATE_HUMAN / TERMINATE
    reason:    str
    rationale: str = ""


@dataclass
class WorkflowOutcome:
    workflow_id:    str
    status:         str        # mirrors ExecutionState.status.value at end
    final_step_id:  str | None
    outcome:        dict | None


# ---------------------------------------------------------------------------
# Protocols — allow injection of fake planners and renderers in tests
# ---------------------------------------------------------------------------

class PlannerProtocol(Protocol):
    def evaluate_step(
        self,
        step: WorkflowStep,
        result: dict,
        state: ExecutionState,
    ) -> ReplanDecision: ...

    def replan(
        self,
        workflow: WorkflowDefinition,
        completed_step: WorkflowStep,
        step_result: dict,
        accumulated_signals: dict,
    ) -> list[WorkflowStep]: ...


class PlanRendererProtocol(Protocol):
    def render(self, workflow: WorkflowDefinition, state: ExecutionState) -> None: ...


# ---------------------------------------------------------------------------
# StepCallable type alias
# ---------------------------------------------------------------------------

StepCallable = Callable[[WorkflowDefinition, ExecutionState, WorkflowStep], dict]


# ---------------------------------------------------------------------------
# RuntimeEngine
# ---------------------------------------------------------------------------

class RuntimeEngine:

    def __init__(
        self,
        planner: PlannerProtocol,
        step_registry: dict[str, StepCallable],
        plan_renderer: PlanRendererProtocol | None = None,
    ) -> None:
        self.planner = planner
        self.step_registry = step_registry
        self.plan_renderer = plan_renderer

    # ------------------------------------------------------------------
    # Public entry point — never raises
    # ------------------------------------------------------------------

    def execute_workflow(
        self,
        workflow_id: str,
        programme_id: str,
        max_steps: int = 50,
    ) -> WorkflowOutcome:
        """Load workflow.json + state.json and execute until terminal or max_steps."""
        try:
            workflow = WorkflowDefinition.load(workflow_id, programme_id)
            state = ExecutionState.load(workflow_id, programme_id)
        except Exception as exc:
            logger.error("Failed to load workflow %s/%s: %s", programme_id, workflow_id, exc)
            return WorkflowOutcome(
                workflow_id=workflow_id,
                status="FAILED",
                final_step_id=None,
                outcome={"reason": f"load_error: {exc!r}"},
            )

        # Ensure programme_id is always persisted on state so evaluate_step
        # can load the workflow without needing it as a separate parameter.
        state.programme_id = programme_id

        # Already in a terminal state — nothing to do.
        if state.status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.BLOCKED,
            ExecutionStatus.FAILED,
        ):
            return self._make_outcome(workflow_id, state)

        # Mark as running.
        state.status = ExecutionStatus.IN_PROGRESS
        state.save()
        self._render(workflow, state)

        steps_started = 0
        last_result: dict = {}
        last_step: WorkflowStep | None = None

        while True:
            next_step = self._next_runnable_step(workflow, state)

            if next_step is None:
                break

            # Condition gate — skipped steps don't count toward max_steps.
            if next_step.condition is not None:
                if not self._evaluate_condition(next_step.condition, state):
                    state.record_step_skip(next_step.step_id, "condition not met")
                    self._update_step_status(workflow, next_step.step_id, StepStatus.SKIPPED)
                    self._render(workflow, state)
                    continue

            # Hard ceiling on steps that actually execute.
            if steps_started >= max_steps:
                state.status = ExecutionStatus.BLOCKED
                state.outcome = {"reason": "max_steps_reached"}
                break

            steps_started += 1
            state.record_step_start(next_step.step_id)
            self._render(workflow, state)

            # ----- retry loop -----
            attempts_so_far = 0
            step_completed = False

            while True:
                attempts_so_far += 1
                try:
                    last_result = self._execute_step(next_step, workflow, state)
                    state.record_step_complete(
                        next_step.step_id, last_result, attempts=attempts_so_far
                    )
                    state.save()
                    self._update_step_status(workflow, next_step.step_id, StepStatus.DONE)
                    step_completed = True
                    break

                except StepRegistryMiss as exc:
                    # Configuration bug — fail this step, continue to next runnable step.
                    # record_step_failure sets state.status=FAILED; reset to IN_PROGRESS
                    # so the workflow is not aborted (only this step is failed).
                    logger.error("StepRegistryMiss for %s: %s", next_step.step_id, exc)
                    state.record_step_failure(
                        next_step.step_id, str(exc), attempts=attempts_so_far
                    )
                    state.status = ExecutionStatus.IN_PROGRESS
                    state.save()
                    self._update_step_status(workflow, next_step.step_id, StepStatus.FAILED)
                    break

                except StepRetryable as exc:
                    if attempts_so_far < next_step.retry_policy.max_attempts:
                        logger.info(
                            "Step %s retryable (attempt %d/%d): %s",
                            next_step.step_id, attempts_so_far,
                            next_step.retry_policy.max_attempts, exc,
                        )
                        time.sleep(next_step.retry_policy.backoff_seconds)
                        continue
                    else:
                        logger.warning(
                            "Step %s failed after %d attempts: %s",
                            next_step.step_id, attempts_so_far, exc,
                        )
                        # record_step_failure sets state.status=FAILED; reset to IN_PROGRESS
                        # — exhausted retries on a single step do not abort the workflow.
                        state.record_step_failure(
                            next_step.step_id, str(exc), attempts=attempts_so_far
                        )
                        state.status = ExecutionStatus.IN_PROGRESS
                        state.save()
                        self._update_step_status(workflow, next_step.step_id, StepStatus.FAILED)
                        break

                except StepFatal as exc:
                    logger.error("StepFatal on %s: %s", next_step.step_id, exc)
                    state.record_step_failure(
                        next_step.step_id, str(exc), attempts=attempts_so_far
                    )
                    state.status = ExecutionStatus.FAILED
                    state.save()
                    self._update_step_status(workflow, next_step.step_id, StepStatus.FAILED)
                    self._render(workflow, state)
                    return self._make_outcome(workflow_id, state)

                except Exception as exc:
                    # One bare `except Exception` in the engine — required here to guarantee
                    # the engine never crashes mid-run regardless of step implementation bugs.
                    logger.error(
                        "Unexpected error in step %s: %r", next_step.step_id, exc
                    )
                    state.record_step_failure(
                        next_step.step_id,
                        f"unexpected: {exc!r}",
                        attempts=attempts_so_far,
                    )
                    state.status = ExecutionStatus.FAILED
                    state.save()
                    self._update_step_status(workflow, next_step.step_id, StepStatus.FAILED)
                    self._render(workflow, state)
                    return self._make_outcome(workflow_id, state)

            # ----- post-step: render, then ask planner -----
            self._render(workflow, state)

            if not step_completed:
                # Step failed (retryable exhausted or registry miss) — move to next.
                last_step = next_step
                continue

            last_step = next_step

            # Planner evaluation.
            try:
                decision = self.planner.evaluate_step(next_step, last_result, state)
            except Exception as exc:
                logger.warning("planner.evaluate_step raised %r — defaulting to CONTINUE", exc)
                decision = ReplanDecision(action="CONTINUE", reason="evaluate_step_error")

            if decision.action == "REPLAN":
                if workflow.replanning_count < 3:
                    try:
                        revised = self.planner.replan(
                            workflow,
                            next_step,
                            last_result,
                            state.accumulated_signals,
                        )
                        workflow.apply_revision(
                            revised,
                            triggered_by=next_step.step_id,
                            trigger_reason=decision.reason,
                            rationale=decision.rationale,
                        )
                        workflow.save()
                        self._render(workflow, state)
                        logger.info(
                            "Workflow %s revised to version %d", workflow_id, workflow.version
                        )
                    except Exception as exc:
                        logger.warning(
                            "apply_revision failed for %s: %r — continuing", workflow_id, exc
                        )
                else:
                    logger.warning(
                        "Workflow %s has reached replanning limit (3) — "
                        "downgrading REPLAN to CONTINUE",
                        workflow_id,
                    )

            elif decision.action == "ESCALATE_HUMAN":
                state.status = ExecutionStatus.BLOCKED
                state.outcome = {
                    "reason": "human_review_required",
                    "step_id": next_step.step_id,
                }
                state.save()
                self._render(workflow, state)
                return self._make_outcome(workflow_id, state)

            elif decision.action == "TERMINATE":
                state.status = ExecutionStatus.COMPLETED
                state.outcome = {
                    "reason": "early_termination",
                    "step_id": next_step.step_id,
                }
                state.save()
                self._render(workflow, state)
                return self._make_outcome(workflow_id, state)

        # ----- loop exited — finalise status -----
        if state.status == ExecutionStatus.IN_PROGRESS:
            done_ids   = set(state.completed_steps.keys())
            failed_ids = set(state.failed_steps.keys())
            skipped_ids = set(state.skipped_steps.keys())
            executed = done_ids | failed_ids | skipped_ids

            remaining = [
                s for s in workflow.steps
                if s.status == StepStatus.PENDING and s.step_id not in executed
            ]
            if not remaining:
                state.status = ExecutionStatus.COMPLETED
            else:
                state.status = ExecutionStatus.BLOCKED
                if state.outcome is None:
                    state.outcome = {"reason": "deadlock_detected"}

        state.save()
        self._render(workflow, state)
        return self._make_outcome(workflow_id, state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_runnable_step(
        self,
        workflow: WorkflowDefinition,
        state: ExecutionState,
    ) -> WorkflowStep | None:
        """Return first PENDING step whose every dependency is DONE in state."""
        done_ids    = set(state.completed_steps.keys())
        failed_ids  = set(state.failed_steps.keys())
        skipped_ids = set(state.skipped_steps.keys())
        already_done = done_ids | failed_ids | skipped_ids

        for step in workflow.steps:
            if step.status != StepStatus.PENDING:
                continue
            if step.step_id in already_done:
                continue
            if all(dep in done_ids for dep in step.depends_on):
                return step
        return None

    def _execute_step(
        self,
        step: WorkflowStep,
        workflow: WorkflowDefinition,
        state: ExecutionState,
    ) -> dict:
        """Look up and invoke the step callable. Raises StepRegistryMiss if unknown."""
        fn = self.step_registry.get(step.step_type)
        if fn is None:
            raise StepRegistryMiss(
                f"No callable registered for step_type {step.step_type!r}"
            )
        return fn(workflow, state, step)

    def _evaluate_condition(self, condition: str, state: ExecutionState) -> bool:
        """Evaluate a step condition expression against completed step results."""
        step_results = {
            step_id: record.result
            for step_id, record in state.completed_steps.items()
        }
        try:
            return evaluate_condition(condition, step_results)
        except Exception as exc:
            logger.warning("Condition evaluation error (%r): %s — treating as False", condition, exc)
            return False

    def _make_outcome(self, workflow_id: str, state: ExecutionState) -> WorkflowOutcome:
        return WorkflowOutcome(
            workflow_id=workflow_id,
            status=state.status.value,
            final_step_id=state.current_step_id,
            outcome=state.outcome,
        )

    def _render(self, workflow: WorkflowDefinition, state: ExecutionState) -> None:
        if self.plan_renderer is not None:
            try:
                self.plan_renderer.render(workflow, state)
            except Exception as exc:
                logger.warning("plan_renderer.render failed: %s", exc)

    @staticmethod
    def _update_step_status(
        workflow: WorkflowDefinition,
        step_id: str,
        status: StepStatus,
    ) -> None:
        """Update a step's status in-memory so apply_revision sees correct terminal states."""
        for step in workflow.steps:
            if step.step_id == step_id:
                step.status = status
                return
