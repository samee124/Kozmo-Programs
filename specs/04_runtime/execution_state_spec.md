# execution_state

## Overview

**Layer:** Runtime
**File:** `src/cobalt/runtime/execution_state.py`
**Role:** Durable record of what has happened during a workflow run. Updated atomically after every step. Source of crash recovery.
**Format on disk:** `state.json` inside `workspace/{programme_id}/workflows/{wf_id}/`

---

## Purpose

`ExecutionState` is the durable record of workflow execution progress.

- Updated by RuntimeEngine after every step result (atomically)
- Read on startup for crash recovery
- Records every step result with timestamps
- Accumulates signals across steps (e.g. fraud signals, confidence values)
- Carries final outcome at the end

Crash + restart = load `state.json`, find last DONE step, resume from next PENDING step.

---

## Dataclasses

### StepRunRecord

```python
@dataclass
class StepRunRecord:
    step_id:           str
    status:            StepStatus            # DONE / FAILED / SKIPPED
    attempts:          int                   # how many tries it took
    started_at:        str                   # ISO timestamp
    completed_at:      str                   # ISO timestamp
    result:            dict                  # raw result data from step
    error:             str | None            # exception message if FAILED
    replan_evaluated:  bool = False
    replan_triggered:  bool = False
```

### ExecutionState

```python
@dataclass
class ExecutionState:
    workflow_id:           str
    status:                ExecutionStatus            # NOT_STARTED / IN_PROGRESS / COMPLETED / FAILED / BLOCKED
    current_step_id:       str | None
    started_at:            str | None
    last_updated:          str
    completed_steps:       dict[str, StepRunRecord] = field(default_factory=dict)
    pending_steps:         list[str] = field(default_factory=list)
    failed_steps:          dict[str, StepRunRecord] = field(default_factory=dict)
    skipped_steps:         dict[str, StepRunRecord] = field(default_factory=dict)
    accumulated_signals:   dict = field(default_factory=dict)
    outcome:               dict | None = None
```

### ExecutionStatus (enum)

```python
class ExecutionStatus(str, Enum):
    NOT_STARTED  = "NOT_STARTED"
    IN_PROGRESS  = "IN_PROGRESS"
    COMPLETED    = "COMPLETED"
    FAILED       = "FAILED"
    BLOCKED      = "BLOCKED"
```

---

## Persistence

### save

```python
def save(self, path: Path | None = None) -> Path:
    """Atomic write to state.json. Updates last_updated to current ISO timestamp."""
```

### load

```python
@classmethod
def load(cls, workflow_id: str, programme_id: str) -> ExecutionState:
    """Read state.json and reconstruct dataclass. Returns NOT_STARTED state if file does not exist."""
```

### record_step_start

```python
def record_step_start(self, step_id: str) -> None:
    """Mark step as RUNNING. Update current_step_id. Persist."""
```

### record_step_complete

```python
def record_step_complete(
    self,
    step_id: str,
    result: dict,
    attempts: int = 1,
) -> None:
    """
    - Add StepRunRecord to completed_steps
    - Remove from pending_steps
    - Update accumulated_signals with any signals from result
    - Persist atomically
    """
```

### record_step_failure

```python
def record_step_failure(
    self,
    step_id: str,
    error: str,
    attempts: int,
) -> None:
    """Add StepRunRecord to failed_steps. Set status=FAILED if no retries left."""
```

### record_step_skip

```python
def record_step_skip(
    self,
    step_id: str,
    reason: str,
) -> None:
    """Add StepRunRecord with status=SKIPPED to skipped_steps."""
```

### accumulate_signals

```python
def accumulate_signals(self, new_signals: dict) -> None:
    """Merge new signals into accumulated_signals.

    Rules:
      - Numeric fields: max() of old and new
      - Lists: concatenated and deduplicated
      - Booleans: OR of old and new
      - Strings: overwrite (latest wins)
    """
```

---

## Crash recovery

The recovery contract:

1. On RuntimeEngine startup with a `workflow_id`:
   - Load `workflow.json` → WorkflowDefinition
   - Load `state.json` → ExecutionState (or NOT_STARTED if missing)

2. If state.status == COMPLETED → nothing to do, return outcome
3. If state.status == BLOCKED → return without executing
4. If state.status == FAILED and no retries available → return failure
5. Otherwise → resume:
   - Find next step where status is PENDING AND all `depends_on` are DONE
   - Skip any RUNNING step that crashed mid-execution (its state will be re-run)
   - Execute from that step

**A RUNNING step on restart means the previous process crashed during that step.** The runtime re-executes it. If the step has side effects, the step is responsible for being idempotent or for storing partial results in `result` to be picked up.

---

## JSON Schema

```json
{
  "workflow_id": "wf-salesforce-intake-001",
  "status": "IN_PROGRESS",
  "current_step_id": "s2",
  "started_at": "2026-05-12T10:01:00Z",
  "last_updated": "2026-05-12T10:01:45Z",
  "completed_steps": {
    "s1": {
      "step_id": "s1",
      "status": "DONE",
      "attempts": 1,
      "started_at": "2026-05-12T10:01:00Z",
      "completed_at": "2026-05-12T10:01:45Z",
      "result": {
        "research_text": "Salesforce Inc. is a CRM software company...",
        "confidence": 0.88
      },
      "error": null,
      "replan_evaluated": true,
      "replan_triggered": false
    }
  },
  "pending_steps": ["s2", "s3"],
  "failed_steps": {},
  "skipped_steps": {},
  "accumulated_signals": {
    "fraud_risk_level": "LOW",
    "entity_confirmed": true,
    "web_confidence": 0.88
  },
  "outcome": null
}
```

---

## Tests required

- save → load round trip preserves all fields
- `record_step_complete` removes step from pending_steps
- `record_step_start` updates current_step_id
- `accumulate_signals` correctly merges numeric (max), list (dedup), boolean (OR)
- load returns NOT_STARTED when file missing
- Crash mid-step (RUNNING) → restart resumes from that step
- `record_step_failure` increments failed_steps
- `record_step_skip` does not affect status counts
