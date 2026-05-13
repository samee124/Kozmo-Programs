"""Tool 3 — entity_resolution: resolve a screened candidate to a known entity."""

import logging
from dataclasses import dataclass, field

from cobalt.brain.loader import load_brain
from cobalt.intake._signal_collector import collect_signals
from cobalt.models.schemas.signal_profile_schema import (
    BatchContext,
    EntityType,
    SignalProfile,
)
from cobalt.tools.candidate_screening import ScreenedCandidate

logger = logging.getLogger(__name__)


@dataclass
class ResolutionResult:
    comparison_key: str
    signal_profile: SignalProfile
    resolution_status: str      # MATCHED / UNMATCHED_VIABLE / UNMATCHED_AMBIGUOUS / DISCARD
    matched_canonical: str | None
    match_type: str | None
    confidence: float
    entity_type: EntityType
    lifecycle_signals: list[str] = field(default_factory=list)   # VENDOR_REBRANDED / VENDOR_ACQUIRED
    recommendation: str = "INVESTIGATE"  # AUTO_CONFIRM / INVESTIGATE / TRIAGE / DISCARD


def resolve(screened: ScreenedCandidate, ctx: BatchContext) -> ResolutionResult:
    """Resolve a screened candidate to a known entity or viability status."""
    profile = collect_signals(screened.raw, ctx)

    # --- resolution_status ---
    if profile.entity_type in (EntityType.PERSON, EntityType.INTERNAL):
        resolution_status = "DISCARD"
    elif profile.brain_hit.matched:
        resolution_status = "MATCHED"
    elif profile.dedup_result.status == "CANDIDATE":
        resolution_status = "UNMATCHED_AMBIGUOUS"
    else:
        resolution_status = "UNMATCHED_VIABLE"

    # --- recommendation ---
    if resolution_status == "DISCARD":
        recommendation = "DISCARD"
    elif resolution_status == "MATCHED":
        recommendation = "AUTO_CONFIRM" if profile.brain_hit.confidence >= 0.90 else "INVESTIGATE"
    elif resolution_status == "UNMATCHED_AMBIGUOUS":
        recommendation = "TRIAGE"
    else:
        recommendation = "INVESTIGATE"

    # --- lifecycle signals ---
    lifecycle_signals: list[str] = []
    if profile.brain_hit.rebrand_match:
        lifecycle_signals.append("VENDOR_REBRANDED")
    if (
        profile.brain_hit.match_type == "ALIAS"
        and profile.brain_hit.alias_target
        and profile.brain_hit.alias_target != profile.cleaned.lower()
    ):
        lifecycle_signals.append("VENDOR_ACQUIRED")

    # --- matched_canonical / confidence ---
    if profile.brain_hit.matched:
        matched_canonical = profile.brain_hit.canonical
        match_type = profile.brain_hit.match_type
        confidence = profile.brain_hit.confidence
    else:
        matched_canonical = None
        match_type = None
        confidence = 0.0

    return ResolutionResult(
        comparison_key=profile.comparison_key,
        signal_profile=profile,
        resolution_status=resolution_status,
        matched_canonical=matched_canonical,
        match_type=match_type,
        confidence=confidence,
        entity_type=profile.entity_type,
        lifecycle_signals=lifecycle_signals,
        recommendation=recommendation,
    )


def resolve_all(
    screened_candidates: list[ScreenedCandidate],
    ctx: BatchContext,
) -> list[ResolutionResult]:
    """Resolve all screened candidates; never raises on individual failures."""
    logger.info("Resolving %d candidates against Brain", len(screened_candidates))
    results: list[ResolutionResult] = []
    for screened in screened_candidates:
        try:
            r = resolve(screened, ctx)
            logger.info(
                "  %-40s → %-18s  conf=%.0f%%  rec=%s",
                screened.raw[:40],
                r.resolution_status,
                r.confidence * 100,
                r.recommendation,
            )
            results.append(r)
        except Exception:
            # Safe fallback — preserve the list length contract.
            from cobalt.models.schemas.signal_profile_schema import (
                BrainHit, DedupResult, ErpSignal, ApSignal, ScriptType,
            )
            dummy_profile = SignalProfile(
                raw=screened.raw,
                cleaned=screened.raw,
                script_type=ScriptType.LATIN,
                country_hint=None,
                normalized="",
                comparison_key="",
                brain_hit=BrainHit(False, 0.0, None, None, False, None, False, None),
                erp_signal=ErpSignal(exists=False, spend=None, category=None, vendor_ids=[]),
                ap_signal=ApSignal(invoice_count=0),
                linked_doc_ids=[],
                spend_hint=None,
                category_hint=None,
                entity_type=EntityType.AMBIGUOUS,
                dedup_result=DedupResult(status="UNIQUE", match_key=None, match_name=None, similarity=0.0),
            )
            logger.warning("  %-40s → resolution failed, falling back to DISCARD", screened.raw[:40])
            results.append(ResolutionResult(
                comparison_key="",
                signal_profile=dummy_profile,
                resolution_status="DISCARD",
                matched_canonical=None,
                match_type=None,
                confidence=0.0,
                entity_type=EntityType.AMBIGUOUS,
                lifecycle_signals=[],
                recommendation="DISCARD",
            ))
    rec_counts: dict[str, int] = {}
    for r in results:
        rec_counts[r.recommendation] = rec_counts.get(r.recommendation, 0) + 1
    logger.info("Resolution complete — %s", rec_counts)
    return results
