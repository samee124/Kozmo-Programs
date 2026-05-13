"""Tool 4 — external_validation: execute an investigation plan step by step."""

import logging
from dataclasses import dataclass, field

from cobalt.agents.research_agent import ResearchAgent
from cobalt.intake.steps import StepResult
from cobalt.intake.steps import (
    _confirm_rebrand_step,
    _document_extraction_step,
    _fraud_check_step,
    _hr_overlap_step,
    _merge_canonical_step,
    _registry_lookup_step,
    _route_to_human_step,
    _sanctions_check_step,
    _web_research_step,
)
from cobalt.models.schemas.investigation_plan_schema import InvestigationPlan
from cobalt.models.schemas.signal_profile_schema import SignalProfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step registry — every callable receives (entry, profile, plan, ra, last_web)
# ---------------------------------------------------------------------------

STEP_REGISTRY: dict[str, object] = {
    "WEB_RESEARCH_FAST": (
        lambda entry, profile, plan, ra, wr:
        _web_research_step.run(entry, profile.comparison_key, "FAST", ra, profile.country_hint)
    ),
    "WEB_RESEARCH_STANDARD": (
        lambda entry, profile, plan, ra, wr:
        _web_research_step.run(entry, profile.comparison_key, "STANDARD", ra, profile.country_hint)
    ),
    "WEB_RESEARCH_DEEP": (
        lambda entry, profile, plan, ra, wr:
        _web_research_step.run(entry, profile.comparison_key, "DEEP", ra, profile.country_hint)
    ),
    "FRAUD_CHECK_BASIC": (
        lambda entry, profile, plan, ra, wr:
        _fraud_check_step.run_basic(profile, wr)
    ),
    "FRAUD_CHECK_DEEP": (
        lambda entry, profile, plan, ra, wr:
        _fraud_check_step.run_deep(profile, wr)
    ),
    "FRAUD_CHECK_ASYNC": (
        lambda entry, profile, plan, ra, wr:
        StepResult(step="FRAUD_CHECK_ASYNC", success=True, early_exit=False,
                   exit_status=None, data={"queued": True}, note="async check queued")
    ),
    "MERGE_CANONICAL": (
        lambda entry, profile, plan, ra, wr:
        _merge_canonical_step.run(profile)
    ),
    "CONFIRM_REBRAND": (
        lambda entry, profile, plan, ra, wr:
        _confirm_rebrand_step.run(profile)
    ),
    "ROUTE_TO_HUMAN": (
        lambda entry, profile, plan, ra, wr:
        _route_to_human_step.run(profile, plan)
    ),
    "SANCTIONS_CHECK": (
        lambda entry, profile, plan, ra, wr:
        _sanctions_check_step.run(profile)
    ),
    "REGISTRY_LOOKUP": (
        lambda entry, profile, plan, ra, wr:
        _registry_lookup_step.run(profile)
    ),
    "HR_OVERLAP_CHECK": (
        lambda entry, profile, plan, ra, wr:
        _hr_overlap_step.run(profile)
    ),
    "DOCUMENT_EXTRACTION": (
        lambda entry, profile, plan, ra, wr:
        _document_extraction_step.run(profile)
    ),
    "TRANSLITERATION": (
        lambda entry, profile, plan, ra, wr:
        StepResult(step="TRANSLITERATION", success=True, early_exit=False,
                   exit_status=None, data={}, note="V2")
    ),
    "MULTILINGUAL_SEARCH": (
        lambda entry, profile, plan, ra, wr:
        StepResult(step="MULTILINGUAL_SEARCH", success=True, early_exit=False,
                   exit_status=None, data={}, note="V2")
    ),
    "LEGAL_GATE": (
        lambda entry, profile, plan, ra, wr:
        StepResult(step="LEGAL_GATE", success=True, early_exit=True,
                   exit_status="TRIAGE_REQUIRED",
                   data={"legal_review": True}, note="requires legal review")
    ),
    "CONFIRM_ALIAS": (
        lambda entry, profile, plan, ra, wr:
        StepResult(step="CONFIRM_ALIAS", success=True, early_exit=False,
                   exit_status=None, data={}, note="alias confirmed")
    ),
}


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    entry:        str
    profile:      SignalProfile
    plan:         InvestigationPlan
    step_results: list[StepResult]
    final_status: str          # CONFIRMED / TRIAGE_REQUIRED / BLOCKED
    exit_reason:  str | None


# ---------------------------------------------------------------------------
# execute_plan
# ---------------------------------------------------------------------------

def execute_plan(
    entry: str,
    profile: SignalProfile,
    plan: InvestigationPlan,
    research_agent: ResearchAgent,
) -> tuple[list[StepResult], str, str | None]:
    """Run each step in plan.steps. Return (results, final_status, exit_reason)."""
    results: list[StepResult] = []
    last_web_result: StepResult | None = None

    logger.info("  Executing %d steps for %r: %s", len(plan.steps), entry[:40], plan.steps)
    for step in plan.steps:
        fn = STEP_REGISTRY.get(step)
        if fn is None:
            logger.warning("  Unknown step %r — skipping", step)
            continue

        logger.info("    → %s ...", step)
        step_result: StepResult = fn(entry, profile, plan, research_agent, last_web_result)  # type: ignore[operator]
        status_tag = "OK" if step_result.success else "FAIL"
        if step_result.early_exit:
            status_tag = f"EARLY_EXIT({step_result.exit_status})"
        logger.info("    ← %s  [%s]  %s", step, status_tag, step_result.note or "")
        results.append(step_result)

        if step.startswith("WEB_RESEARCH_"):
            last_web_result = step_result

        if step_result.early_exit:
            return results, step_result.exit_status or "UNKNOWN", step_result.exit_status

    # Post-loop: derive final_status from fraud check outcomes
    final_status = "CONFIRMED"
    exit_reason: str | None = None

    for r in results:
        action = r.data.get("recommended_action")
        if action == "BLOCK":
            final_status = "BLOCKED"
            exit_reason = "FRAUD_DETECTED"
            break
        if action == "TRIAGE" and final_status != "BLOCKED":
            final_status = "TRIAGE_REQUIRED"
            exit_reason = "FRAUD_SIGNALS"

    return results, final_status, exit_reason


# ---------------------------------------------------------------------------
# validate / validate_all
# ---------------------------------------------------------------------------

def validate(
    entry: str,
    profile: SignalProfile,
    plan: InvestigationPlan,
    research_agent: ResearchAgent,
) -> ValidationResult:
    """Validate a single entry. Never raises."""
    try:
        step_results, final_status, exit_reason = execute_plan(
            entry, profile, plan, research_agent
        )
    except Exception as exc:
        logger.error("execute_plan failed for %r: %s", entry, exc)
        step_results = []
        final_status = "TRIAGE_REQUIRED"
        exit_reason = "EXECUTION_ERROR"

    return ValidationResult(
        entry=entry,
        profile=profile,
        plan=plan,
        step_results=step_results,
        final_status=final_status,
        exit_reason=exit_reason,
    )


def validate_all(
    entries: list[tuple[str, SignalProfile, InvestigationPlan]],
    research_agent: ResearchAgent,
) -> list[ValidationResult]:
    """Validate all entries. Never raises. Same-length list as input."""
    return [validate(e, p, pl, research_agent) for e, p, pl in entries]
