"""Workspace plan writer — writes programme and investigation plan files.

All file writes go through atomic_write() per Rule 3.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

import cobalt.core.file_system as _fs
from cobalt.core.atomic_write import atomic_write
from cobalt.intake.steps import StepResult
from cobalt.models.schemas.investigation_plan_schema import InvestigationPlan
from cobalt.models.schemas.signal_profile_schema import SignalProfile

logger = logging.getLogger(__name__)

_PROGRAMME_STEPS = [
    "source_intake",
    "candidate_screening",
    "entity_resolution",
    "external_validation",
    "entity_decision_and_shell_creation",
]


def _ws_root() -> Path:
    return Path(str(_fs.WORKSPACE_ROOT))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _parse_file(path: Path) -> tuple[dict, str] | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return None
    fm = yaml.safe_load(parts[1]) or {}
    return fm, parts[2]


def _write_file(path: Path, fm: dict, body: str) -> None:
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True)
    content = f"---\n{fm_yaml}---\n{body}"
    atomic_write(path, content)


# ---------------------------------------------------------------------------
# write_programme_plan
# ---------------------------------------------------------------------------

def write_programme_plan(programme_id: str, plan_context: dict) -> Path:
    """Write workspace/{programme_id}/programme_run/programme_plan.md."""
    root = _ws_root() / programme_id / "programme_run"
    path = root / "programme_plan.md"

    ctx = dict(plan_context)
    created_at = ctx.pop("planned_at", None) or _now_iso()
    fm: dict = {"programme_id": programme_id, "created_at": created_at, **ctx}

    step_checkboxes = "\n".join(f"- [ ] `{s}`" for s in _PROGRAMME_STEPS)
    strategy_prose = ctx.get("strategy_prose", "")
    strategy_section = f"\n## Strategy\n\n{strategy_prose}\n" if strategy_prose else ""

    body = (
        f"\n# Programme Plan — {programme_id}\n"
        f"{strategy_section}"
        f"\n## Steps\n\n{step_checkboxes}\n"
    )

    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True)
    atomic_write(path, f"---\n{fm_yaml}---\n{body}")
    return path


# ---------------------------------------------------------------------------
# write_investigation_plan
# ---------------------------------------------------------------------------

def write_investigation_plan(
    programme_id: str,
    candidate_key: str,
    profile: SignalProfile,
    plan: InvestigationPlan,
    why_prose: str | None = None,
) -> Path:
    """Write IP-{candidate_key}-{n:03}.md under programme_run/intake_plans/."""
    intake_plans_dir = _ws_root() / programme_id / "programme_run" / "intake_plans"
    intake_plans_dir.mkdir(parents=True, exist_ok=True)

    existing = list(intake_plans_dir.glob(f"IP-{candidate_key}-*.md"))
    n = f"{len(existing) + 1:03d}"
    fname = f"IP-{candidate_key}-{n}.md"
    path = intake_plans_dir / fname

    entity_type = (
        profile.entity_type.value
        if hasattr(profile.entity_type, "value")
        else str(profile.entity_type)
    )
    plan_depth = plan.depth.value if hasattr(plan.depth, "value") else str(plan.depth)
    plan_fraud = plan.fraud_risk.value if hasattr(plan.fraud_risk, "value") else str(plan.fraud_risk)

    fm: dict = {
        "intake_plan_id": f"IP-{candidate_key}-{n}",
        "candidate_key": candidate_key,
        "candidate_raw": profile.raw,
        "signals": {
            "brain_hit": {
                "matched": profile.brain_hit.matched,
                "confidence": profile.brain_hit.confidence,
                "canonical": profile.brain_hit.canonical,
            },
            "dedup_result": {
                "status": profile.dedup_result.status,
                "match_key": profile.dedup_result.match_key,
            },
            "entity_type": entity_type,
            "erp_signal": {"exists": profile.erp_signal.exists},
        },
        "plan": {
            "depth": plan_depth,
            "steps": list(plan.steps),
            "fraud_risk": plan_fraud,
            "reason": plan.reason,
        },
        "execution": {
            "step_results": {},
            "started_at": None,
        },
        "outcome": {
            "status": "PENDING",
            "vendor_id": None,
            "workspace_path": None,
        },
    }

    entity_table = (
        "## Entity Profile\n\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        f"| Raw input | {profile.raw} |\n"
        f"| Normalized | {profile.normalized} |\n"
        f"| Entity type | {entity_type} |\n"
        f"| Country | {profile.country_hint or '—'} |\n"
    )

    why_section = f"\n## Why This Plan\n\n{why_prose}\n" if why_prose else "\n## Why This Plan\n\n"

    if plan.steps:
        step_checks = "\n".join(f"- [ ] `{s}`" for s in plan.steps)
        steps_md = f"## Steps\n\n{step_checks}\n"
        log_rows = "\n".join(f"| `{s}` | PENDING | — |" for s in plan.steps)
        exec_log_md = (
            "## Execution Log\n\n"
            "| Step | Status | Note |\n"
            "| --- | --- | --- |\n"
            f"{log_rows}\n"
        )
    else:
        steps_md = "## Steps\n\nNo steps required\n"
        exec_log_md = "## Execution Log\n\nNo steps required\n"

    outcome_md = (
        "## Outcome\n\n"
        "**Status:** PENDING\n"
        "**Vendor ID:** —\n"
        "**Workspace:** —\n"
    )

    body = (
        f"\n# Investigation Plan — {candidate_key}\n\n"
        f"{entity_table}\n"
        f"{why_section}"
        f"{steps_md}\n"
        f"{exec_log_md}\n"
        f"{outcome_md}"
    )

    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True)
    atomic_write(path, f"---\n{fm_yaml}---\n{body}")
    return path


# ---------------------------------------------------------------------------
# update_plan_step
# ---------------------------------------------------------------------------

def update_plan_step(path: Path, step: str, result: StepResult) -> None:
    """Record a completed step result; tick its checkbox and update execution log."""
    parsed = _parse_file(path)
    if parsed is None:
        return
    fm, body = parsed

    exec_section = fm.setdefault("execution", {})
    exec_section.setdefault("step_results", {})[step] = {
        "success": result.success,
        "early_exit": result.early_exit,
        "exit_status": result.exit_status,
        "note": result.note,
    }

    step_esc = re.escape(step)

    # Tick checkbox — use lambda to prevent backslash group-reference expansion
    body = re.sub(
        rf"- \[ \] `{step_esc}`",
        lambda _m, _s=step: f"- [x] `{_s}`",
        body,
    )

    # Update execution log row — lambda prevents \N group-reference issues
    note = result.note or "ok"
    body = re.sub(
        rf"\| `{step_esc}` \| PENDING \| — \|",
        lambda _m, _s=step, _n=note: f"| `{_s}` | DONE | {_n} |",
        body,
    )

    _write_file(path, fm, body)


# ---------------------------------------------------------------------------
# finalise_plan
# ---------------------------------------------------------------------------

def finalise_plan(
    path: Path,
    status: str,
    vendor_id: str | None = None,
    workspace_path: str | None = None,
) -> None:
    """Record the final outcome of an investigation plan."""
    parsed = _parse_file(path)
    if parsed is None:
        return
    fm, body = parsed

    outcome = fm.setdefault("outcome", {})
    outcome["status"] = status
    outcome["vendor_id"] = vendor_id
    outcome["workspace_path"] = workspace_path

    execution = fm.setdefault("execution", {})
    if execution.get("started_at") is None:
        execution["started_at"] = _now_iso()

    # Update body markers using lambdas to handle backslashes in replacement values
    _status = status
    body = re.sub(
        r"\*\*Status:\*\* .*",
        lambda _m, _v=_status: f"**Status:** {_v}",
        body,
    )
    if vendor_id is not None:
        _vid = vendor_id
        body = re.sub(
            r"\*\*Vendor ID:\*\* .*",
            lambda _m, _v=_vid: f"**Vendor ID:** {_v}",
            body,
        )
    if workspace_path is not None:
        _wp = workspace_path
        body = re.sub(
            r"\*\*Workspace:\*\* .*",
            lambda _m, _v=_wp: f"**Workspace:** {_v}",
            body,
        )

    _write_file(path, fm, body)


# ---------------------------------------------------------------------------
# read_next_pending_step
# ---------------------------------------------------------------------------

def read_next_pending_step(path: Path) -> str | None:
    """Return the first step in plan.steps that has no entry in execution.step_results."""
    parsed = _parse_file(path)
    if parsed is None:
        return None
    fm, _ = parsed
    steps = fm.get("plan", {}).get("steps", [])
    done = set(fm.get("execution", {}).get("step_results", {}).keys())
    for step in steps:
        if step not in done:
            return step
    return None
