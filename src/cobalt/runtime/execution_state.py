"""Runtime layer — ExecutionState: durable record of workflow execution progress."""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from cobalt.core.atomic_write import atomic_write
from cobalt.core.exceptions import WorkflowParseError, WorkflowSaveError
from cobalt.runtime.workflow_definition import StepStatus


# ---------------------------------------------------------------------------
# Enums and dataclasses
# ---------------------------------------------------------------------------

class ExecutionStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED   = "COMPLETED"
    FAILED      = "FAILED"
    BLOCKED     = "BLOCKED"


@dataclass
class StepRunRecord:
    step_id:         str
    status:          StepStatus
    attempts:        int
    started_at:      str
    completed_at:    str
    result:          dict
    error:           str | None
    replan_evaluated: bool = False
    replan_triggered: bool = False


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _record_to_dict(r: StepRunRecord) -> dict:
    return {
        "step_id":          r.step_id,
        "status":           r.status.value,
        "attempts":         r.attempts,
        "started_at":       r.started_at,
        "completed_at":     r.completed_at,
        "result":           r.result,
        "error":            r.error,
        "replan_evaluated": r.replan_evaluated,
        "replan_triggered": r.replan_triggered,
    }


def _record_from_dict(d: dict) -> StepRunRecord:
    return StepRunRecord(
        step_id=d["step_id"],
        status=StepStatus(d["status"]),
        attempts=d.get("attempts", 1),
        started_at=d["started_at"],
        completed_at=d["completed_at"],
        result=d.get("result") or {},
        error=d.get("error"),
        replan_evaluated=d.get("replan_evaluated", False),
        replan_triggered=d.get("replan_triggered", False),
    )


# ---------------------------------------------------------------------------
# Signal accumulation
# ---------------------------------------------------------------------------

def _merge_signal(old, new):
    """Merge a single signal value according to type-specific rules."""
    # Bool must be checked before int (bool is a subclass of int).
    if isinstance(old, bool) or isinstance(new, bool):
        return bool(old) or bool(new)
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return max(old, new)
    if isinstance(old, list) and isinstance(new, list):
        seen = set()
        merged = []
        for item in old + new:
            key = item if not isinstance(item, dict) else json.dumps(item, sort_keys=True)
            if key not in seen:
                seen.add(key)
                merged.append(item)
        return merged
    # String and everything else: latest wins.
    return new


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def _state_path(programme_id: str, workflow_id: str) -> Path:
    root = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))
    return root / programme_id / "workflows" / workflow_id / "state.json"


# ---------------------------------------------------------------------------
# ExecutionState
# ---------------------------------------------------------------------------

@dataclass
class ExecutionState:
    workflow_id:          str
    programme_id:         str
    status:               ExecutionStatus
    current_step_id:      str | None
    started_at:           str | None
    last_updated:         str
    completed_steps:      dict[str, StepRunRecord] = field(default_factory=dict)
    pending_steps:        list[str] = field(default_factory=list)
    failed_steps:         dict[str, StepRunRecord] = field(default_factory=dict)
    skipped_steps:        dict[str, StepRunRecord] = field(default_factory=dict)
    accumulated_signals:  dict = field(default_factory=dict)
    outcome:              dict | None = None

    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "workflow_id":         self.workflow_id,
            "programme_id":        self.programme_id,
            "status":              self.status.value,
            "current_step_id":     self.current_step_id,
            "started_at":          self.started_at,
            "last_updated":        self.last_updated,
            "completed_steps":     {k: _record_to_dict(v) for k, v in self.completed_steps.items()},
            "pending_steps":       self.pending_steps,
            "failed_steps":        {k: _record_to_dict(v) for k, v in self.failed_steps.items()},
            "skipped_steps":       {k: _record_to_dict(v) for k, v in self.skipped_steps.items()},
            "accumulated_signals": self.accumulated_signals,
            "outcome":             self.outcome,
        }

    def save(self, path: Path | None = None) -> Path:
        self.last_updated = datetime.now(tz=timezone.utc).isoformat()
        if path is None:
            path = _state_path(self.programme_id, self.workflow_id)
        json_str = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        try:
            atomic_write(path, json_str)
        except Exception as exc:
            raise WorkflowSaveError(
                f"Failed to save execution state for {self.workflow_id}: {exc}"
            ) from exc
        return path

    @classmethod
    def load(cls, workflow_id: str, programme_id: str) -> "ExecutionState":
        path = _state_path(programme_id, workflow_id)
        if not path.exists():
            return cls(
                workflow_id=workflow_id,
                programme_id=programme_id,
                status=ExecutionStatus.NOT_STARTED,
                current_step_id=None,
                started_at=None,
                last_updated=datetime.now(tz=timezone.utc).isoformat(),
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowParseError(f"Malformed JSON in state.json: {exc}") from exc
        try:
            return cls(
                workflow_id=raw["workflow_id"],
                programme_id=raw.get("programme_id", programme_id),
                status=ExecutionStatus(raw["status"]),
                current_step_id=raw.get("current_step_id"),
                started_at=raw.get("started_at"),
                last_updated=raw.get("last_updated", datetime.now(tz=timezone.utc).isoformat()),
                completed_steps={k: _record_from_dict(v) for k, v in raw.get("completed_steps", {}).items()},
                pending_steps=raw.get("pending_steps") or [],
                failed_steps={k: _record_from_dict(v) for k, v in raw.get("failed_steps", {}).items()},
                skipped_steps={k: _record_from_dict(v) for k, v in raw.get("skipped_steps", {}).items()},
                accumulated_signals=raw.get("accumulated_signals") or {},
                outcome=raw.get("outcome"),
            )
        except (KeyError, ValueError) as exc:
            raise WorkflowParseError(f"state.json schema mismatch: {exc}") from exc

    # ------------------------------------------------------------------
    # Mutation methods — each persists atomically
    # ------------------------------------------------------------------

    def record_step_start(self, step_id: str) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self.current_step_id = step_id
        self.status = ExecutionStatus.IN_PROGRESS
        if self.started_at is None:
            self.started_at = now
        self.save()

    def record_step_complete(
        self,
        step_id: str,
        result: dict,
        attempts: int = 1,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self.completed_steps[step_id] = StepRunRecord(
            step_id=step_id,
            status=StepStatus.DONE,
            attempts=attempts,
            started_at=now,
            completed_at=now,
            result=result,
            error=None,
        )
        if step_id in self.pending_steps:
            self.pending_steps.remove(step_id)
        self.accumulate_signals(result)
        self.save()

    def record_step_failure(
        self,
        step_id: str,
        error: str,
        attempts: int,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self.failed_steps[step_id] = StepRunRecord(
            step_id=step_id,
            status=StepStatus.FAILED,
            attempts=attempts,
            started_at=now,
            completed_at=now,
            result={},
            error=error,
        )
        if step_id in self.pending_steps:
            self.pending_steps.remove(step_id)
        self.status = ExecutionStatus.FAILED
        self.save()

    def record_step_skip(self, step_id: str, reason: str) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self.skipped_steps[step_id] = StepRunRecord(
            step_id=step_id,
            status=StepStatus.SKIPPED,
            attempts=0,
            started_at=now,
            completed_at=now,
            result={"skip_reason": reason},
            error=None,
        )
        if step_id in self.pending_steps:
            self.pending_steps.remove(step_id)
        self.save()

    def accumulate_signals(self, new_signals: dict) -> None:
        for key, new_val in new_signals.items():
            if key not in self.accumulated_signals:
                self.accumulated_signals[key] = new_val
            else:
                self.accumulated_signals[key] = _merge_signal(
                    self.accumulated_signals[key], new_val
                )
