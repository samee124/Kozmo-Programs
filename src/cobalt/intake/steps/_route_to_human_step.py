"""Route to human step — triggers triage gate with a resolving question."""

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.investigation_plan_schema import InvestigationPlan
from cobalt.models.schemas.signal_profile_schema import SignalProfile


def run(profile: SignalProfile, plan: InvestigationPlan) -> StepResult:
    return StepResult(
        step="ROUTE_TO_HUMAN",
        success=True,
        early_exit=True,
        exit_status="TRIAGE_REQUIRED",
        data={
            "triage_reason": plan.reason,
            "triage_question": plan.resolving_question,
        },
        note="",
    )
