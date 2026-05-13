"""Confirm rebrand step — records rebrand mapping decision."""

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.signal_profile_schema import SignalProfile


def run(profile: SignalProfile) -> StepResult:
    return StepResult(
        step="CONFIRM_REBRAND",
        success=True,
        early_exit=False,
        exit_status=None,
        data={
            "former_name": profile.normalized,
            "canonical_name": profile.brain_hit.rebrand_target,
        },
        note="",
    )
