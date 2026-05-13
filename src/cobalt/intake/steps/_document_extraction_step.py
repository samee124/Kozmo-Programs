"""Document extraction step — V1 stub. Real extraction via Analysis Agent is V2."""

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.signal_profile_schema import SignalProfile


def run(profile: SignalProfile) -> StepResult:
    # TODO V2: call Analysis Agent extract_contract_terms on linked docs
    return StepResult(
        step="DOCUMENT_EXTRACTION",
        success=True,
        early_exit=False,
        exit_status=None,
        data={"configured": False},
        note="DOCUMENT_EXTRACTION not configured in V1",
    )
