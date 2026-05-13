"""Registry lookup step — V1 stub. Real company registry lookup is V2."""

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.signal_profile_schema import SignalProfile


def run(profile: SignalProfile) -> StepResult:
    # TODO V2: implement real company registry connector
    return StepResult(
        step="REGISTRY_LOOKUP",
        success=True,
        early_exit=False,
        exit_status=None,
        data={"configured": False},
        note="REGISTRY_LOOKUP not configured in V1",
    )
