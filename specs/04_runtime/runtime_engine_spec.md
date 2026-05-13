# runtime_engine

## Overview

**Layer:** Runtime
**File:** `src/cobalt/runtime/runtime_engine.py`
**Role:** Deterministic execution loop. Reads `WorkflowDefinition`, executes steps, updates `ExecutionState` atomically, triggers replanning when needed.
**Calls:** Planning Agent (evaluate_step, replan), STEP_REGISTRY for step execution
**Calls into:** PlanRenderer after every state change

---

## Purpose

`RuntimeEngine` is the bounded, deterministic execution runtime.

It does **not** make planning decisions. It executes the plan, observes results, and asks the Planning Agent whether to revise. All intelligence lives in Planning Agent. All state lives on disk.

---

## Public API

```python
class RuntimeEngine:

    def __init__(
        self,
        planning_agent: PlanningAgent,
        step_registry: dict[str, Callable],
        plan_renderer: PlanRenderer,
    ):
        ...

    def execute_workflow(
        self,
        workflow_id: str,
        programme_id: str,
        max_steps: int = 50,
    ) -> WorkflowOutcome:
        """
        Load workflow.json + state.json from disk.
        Execute pending steps until COMPLETED, BLOCKED, FAILED, or max_steps hit.
        Returns WorkflowOutcome.
        Never raises — all errors recorded in state.
        """
```

---

## The Execution Loop

```
1. Load WorkflowDefinition from workflow.json
2. Load ExecutionState from state.json (or NOT_STARTED if missing)

3. If state.status in (COMPLETED, BLOCKED, FAILED with no retries):
     return WorkflowOutcome(state)

4. Set state.status = IN_PROGRESS
5. Persist state.json

6. Loop (max max_steps iterations):

   a. next_step = self._next_runnable_step(workflow, state)
      If next_step is None: break  (all steps complete or blocked)

   b. If step has condition:
        If self._evaluate_condition(next_step.condition, state) is False:
          state.record_step_skip(next_step.step_id, reason='condition not met')
          continue

   c. state.record_step_start(next_step.step_id)
      state.save()

   d. Execute step:
        try:
          result = self._execute_step(next_step, workflow, state)
          state.record_step_complete(next_step.step_id, result)
        except StepRetryable as exc:
          attempts_so_far = state.failed_steps.get(step_id, {}).get('attempts', 0) + 1
          if attempts_so_far < next_step.retry_policy.max_attempts:
            time.sleep(next_step.retry_policy.backoff_seconds)
            retry from step c (without persisting failure yet)
          else:
            state.record_step_failure(step_id, str(exc), attempts_so_far)
        except StepFatal as exc:
          state.record_step_failure(step_id, str(exc), attempts=1)
          state.status = FAILED
          break

   e. state.save()
      plan_renderer.render(workflow, state)

   f. Planning Agent evaluation (CHEAP, no LLM):
        decision = planning_agent.evaluate_step(next_step, result, state)
        # Returns: CONTINUE / REPLAN / ESCALATE_HUMAN / TERMINATE

   g. If decision == REPLAN and workflow.replanning_count < 3:
        revised_steps = planning_agent.replan(
            workflow=workflow,
            completed_step=next_step,
            step_result=result,
            accumulated_signals=state.accumulated_signals,
        )
        workflow.apply_revision(
            revised_steps,
            triggered_by=next_step.step_id,
            trigger_reason=decision.reason,
            rationale=decision.rationale,
        )
        workflow.save()
        plan_renderer.render(workflow, state)

   h. If decision == TERMINATE:
        state.status = COMPLETED
        break

   i. If decision == ESCALATE_HUMAN:
        state.status = BLOCKED
        state.outcome = {'reason': 'human_review_required', ...}
        break

7. If all steps done: state.status = COMPLETED
8. state.save()
9. plan_renderer.render(workflow, state)
10. return WorkflowOutcome(state)
```

---

## Step dependency resolution

```python
def _next_runnable_step(
    self,
    workflow: WorkflowDefinition,
    state: ExecutionState,
) -> WorkflowStep | None:
    """
    Find the next step where:
      - status == PENDING
      - all depends_on step_ids are in state.completed_steps with status=DONE

    Returns None if no such step exists.
    Steps with dependencies on FAILED steps will never become runnable (deadlock detection: return None).
    """
```

---

## Step execution

```python
def _execute_step(
    self,
    step: WorkflowStep,
    workflow: WorkflowDefinition,
    state: ExecutionState,
) -> dict:
    """
    Lookup step.step_type in self.step_registry.
    Call the registered callable with (workflow, state, step).
    The callable returns a dict of result data.

    Result data conventions:
      - Always include 'confidence' (float 0.0-1.0) when meaningful
      - Always include 'success' (bool)
      - Include named signals: 'fraud_signals' (list), 'matched' (bool), etc.

    Raises:
      StepRetryable on transient failures (network, rate limit)
      StepFatal on permanent failures (invalid input, missing dependency)
    """
```

---

## Crash recovery contract

- Workflow A has 7 steps
- Crash after step 3 completes
- Restart with same workflow_id

```
Load state.json:
  completed_steps = {s1: DONE, s2: DONE, s3: DONE}
  pending_steps = [s4, s5, s6, s7]
  status = IN_PROGRESS  (was being executed when crash happened)

Engine logic:
  s4.depends_on = [s3]  — s3 is DONE — runnable
  Execute s4...
  Continue until completion.

Steps s1, s2, s3 NEVER re-execute.
```

If crash happened during step 3 (status=RUNNING on disk):

```
Load state.json:
  completed_steps = {s1: DONE, s2: DONE}
  current_step_id = s3   (was running)
  pending_steps = [s4, s5, s6, s7]
  # s3 not in completed_steps (crashed before save)

Engine logic:
  s3 is still PENDING from workflow.json perspective.
  Execute s3 fresh.

Step idempotency is responsibility of the step implementation.
For non-idempotent steps (e.g. POST to external API), the step
should store a side-effect token and check on retry.
```

---

## Bounded replanning

- Each workflow has `replanning_count` in workflow.json
- Hard cap: max 3 replans per workflow
- After 3 replans: `evaluate_step` decisions REPLAN are downgraded to CONTINUE
- Replanning frequency is logged for observability

---

## STEP_REGISTRY contract

Steps are registered with a uniform signature:

```python
StepCallable = Callable[
    [WorkflowDefinition, ExecutionState, WorkflowStep],
    dict
]
```

Existing Process 1 steps (from `src/cobalt/intake/steps/`) are wrapped to match this signature in `runtime_engine.py` so the same registry can drive both legacy intake steps and new V2 workflows.

---

## Tests required

- 3-step workflow runs to completion
- Crash at step 2 (state.json has s1=DONE) → restart resumes at step 2
- Step with condition that evaluates False → SKIPPED, next step runs
- Step with `depends_on=[s3]` waits until s3 is DONE
- Retry policy: step fails twice with StepRetryable, succeeds on third attempt
- Retry exhausted: StepFatal recorded, workflow.status=FAILED
- Replanning trigger: evaluate_step returns REPLAN → replan() called → workflow revised
- Max 3 replans: 4th REPLAN decision treated as CONTINUE
- ESCALATE_HUMAN: workflow.status=BLOCKED
- Deadlock: step depends on FAILED step → step skipped, eventually no runnable step → workflow ends
