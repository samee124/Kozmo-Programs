"""HR overlap check step — V1 stub. Real employee cross-reference is V2."""

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.signal_profile_schema import SignalProfile


def run(profile: SignalProfile) -> StepResult:
    # TODO V2: implement real HR overlap / employee cross-reference
    return StepResult(
        step="HR_OVERLAP_CHECK",
        success=True,
        early_exit=False,
        exit_status=None,
        data={"configured": False},
        note="HR_OVERLAP_CHECK not configured in V1",
    )
