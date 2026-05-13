# plan_renderer

## Overview

**Layer:** Runtime
**File:** `src/cobalt/runtime/plan_renderer.py`
**Role:** Derive human-readable `plan.md` from `workflow.json` + `state.json`. View only — never source of truth.

---

## Purpose

`PlanRenderer` converts the executable workflow and execution state into a markdown document for human review. The .md is:

- **Read-only for execution** — the RuntimeEngine never opens it
- **Deletable** — safe to delete; can always be regenerated
- **Updated after every state change** — every step completion, every replan
- **A view, not a source of truth**

---

## Public API

```python
class PlanRenderer:

    def __init__(self, workspace_root: Path | None = None):
        ...

    def render(
        self,
        workflow: WorkflowDefinition,
        state: ExecutionState,
    ) -> Path:
        """
        Generate plan.md content from workflow + state.
        Write to workspace/{programme_id}/workflows/{wf_id}/plan.md via atomic_write.
        Returns the path written.
        """

    def render_to_string(
        self,
        workflow: WorkflowDefinition,
        state: ExecutionState,
    ) -> str:
        """Generate plan.md content without writing."""
```

---

## Rendered Output

```markdown
# Workflow — {workflow_id}
_{workflow_type} | {programme_id} | version {N} | created {timestamp}_

## Context

{key facts from workflow.context — vendor, signals, etc.}

## Planning Rationale

{workflow.context.planning_rationale}

## Steps

- [x] `s1` `WEB_RESEARCH_DEEP` — confidence=0.88 ✓
  > Confirm vendor identity before fraud assessment
- [ ] `s2` `DOCUMENT_EXTRACTION` — waiting (depends on s1: done, condition met)
  > Extract renewal date and contract value from ELA
- [ ] `s3` `FRAUD_CHECK_BASIC` — pending
  > Standard fraud check for high-spend unknown vendor

## Execution Log

| Step | Status | Started | Completed | Attempts | Summary |
|---|---|---|---|---|---|
| s1 `WEB_RESEARCH_DEEP` | DONE | 10:01:00Z | 10:01:45Z | 1 | Vendor confirmed, confidence 0.88 |
| s2 `DOCUMENT_EXTRACTION` | PENDING | — | — | 0 | — |
| s3 `FRAUD_CHECK_BASIC` | PENDING | — | — | 0 | — |

## Accumulated Signals

- `fraud_risk_level`: LOW
- `entity_confirmed`: true
- `web_confidence`: 0.88

## Replanning History

_No replans yet._

## Outcome

**Status:** IN_PROGRESS
**Current step:** s2
```

When the workflow has been replanned:

```markdown
## Replanning History

### Version 2 — replanned at 2026-05-12T10:02:15Z

**Triggered by:** s1
**Reason:** low_confidence
**Rationale:** Web research returned multiple matches. Adding REGISTRY_LOOKUP before fraud check to disambiguate.

- Added: s2 `REGISTRY_LOOKUP`
- Added: s4 `ROUTE_TO_HUMAN` (conditional)
- Modified: s3 `FRAUD_CHECK_BASIC` now depends on s2
```

---

## Rendering rules

1. **Step status icons:** ☐ PENDING, ▶ RUNNING, ☑ DONE, ✗ FAILED, ⊘ SKIPPED
2. **Step rationale** rendered as `>` blockquote below the step line
3. **Step depends_on** shown next to status: `(depends on s1: done)`
4. **Step condition** shown when present: `(condition: s1.confidence >= 0.4 — met)`
5. **Execution log** is one row per step (ordered by step_id)
6. **Outcome** section shows final status when COMPLETED/FAILED/BLOCKED, else `IN_PROGRESS` with current step

---

## Atomic write

Uses `atomic_write()` from `src/cobalt/core/atomic_write.py`. Path:
`workspace/{programme_id}/workflows/{workflow_id}/plan.md`

---

## When render() is called

By RuntimeEngine, after every state change:

- After every step completion
- After every step skip
- After every step failure
- After every replanning event
- At workflow start (initial render)
- At workflow end (final render)

For high-frequency execution, render() can be debounced — but the spec is "called after every state change" by default. Performance optimisation comes later.

---

## Tests required

- render() produces correct markdown given a workflow + state
- DONE steps show with check mark
- PENDING steps show with empty box
- Replanning history section shows all ReplanEvents in order
- Steps without rationale don't break the renderer
- Empty workflow renders correctly
- render() writes plan.md via atomic_write
- render_to_string() returns string without writing
