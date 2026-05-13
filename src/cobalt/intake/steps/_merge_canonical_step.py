"""Merge canonical step — records dedup merge decision."""

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.signal_profile_schema import SignalProfile


def run(profile: SignalProfile) -> StepResult:
    return StepResult(
        step="MERGE_CANONICAL",
        success=True,
        early_exit=False,
        exit_status=None,
        data={
            "merged_into": profile.dedup_result.match_key,
            "similarity": profile.dedup_result.similarity,
        },
        note=f"Merged into '{profile.dedup_result.match_key}'",
    )
