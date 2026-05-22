"""Tool 5 — entity_decision_and_shell_creation: determine outcome and create workspace."""

import logging
import re
from dataclasses import dataclass, field

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.intake_result_schema import IntakeResult, IntakeStatus
from cobalt.models.schemas.investigation_plan_schema import InvestigationDepth, InvestigationPlan
from cobalt.models.schemas.signal_profile_schema import EntityType, ErpSignal, SignalProfile
from cobalt.workspace.builder import build_workspace

logger = logging.getLogger(__name__)

_LEGAL_SUFFIX_RE = re.compile(
    r",?\s*(Corporation|Corp\.?|Incorporated|Inc\.?|Limited|Ltd\.?|LLC|LLP|GmbH|Co\.?)$",
    re.IGNORECASE,
)

_INTAKE_STATUS_MAP = {
    "CONFIRMED": IntakeStatus.CONFIRMED,
    "TRIAGE_REQUIRED": IntakeStatus.TRIAGE_REQUIRED,
    "DISCARDED": IntakeStatus.DISCARDED,
    "BLOCKED": IntakeStatus.BLOCKED,
}


@dataclass
class EntityDecision:
    vendor_id:       str | None
    status:          str           # CONFIRMED / TRIAGE_REQUIRED / DISCARDED / BLOCKED
    workspace_path:  str | None
    data_class:      str
    initial_pcs:     int
    confidence:      float
    fraud_risk:      str
    triage_question: str | None
    block_reason:    str | None
    files_written:   list[str] = field(default_factory=list)


def _determine_status(
    plan: InvestigationPlan,
    profile: SignalProfile,
    step_results: list[StepResult],
) -> str:
    if plan.depth == InvestigationDepth.SKIP and "MERGE_CANONICAL" in plan.steps:
        return "CONFIRMED"
    if plan.depth == InvestigationDepth.SKIP and profile.entity_type in (
        EntityType.PERSON, EntityType.INTERNAL
    ):
        return "DISCARDED"
    for r in step_results:
        if r.exit_status == "BLOCKED":
            return "BLOCKED"
    for r in step_results:
        if r.exit_status == "TRIAGE_REQUIRED":
            return "TRIAGE_REQUIRED"
    return "CONFIRMED"


def _compute_data_class(erp_signal: ErpSignal | None, extracted_terms: dict | None) -> str:
    if erp_signal and erp_signal.spend is not None:
        return "CLASS_B" if extracted_terms else "CLASS_C"
    return "CLASS_D"


def _compute_pcs(erp_signal: ErpSignal | None, extracted_terms: dict | None) -> int:
    pcs = 0
    if erp_signal and erp_signal.spend is not None:
        pcs += 12
    if extracted_terms:
        if extracted_terms.get("renewal_date") is not None:
            pcs += 15
        if extracted_terms.get("auto_renewal") is not None:
            pcs += 10
        if extracted_terms.get("contract_value") is not None:
            pcs += 8
        if extracted_terms.get("price_escalation") is not None:
            pcs += 5
        if extracted_terms.get("baa_present") is True:
            pcs += 5
    return pcs


def _extract_fraud_signals(step_results: list[StepResult]) -> list[str]:
    signals: list[str] = []
    for r in step_results:
        for sig in r.data.get("fraud_signals", []):
            signals.append(sig)
    return signals


def _get_triage_question(step_results: list[StepResult]) -> str | None:
    for r in step_results:
        if r.exit_status == "TRIAGE_REQUIRED":
            q = r.data.get("triage_question")
            if q:
                return q
    return None


def _resolution_method(profile: SignalProfile, step_results: list[StepResult]) -> str:
    if profile.dedup_result.status == "AUTO_MERGE":
        return "DEDUP_MERGE"
    if profile.brain_hit.matched:
        if profile.brain_hit.rebrand_match:
            return "REBRAND"
        if profile.brain_hit.alias_match:
            return "ALIAS"
        return "BRAIN_LOOKUP"
    for r in step_results:
        if r.step.startswith("WEB_RESEARCH_"):
            return "WEB_RESEARCH"
    return "UNKNOWN"


def _canonical_name(profile: SignalProfile) -> str | None:
    if profile.brain_hit.matched and profile.brain_hit.canonical:
        return profile.brain_hit.canonical
    if profile.dedup_result.status == "AUTO_MERGE" and profile.dedup_result.match_name:
        return profile.dedup_result.match_name
    raw = (profile.raw or "").strip()
    if raw:
        cleaned = _LEGAL_SUFFIX_RE.sub("", raw).strip().rstrip(".,;: ")
        return cleaned or raw
    return profile.normalized


def decide_and_create(
    entry: str,
    profile: SignalProfile,
    plan: InvestigationPlan,
    step_results: list[StepResult],
    programme_id: str,
    extracted_terms: dict | None = None,
    erp_signal: ErpSignal | None = None,
) -> EntityDecision:
    """Determine entity outcome and create workspace for CONFIRMED vendors. Never raises."""
    logger.info("  Decision for %r ...", entry[:40])
    try:
        # Step 1 — determine status
        status = _determine_status(plan, profile, step_results)
        logger.info("  Status → %s", status)

        # Step 2 — vendor_id for CONFIRMED (human-readable: comparison_key slug)
        vendor_id: str | None = None
        if status == "CONFIRMED":
            vendor_id = profile.comparison_key.replace(" ", "-")
            logger.info("  vendor_id assigned → %s", vendor_id)

        # Step 3 — data_class
        data_class = _compute_data_class(erp_signal, extracted_terms)

        # Step 4 — initial_pcs
        initial_pcs = _compute_pcs(erp_signal, extracted_terms)

        # Step 5 — build IntakeResult
        fraud_signals = _extract_fraud_signals(step_results)
        triage_question = _get_triage_question(step_results)
        block_reason = "FRAUD_DETECTED" if status == "BLOCKED" else None
        confidence = profile.brain_hit.confidence if profile.brain_hit.matched else 0.82
        fraud_risk_val = plan.fraud_risk.value if hasattr(plan.fraud_risk, "value") else str(plan.fraud_risk)

        intake_result = IntakeResult(
            raw_input=entry,
            canonical_name=_canonical_name(profile),
            vendor_id=vendor_id,
            status=_INTAKE_STATUS_MAP.get(status, IntakeStatus.TRIAGE_REQUIRED),
            confidence=confidence,
            resolution_method=_resolution_method(profile, step_results),
            country_code=profile.country_hint,
            erp_spend=erp_signal.spend if erp_signal else None,
            erp_category=erp_signal.category if erp_signal else profile.category_hint,
            data_class=data_class,
            entity_type=(
                profile.entity_type.value
                if hasattr(profile.entity_type, "value")
                else str(profile.entity_type)
            ),
            triage_reason=plan.reason if status in ("TRIAGE_REQUIRED", "BLOCKED") else None,
            triage_question=triage_question,
            fraud_signals=fraud_signals,
            fraud_risk=fraud_risk_val,
            block_reason=block_reason,
            aliases=[],
            linked_doc_ids=profile.linked_doc_ids,
            extracted_terms=extracted_terms,
            investigation_plan=plan,
        )

        # Step 6 — create workspace for CONFIRMED
        files_written: list[str] = []
        workspace_path: str | None = None

        if status == "CONFIRMED":
            logger.info("  Building workspace for %s ...", vendor_id)
            build_result = build_workspace(intake_result, programme_id, extracted_terms, erp_signal)
            files_written = build_result.files_written
            workspace_path = str(build_result.workspace_path)
            logger.info("  Workspace created → %s  (%d files)", workspace_path, len(files_written))

            # Write per-vendor P1 plan
            try:
                from cobalt.agents.planning_agent import PlanningAgent
                PlanningAgent().write_vendor_p1_plan(
                    programme_id=programme_id,
                    vendor_id=vendor_id,
                    decision=status,
                    confidence=confidence,
                    data_class=data_class,
                    fraud_risk=fraud_risk_val,
                )
            except Exception as _plan_exc:
                logger.warning("P1 plan write failed for %s: %s", vendor_id, _plan_exc)

        # Step 7 — return EntityDecision
        return EntityDecision(
            vendor_id=vendor_id,
            status=status,
            workspace_path=workspace_path,
            data_class=data_class,
            initial_pcs=initial_pcs,
            confidence=confidence,
            fraud_risk=fraud_risk_val,
            triage_question=triage_question,
            block_reason=block_reason,
            files_written=files_written,
        )

    except Exception as exc:
        logger.error("decide_and_create failed for %r: %s", entry, exc)
        return EntityDecision(
            vendor_id=None,
            status="TRIAGE_REQUIRED",
            workspace_path=None,
            data_class="CLASS_D",
            initial_pcs=0,
            confidence=0.0,
            fraud_risk="LOW",
            triage_question="Processing error — manual review needed",
            block_reason=None,
            files_written=[],
        )
