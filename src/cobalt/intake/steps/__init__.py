"""Step result type — shared across all intake step modules."""

from dataclasses import dataclass, field


@dataclass
class StepResult:
    step:        str
    success:     bool
    early_exit:  bool
    exit_status: str | None   # BLOCKED / TRIAGE_REQUIRED / None
    data:        dict
    note:        str
