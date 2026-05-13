"""Sanctions check step — V1 stub. Real OFAC/EU/UN check is V2."""

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.signal_profile_schema import SignalProfile


def run(profile: SignalProfile) -> StepResult:
    # TODO V2: implement real OFAC/EU/UN sanctions connector
    return StepResult(
        step="SANCTIONS_CHECK",
        success=True,
        early_exit=False,
        exit_status=None,
        data={"configured": False},
        note="SANCTIONS_CHECK not configured in V1",
    )
