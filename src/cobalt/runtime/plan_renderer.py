"""Runtime layer — PlanRenderer: derive human-readable plan.md from workflow + state."""

from __future__ import annotations

import os
import re
from pathlib import Path

from cobalt.core.atomic_write import atomic_write
from cobalt.runtime.execution_state import ExecutionState, ExecutionStatus, StepRunRecord
from cobalt.runtime.workflow_definition import StepStatus, WorkflowDefinition, WorkflowStep


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _step_status_icon(status: StepStatus) -> str:
    return {
        StepStatus.PENDING: "☐",
        StepStatus.RUNNING: "▶",
        StepStatus.DONE:    "☑",
        StepStatus.FAILED:  "✗",
        StepStatus.SKIPPED: "⊘",
    }.get(status, "?")


def _format_iso(ts: str | None) -> str:
    """Shorten a full ISO timestamp to HH:MM:SSZ for the execution log."""
    if ts is None:
        return "—"
    try:
        time_part = ts.split("T")[1] if "T" in ts else ts
        # Strip timezone offset and microseconds
        time_part = time_part.split("+")[0].split("-")[0].split(".")[0]
        return time_part + "Z"
    except Exception:
        return ts


def _summarise_result(result: dict) -> str:
    """Pick 1-2 fields from result dict for the execution log summary column."""
    if not result:
        return "—"
    parts: list[str] = []
    if "confidence" in result:
        parts.append(f"confidence={result['confidence']:.2f}")
    if "matched" in result:
        parts.append(f"matched={result['matched']}")
    if not parts and "fraud_signals" in result and result["fraud_signals"]:
        parts.append(f"fraud_signals={len(result['fraud_signals'])}")
    if not parts:
        key, val = next(iter(result.items()))
        val_str = str(val)
        if len(val_str) > 40:
            val_str = val_str[:40] + "…"
        parts.append(f"{key}={val_str}")
    return ", ".join(parts[:2])


def _step_sort_key(step_id: str) -> tuple[str, int]:
    """Sort step IDs so s1 < s2 < s10 < s_extra."""
    m = re.match(r"^(.*?)(\d+)$", step_id)
    if m:
        return (m.group(1), int(m.group(2)))
    return (step_id, 0)


def _plan_md_path(workspace_root: Path, programme_id: str, workflow_id: str) -> Path:
    return workspace_root / programme_id / "workflows" / workflow_id / "plan.md"


# ---------------------------------------------------------------------------
# PlanRenderer
# ---------------------------------------------------------------------------

class PlanRenderer:
    """Converts (WorkflowDefinition, ExecutionState) into a human-readable plan.md."""

    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = workspace_root or Path(
            os.getenv("WORKSPACE_ROOT", "./workspace")
        )

    def render(self, workflow: WorkflowDefinition, state: ExecutionState) -> Path:
        """Write plan.md atomically; return the path written."""
        content = self.render_to_string(workflow, state)
        path = _plan_md_path(self.workspace_root, workflow.programme_id, workflow.workflow_id)
        atomic_write(path, content)
        return path

    def render_to_string(self, workflow: WorkflowDefinition, state: ExecutionState) -> str:
        """Pure function — return full markdown string without any I/O."""
        parts: list[str] = []

        # ----------------------------------------------------------------
        # Header
        # ----------------------------------------------------------------
        parts.append(f"# Workflow — {workflow.workflow_id}")
        parts.append(
            f"_{workflow.workflow_type} | {workflow.programme_id} | "
            f"version {workflow.version} | created {workflow.created_at}_"
        )
        parts.append("")

        # ----------------------------------------------------------------
        # Context
        # ----------------------------------------------------------------
        parts.append("## Context")
        parts.append("")
        ctx = {
            k: v for k, v in sorted(workflow.context.items())
            if v is not None and k != "planning_rationale"
        }
        if ctx:
            for k, v in ctx.items():
                parts.append(f"- `{k}`: {v}")
        else:
            parts.append("_No context fields._")
        parts.append("")

        # ----------------------------------------------------------------
        # Planning Rationale
        # ----------------------------------------------------------------
        parts.append("## Planning Rationale")
        parts.append("")
        rationale = workflow.context.get("planning_rationale") or ""
        parts.append(rationale if rationale.strip() else "_No rationale._")
        parts.append("")

        # ----------------------------------------------------------------
        # Steps
        # ----------------------------------------------------------------
        parts.append("## Steps")
        parts.append("")

        sorted_steps = sorted(workflow.steps, key=lambda s: _step_sort_key(s.step_id))

        # Build lookup for step records
        all_records: dict[str, StepRunRecord] = {
            **state.completed_steps,
            **state.failed_steps,
            **state.skipped_steps,
        }

        if not sorted_steps:
            parts.append("_No steps defined._")
        else:
            for step in sorted_steps:
                rec = all_records.get(step.step_id)
                icon = _step_status_icon(step.status)
                attempts = rec.attempts if rec else 0
                if step.status == StepStatus.FAILED:
                    status_phrase = f"failed (attempts: {attempts})"
                elif step.status == StepStatus.DONE:
                    status_phrase = "done"
                elif step.status == StepStatus.SKIPPED:
                    status_phrase = "skipped"
                elif step.status == StepStatus.RUNNING:
                    status_phrase = "running"
                else:
                    status_phrase = "pending"

                parts.append(
                    f"- {icon} `{step.step_id}` `{step.step_type}` — {status_phrase}"
                )
                rationale = (step.planning_rationale or "").strip()
                parts.append(f"  > {rationale if rationale else 'No rationale.'}")
                parts.append("")

        # ----------------------------------------------------------------
        # Execution Log
        # ----------------------------------------------------------------
        parts.append("## Execution Log")
        parts.append("")
        parts.append("| Step | Status | Started | Completed | Attempts | Summary |")
        parts.append("|---|---|---|---|---|---|")

        for step in sorted_steps:
            rec = all_records.get(step.step_id)
            if rec is not None:
                started   = _format_iso(rec.started_at)
                completed = _format_iso(rec.completed_at)
                attempts  = rec.attempts
                summary   = _summarise_result(rec.result)
                status    = rec.status.value
            else:
                started = completed = "—"
                attempts = 0
                summary  = "—"
                status   = step.status.value

            parts.append(
                f"| `{step.step_id}` `{step.step_type}` "
                f"| {status} | {started} | {completed} | {attempts} | {summary} |"
            )
        parts.append("")

        # ----------------------------------------------------------------
        # Accumulated Signals
        # ----------------------------------------------------------------
        parts.append("## Accumulated Signals")
        parts.append("")
        signals = state.accumulated_signals
        if signals:
            for k in sorted(signals.keys()):
                parts.append(f"- `{k}`: {signals[k]}")
        else:
            parts.append("_No signals yet._")
        parts.append("")

        # ----------------------------------------------------------------
        # Replanning History
        # ----------------------------------------------------------------
        parts.append("## Replanning History")
        parts.append("")
        if not workflow.replanning_history:
            parts.append("_No replans yet._")
        else:
            for event in workflow.replanning_history:
                parts.append(
                    f"### Version {event.version} — replanned at {event.replanned_at}"
                )
                parts.append("")
                parts.append(f"**Triggered by:** {event.triggered_by}")
                parts.append(f"**Reason:** {event.trigger_reason}")
                parts.append(f"**Rationale:** {event.rationale}")
                parts.append("")
                for sid in event.steps_added:
                    parts.append(f"- Added: `{sid}`")
                for sid in event.steps_removed:
                    parts.append(f"- Removed: `{sid}`")
                for sid in event.steps_modified:
                    parts.append(f"- Modified: `{sid}`")
                parts.append("")

        # ----------------------------------------------------------------
        # Outcome
        # ----------------------------------------------------------------
        parts.append("## Outcome")
        parts.append("")
        status = state.status

        if status == ExecutionStatus.COMPLETED:
            parts.append("**Status:** COMPLETED")
            if state.current_step_id:
                parts.append(f"**Final step:** {state.current_step_id}")
            outcome = state.outcome or {}
            if outcome:
                parts.append("**Result:**")
                for k, v in outcome.items():
                    parts.append(f"- {k}: {v}")
            else:
                parts.append("_No outcome data._")

        elif status == ExecutionStatus.BLOCKED:
            parts.append("**Status:** BLOCKED")
            outcome = state.outcome or {}
            parts.append(f"**Reason:** {outcome.get('reason', 'unknown')}")
            parts.append(f"**Step:** {outcome.get('step_id', '—')}")

        elif status == ExecutionStatus.FAILED:
            parts.append("**Status:** FAILED")
            outcome = state.outcome or {}
            if outcome:
                for k, v in outcome.items():
                    parts.append(f"- {k}: {v}")

        else:
            parts.append(f"**Status:** {status.value}")
            if state.current_step_id:
                parts.append(f"**Current step:** {state.current_step_id}")

        parts.append("")

        return "\n".join(parts)
