# workflow_definition

## Overview

**Layer:** Runtime
**File:** `src/cobalt/runtime/workflow_definition.py`
**Role:** Executable persistent plan. The single source of truth for what should happen during a workflow run.
**Format on disk:** `workflow.json` inside `workspace/{programme_id}/workflows/{wf_id}/`

---

## Purpose

`WorkflowDefinition` is the durable, executable plan that drives all V2+ execution.

- Created by Planning Agent via `create_workflow()`
- Revised by Planning Agent via `replan()` when adaptive intelligence triggers
- Read by RuntimeEngine on every step
- Persisted atomically on every revision
- **Never** read or written by `.md` files

The dataclass is a deserialised view of `workflow.json`. The JSON file is the truth. On crash, reload from disk.

---

## Dataclasses

### WorkflowStep

```python
@dataclass
class WorkflowStep:
    step_id:              str                   # "s1", "s2", "s3"
    step_type:            str                   # "WEB_RESEARCH_DEEP", "FRAUD_CHECK_BASIC", etc.
    status:               StepStatus            # PENDING / RUNNING / DONE / FAILED / SKIPPED
    depends_on:           list[str]             # step_ids this depends on
    condition:            str | None            # "s1.confidence >= 0.4" or None
    retry_policy:         RetryPolicy           # max_attempts, backoff_seconds
    planning_rationale:   str                   # why this step is in the plan
    added_in_version:     int = 1               # workflow version when step was added
```

### StepStatus (enum)

```python
class StepStatus(str, Enum):
    PENDING  = "PENDING"
    RUNNING  = "RUNNING"
    DONE     = "DONE"
    FAILED   = "FAILED"
    SKIPPED  = "SKIPPED"
```

### RetryPolicy

```python
@dataclass
class RetryPolicy:
    max_attempts:     int = 1
    backoff_seconds:  int = 0
```

### ReplanEvent

```python
@dataclass
class ReplanEvent:
    version:         int
    triggered_by:    str                  # step_id that caused replan
    trigger_reason:  str                  # "low_confidence" / "fraud_signals" / etc.
    rationale:       str                  # LLM explanation
    steps_added:     list[str]
    steps_removed:   list[str]
    steps_modified:  list[str]
    replanned_at:    str                  # ISO timestamp
```

### WorkflowDefinition

```python
@dataclass
class WorkflowDefinition:
    workflow_id:           str
    programme_id:          str
    vendor_key:            str | None
    vendor_id:             str | None
    workflow_type:         str                # "INTAKE_INVESTIGATION" / "ENRICHMENT" / etc.
    created_by:            str                # "planning_agent"
    created_at:            str
    version:               int = 1
    replanning_count:      int = 0
    context:               dict = field(default_factory=dict)
    steps:                 list[WorkflowStep] = field(default_factory=list)
    replanning_history:    list[ReplanEvent] = field(default_factory=list)
```

---

## Persistence

### Save

```python
def save(self, path: Path | None = None) -> Path:
    """Write to workflow.json atomically.

    Default path: workspace/{programme_id}/workflows/{workflow_id}/workflow.json
    """
```

Uses `atomic_write()` from `src/cobalt/core/atomic_write.py`.

### Load

```python
@classmethod
def load(cls, workflow_id: str, programme_id: str) -> WorkflowDefinition:
    """Read workflow.json and reconstruct dataclass.

    Raises:
        FileNotFoundError if workflow.json does not exist
        WorkflowParseError if JSON is malformed or schema mismatch
    """
```

### apply_revision

```python
def apply_revision(
    self,
    revised_steps: list[WorkflowStep],
    triggered_by: str,
    trigger_reason: str,
    rationale: str,
) -> None:
    """Apply a Planning Agent replan output to this workflow.

    - Increments version
    - Increments replanning_count
    - Replaces self.steps with the merged result
      (completed steps preserved; remaining steps replaced)
    - Appends a ReplanEvent to replanning_history
    """
```

**Merge rules during apply_revision:**

- Steps with status DONE / FAILED / SKIPPED are preserved unchanged
- Steps with status PENDING / RUNNING are replaced by `revised_steps`
- New steps get `added_in_version` set to the new version number
- Step IDs from revised_steps must not collide with completed step IDs

---

## Condition expression evaluator

`condition` is a simple expression like `"s1.confidence >= 0.4"`. The evaluator supports:

- Comparison: `==`, `!=`, `>`, `<`, `>=`, `<=`
- Boolean: `and`, `or`, `not`
- Reference: `{step_id}.{field}` resolves to that step's result data
- Literals: numbers, strings, `null`, `true`, `false`

Implementation: minimal AST evaluator. **No `eval()`.** Uses `ast.parse` with whitelisted node types.

Example:
- `"s1.confidence >= 0.4"` → True if step s1's result has confidence ≥ 0.4
- `"s1.matched == true and s2.risk_level != 'CRITICAL'"`
- `"s1.web_text != null"`

If reference cannot be resolved (step missing, field missing) → returns `False` (step skipped).

---

## JSON Schema

```json
{
  "workflow_id": "wf-salesforce-intake-001",
  "programme_id": "nova-2026",
  "vendor_key": "salesforce",
  "vendor_id": null,
  "workflow_type": "INTAKE_INVESTIGATION",
  "created_by": "planning_agent",
  "created_at": "2026-05-12T10:00:00Z",
  "version": 1,
  "replanning_count": 0,
  "context": {
    "entity_type": "COMPANY",
    "brain_hit": false,
    "erp_spend": 840000,
    "planning_rationale": "High spend unknown vendor with linked document."
  },
  "steps": [
    {
      "step_id": "s1",
      "step_type": "WEB_RESEARCH_DEEP",
      "status": "PENDING",
      "depends_on": [],
      "condition": null,
      "retry_policy": {"max_attempts": 2, "backoff_seconds": 5},
      "planning_rationale": "Confirm vendor identity before fraud assessment",
      "added_in_version": 1
    }
  ],
  "replanning_history": []
}
```

---

## Errors

| Error | Condition |
|---|---|
| `WorkflowParseError` | workflow.json malformed or missing required fields |
| `WorkflowSaveError` | atomic_write fails during save |
| `InvalidConditionExpression` | condition string cannot be parsed |
| `StepIdCollision` | apply_revision attempts to add a step_id that already exists |

---

## Tests required

- Serialise → deserialise round trip preserves all fields
- Save to disk + load from disk produces equal object
- `apply_revision` preserves completed steps, replaces pending steps
- `apply_revision` increments version and replanning_count
- Condition evaluator: comparison operators work
- Condition evaluator: missing step reference → returns False
- Condition evaluator: literal `eval()` and other dangerous code is rejected
- `replanning_history` accumulates correctly across multiple revisions
