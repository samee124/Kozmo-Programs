# Planning Agent

## Overview

**File:** `src/cobalt/agents/planning_agent.py`
**Role:** Writes every plan in the system. Creates and revises workflow definitions. Rules engine lives inside this class — Mode 1 (rules) for clear cases, Mode 2 (LLM) for conflicting signals.

---

## Two Eras of Planning

### Era 1: Process 1 (existing)

Per-candidate investigation plans written as .md files.

| Method | Purpose |
|---|---|
| `compute_investigation_plan(profile)` | Build InvestigationPlan from SignalProfile |
| `compute_programme_plan(source_summary)` | Build programme strategy |
| `compute_campaign_plan(input)` | Build CampaignPlan from gaps |
| `compute_negotiation_plan(input)` | V1 shell |
| `write_programme_plan(programme_id, ctx)` | Write programme_plan.md |
| `write_investigation_plan(programme_id, key, profile)` | Write IP-*.md |

### Era 2: V2 Runtime (new)

Workflow definitions written as workflow.json. Adaptive replanning.

| Method | Purpose |
|---|---|
| `create_workflow(profile, workflow_type, programme_id)` | Build WorkflowDefinition |
| `evaluate_step(step, result, state)` | Decide replanning need (fast, no LLM) |
| `replan(workflow, completed_step, result, signals)` | Revise remaining steps (one LLM call) |

---

## create_workflow

```python
def create_workflow(
    self,
    profile: SignalProfile,
    workflow_type: str,            # "INTAKE_INVESTIGATION" / "ENRICHMENT" / etc.
    programme_id: str,
    context_overrides: dict | None = None,
) -> WorkflowDefinition:
    """
    Produce a complete WorkflowDefinition.

    Internally:
    1. Run rules engine to get base step list
    2. For INTAKE_INVESTIGATION: reuse compute_investigation_plan() rules R01-R14
    3. For ENRICHMENT: standard 4-step pipeline (COLLECT_SOURCES, EXTRACT_ATTRIBUTES,
       MAP_RELATIONSHIPS, CREATE_PROFILE)
    4. Wrap each step in WorkflowStep with: step_id, step_type, depends_on, condition,
       retry_policy, planning_rationale
    5. Build context dict with all signals and rationale
    6. Save workflow.json via WorkflowDefinition.save()
    """
```

---

## evaluate_step

```python
def evaluate_step(
    self,
    step: WorkflowStep,
    result: dict,
    state: ExecutionState,
) -> ReplanDecision:
    """
    Fast heuristic check. NO LLM call.
    Returns ReplanDecision with action:
      CONTINUE        — proceed to next step
      REPLAN          — Planning Agent should revise remaining steps
      ESCALATE_HUMAN  — workflow needs human intervention
      TERMINATE       — workflow is done (early success)
    """
```

### Replan trigger rules

```python
def evaluate_step(self, step, result, state) -> ReplanDecision:
    # Early exit (BLOCKED step) — always terminates
    if result.get("early_exit"):
        if result.get("exit_status") == "BLOCKED":
            return ReplanDecision(action="TERMINATE", reason="step_blocked")
        if result.get("exit_status") == "TRIAGE_REQUIRED":
            return ReplanDecision(action="ESCALATE_HUMAN", reason="triage_required")

    # Confidence collapse — web research came back weak
    if result.get("confidence", 1.0) < 0.35:
        return ReplanDecision(
            action="REPLAN",
            reason="low_confidence",
            rationale=f"Step {step.step_id} returned confidence {result['confidence']}, below 0.35 threshold",
        )

    # New fraud signals appeared that were not in original plan
    new_fraud = result.get("fraud_signals", [])
    known_fraud = state.accumulated_signals.get("known_fraud_signals", [])
    if len(new_fraud) > len(known_fraud):
        return ReplanDecision(
            action="REPLAN",
            reason="new_fraud_signals",
            rationale=f"Step {step.step_id} discovered new fraud signals: {set(new_fraud) - set(known_fraud)}",
        )

    # Multiple entity matches — ambiguity not anticipated
    if result.get("multiple_matches"):
        return ReplanDecision(
            action="REPLAN",
            reason="entity_ambiguity",
            rationale="Multiple entities match — disambiguation step needed",
        )

    # Replan count guard — never replan more than 3 times
    if state.workflow.replanning_count >= 3:
        return ReplanDecision(action="CONTINUE", reason="replan_limit_reached")

    return ReplanDecision(action="CONTINUE", reason="step_ok")
```

---

## replan

```python
def replan(
    self,
    workflow: WorkflowDefinition,
    completed_step: WorkflowStep,
    step_result: dict,
    accumulated_signals: dict,
) -> list[WorkflowStep]:
    """
    ONE LLM call. Produces revised remaining steps.

    Prompt structure:
      - Original workflow context and rationale
      - All completed steps with their results
      - The step that triggered replanning + its result
      - Accumulated signals
      - List of available step types (from STEP_REGISTRY)
      - Constraint: only modify pending steps, never completed ones

    LLM output: JSON with list of new step definitions (step_id, step_type, depends_on,
                condition, retry_policy, planning_rationale)

    Returns: list of WorkflowStep objects ready for workflow.apply_revision()
    """
```

Fallback if LLM fails:
- Append a `ROUTE_TO_HUMAN` step
- Set its planning_rationale to "Replanning LLM call failed, escalating for review"
- Continue execution

---

## Rules engine (existing — keep for Process 1)

The 14 rules R01-R14 from Process 1 stay. `compute_investigation_plan()` is unchanged.

For V2 `create_workflow()` with type INTAKE_INVESTIGATION, internally call `compute_investigation_plan()` then wrap the result in WorkflowDefinition format.

---

## ReplanDecision dataclass

```python
@dataclass
class ReplanDecision:
    action:     str        # CONTINUE / REPLAN / ESCALATE_HUMAN / TERMINATE
    reason:     str        # short tag: low_confidence / new_fraud_signals / etc.
    rationale:  str = ""   # human-readable explanation
```

---

## Tests required

- `create_workflow(profile, "INTAKE_INVESTIGATION")` → valid WorkflowDefinition
- `create_workflow(profile, "ENRICHMENT")` → 4-step workflow
- `evaluate_step` returns REPLAN when confidence < 0.35
- `evaluate_step` returns REPLAN when new fraud signals appear
- `evaluate_step` returns CONTINUE when replanning_count >= 3
- `evaluate_step` returns ESCALATE_HUMAN when exit_status == "TRIAGE_REQUIRED"
- `replan` LLM mock returns valid revised steps
- `replan` LLM failure → fallback ROUTE_TO_HUMAN step
- Existing Process 1 methods unchanged and tests still pass
