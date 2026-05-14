"""Enrichment orchestrator — Process 2 entry point.

Wires all five enrichment tools through the RuntimeEngine.  Supports crash
recovery via per-step snapshots stored in state.completed_steps results.
Never raises — always returns EnrichmentRunResult.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import yaml

from cobalt.agents.planning_agent import PlanningAgent
from cobalt.core.atomic_write import append_md
from cobalt.core.exceptions import EnrichmentReadinessReadError
from cobalt.core.file_system import read_md
from cobalt.models.schemas.enrichment_schema import (
    BrainUpdateSuggestion,
    EnrichedProfileResult,
    EnrichmentReadinessResult,
    EnrichmentRunResult,
    ExtractedAttributes,
    RelationshipLifecycleResult,
    RelationshipMap,
    SourceEvidenceBundle,
)
from cobalt.runtime.execution_state import ExecutionState
from cobalt.runtime.runtime_engine import RuntimeEngine, StepFatal, WorkflowOutcome
from cobalt.tools.attribute_extractor import extract_attributes
from cobalt.tools.enriched_profile_creator import create_enriched_profile
from cobalt.tools.enrichment_readiness_check import check_enrichment_readiness
from cobalt.tools.external_source_collector import collect_sources
from cobalt.tools.relationship_and_lifecycle_mapper import map_relationships_and_lifecycle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _resolve_workspace_root(workspace_root: Path | None) -> Path:
    return workspace_root or Path(os.getenv("WORKSPACE_ROOT", "./workspace"))


def _read_entity_md(programme_id: str, vendor_id: str, workspace: Path) -> dict:
    """Read vendor data from the single vendor file. Returns {} if missing or unreadable."""
    from cobalt.core.file_system import _find_vendor_file
    try:
        file_path = _find_vendor_file(programme_id, vendor_id, workspace)
        if file_path is None:
            return {}
        data = read_md(file_path) or {}
        # Flatten nested new-format to the flat keys expected by SearchContext
        identity = data.get("identity") or {}
        digital = data.get("digital") or {}
        intake = data.get("intake") or {}

        def _val(section: dict, key: str):
            fd = section.get(key)
            return fd.get("value") if isinstance(fd, dict) else fd

        return {
            "vendor_name":        data.get("canonical_name"),
            "canonical_name":     data.get("canonical_name"),
            "hq_country":         _val(identity, "hq_country"),
            "website":            _val(identity, "website"),
            "domain":             _val(digital, "domain"),
            "confidence":         intake.get("confidence"),
            "identity_confidence": intake.get("confidence"),
            "data_class":         intake.get("data_class"),
        }
    except Exception as exc:
        logger.warning("Could not read vendor file for %s/%s: %s", programme_id, vendor_id, exc)
        return {}


def _read_pcs_from_coverage(programme_id: str, vendor_id: str, workspace: Path) -> float:
    """Read PCS score from the single vendor file. Returns 0.0 if missing."""
    from cobalt.core.file_system import _find_vendor_file
    try:
        file_path = _find_vendor_file(programme_id, vendor_id, workspace)
        if file_path is None:
            return 0.0
        data = read_md(file_path) or {}
        pcs = data.get("pcs") or {}
        score = pcs.get("score")
        if score is not None:
            # pcs.score is stored on 0-100 scale; _compute_pcs works on 0-1 scale
            return float(score) / 100.0
        # Legacy flat format fallback
        return float(data.get("overall_pcs", 0.0))
    except Exception as exc:
        logger.warning("Could not read PCS for %s/%s: %s", programme_id, vendor_id, exc)
    return 0.0


# ---------------------------------------------------------------------------
# Snapshot restore helpers (crash recovery)
# ---------------------------------------------------------------------------

def _restore_bundle(run_cache: dict, state: ExecutionState) -> SourceEvidenceBundle | None:
    """Return bundle from run_cache or restore from s1 snapshot in state."""
    if "bundle" in run_cache:
        return run_cache["bundle"]  # type: ignore[return-value]
    s1 = state.completed_steps.get("s1")
    if s1 and s1.result and "bundle_snapshot" in s1.result:
        bundle = SourceEvidenceBundle.from_dict(s1.result["bundle_snapshot"])
        run_cache["bundle"] = bundle
        return bundle
    return None


def _restore_extracted(run_cache: dict, state: ExecutionState) -> ExtractedAttributes | None:
    """Return extracted from run_cache or restore from s2 snapshot in state."""
    if "extracted" in run_cache:
        return run_cache["extracted"]  # type: ignore[return-value]
    s2 = state.completed_steps.get("s2")
    if s2 and s2.result and "extracted_snapshot" in s2.result:
        extracted = ExtractedAttributes.from_dict(s2.result["extracted_snapshot"])
        run_cache["extracted"] = extracted
        return extracted
    return None


def _restore_relationship(
    run_cache: dict, state: ExecutionState
) -> RelationshipLifecycleResult | None:
    """Return relationship_result from run_cache or restore from s3 snapshot in state."""
    if "relationship_result" in run_cache:
        return run_cache["relationship_result"]  # type: ignore[return-value]
    s3 = state.completed_steps.get("s3")
    if s3 and s3.result and "relationship_snapshot" in s3.result:
        rel = RelationshipLifecycleResult.from_dict(s3.result["relationship_snapshot"])
        run_cache["relationship_result"] = rel
        return rel
    return None


# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------

def _build_enrichment_step_registry(
    readiness: EnrichmentReadinessResult,
    entity_data: dict,
    pcs_before: float,
    workspace_root: Path,
    programme_id: str,
    vendor_id: str,
    brain: object,
    run_cache: dict,
) -> dict:
    """Build step registry with closures that share run_cache for crash recovery."""

    def step_collect_sources(workflow, state, step) -> dict:
        bundle = collect_sources(
            vendor_id=vendor_id,
            programme_id=programme_id,
            readiness=readiness,
            entity_data=entity_data,
            workspace_root=workspace_root,
        )
        run_cache["bundle"] = bundle
        return {"bundle_snapshot": bundle.to_dict(), "depth_tier": bundle.depth_tier}

    def step_extract_attributes(workflow, state, step) -> dict:
        bundle = _restore_bundle(run_cache, state)
        if bundle is None:
            raise StepFatal("s1 bundle missing — cannot extract attributes")
        extracted = extract_attributes(bundle, readiness.known_facts)
        run_cache["extracted"] = extracted
        return {"extracted_snapshot": extracted.to_dict()}

    def step_map_relationships(workflow, state, step) -> dict:
        bundle = _restore_bundle(run_cache, state)
        if bundle is None:
            raise StepFatal("s1 bundle missing — cannot map relationships")
        # Use extracted if s2 has already run; fall back to empty placeholder
        extracted = _restore_extracted(run_cache, state) or ExtractedAttributes(
            vendor_id=vendor_id, fields={}, conflicts=[], extraction_flags=[]
        )
        rel = map_relationships_and_lifecycle(bundle, extracted, entity_data, brain)
        run_cache["relationship_result"] = rel
        return {"relationship_snapshot": rel.to_dict()}

    def step_create_profile(workflow, state, step) -> dict:
        extracted = _restore_extracted(run_cache, state)
        rel = _restore_relationship(run_cache, state)
        if extracted is None:
            raise StepFatal("s2 extracted attributes missing — cannot create profile")
        if rel is None:
            raise StepFatal("s3 relationship result missing — cannot create profile")
        profile_result = create_enriched_profile(
            extracted=extracted,
            relationship_result=rel,
            readiness=readiness,
            entity_data=entity_data,
            pcs_before=pcs_before,
            workspace_root=workspace_root,
            programme_id=programme_id,
            vendor_id=vendor_id,
        )
        run_cache["profile_result"] = profile_result
        return {
            "profile_status":     profile_result.profile_status,
            "overall_confidence": profile_result.overall_confidence,
            "pcs_after":          profile_result.pcs_after,
            "flags":              profile_result.flags,
            "triage_task_count":  len(profile_result.triage_tasks),
        }

    return {
        "COLLECT_SOURCES":    step_collect_sources,
        "EXTRACT_ATTRIBUTES": step_extract_attributes,
        "MAP_RELATIONSHIPS":  step_map_relationships,
        "CREATE_PROFILE":     step_create_profile,
    }


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------

def _assemble_result(
    vendor_id: str,
    workflow_id: str,
    outcome: WorkflowOutcome,
    pcs_before: float,
    run_cache: dict,
) -> EnrichmentRunResult:
    profile_result: EnrichedProfileResult | None = run_cache.get("profile_result")
    reason = (outcome.outcome or {}).get("reason") if outcome.outcome else None

    if outcome.status == "COMPLETED":
        if profile_result is not None:
            return EnrichmentRunResult(
                vendor_id=vendor_id,
                workflow_id=workflow_id,
                status="COMPLETED",
                profile_status=profile_result.profile_status,
                overall_confidence=profile_result.overall_confidence,
                flags=profile_result.flags,
                triage_tasks=profile_result.triage_tasks,
                brain_update_suggestions=profile_result.brain_update_suggestions,
                pcs_before=pcs_before,
                pcs_after=profile_result.pcs_after,
                error=profile_result.error,
                reason=None,
            )
        # Workflow completed but s4 failed silently (exhausted retries)
        return EnrichmentRunResult(
            vendor_id=vendor_id,
            workflow_id=workflow_id,
            status="PARTIAL",
            profile_status=None,
            overall_confidence=None,
            flags=[],
            triage_tasks=[],
            brain_update_suggestions=[],
            pcs_before=pcs_before,
            pcs_after=pcs_before,
            error="profile_not_created",
            reason="s4_not_completed",
        )

    return EnrichmentRunResult(
        vendor_id=vendor_id,
        workflow_id=workflow_id,
        status=outcome.status,  # BLOCKED or FAILED
        profile_status=profile_result.profile_status if profile_result else None,
        overall_confidence=profile_result.overall_confidence if profile_result else None,
        flags=profile_result.flags if profile_result else [],
        triage_tasks=profile_result.triage_tasks if profile_result else [],
        brain_update_suggestions=(
            profile_result.brain_update_suggestions if profile_result else []
        ),
        pcs_before=pcs_before,
        pcs_after=profile_result.pcs_after if profile_result else pcs_before,
        error=reason,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Programme-level file writes
# ---------------------------------------------------------------------------

def _write_programme_files(
    programme_id: str,
    workspace: Path,
    result: EnrichmentRunResult,
    now: str,
) -> None:
    """Append per-run summary to programme-level ledger files."""
    run_dir = workspace / programme_id / "programme_run"

    log_entry = {
        "vendor_id":      result.vendor_id,
        "workflow_id":    result.workflow_id,
        "status":         result.status,
        "profile_status": result.profile_status,
        "pcs_before":     result.pcs_before,
        "pcs_after":      result.pcs_after,
        "flags":          result.flags,
        "enriched_at":    now,
        "error":          result.error,
    }
    log_yaml = yaml.dump(
        {"enrichment_log_entry": log_entry}, default_flow_style=False, allow_unicode=True
    )
    append_md(run_dir / "enrichment_log.md", f"---\n{log_yaml}---", programme_id=programme_id)

    if result.brain_update_suggestions:
        queue_entry = {
            "vendor_id":      result.vendor_id,
            "queued_at":      now,
            "pending_updates": [s.to_dict() for s in result.brain_update_suggestions],
        }
        queue_yaml = yaml.dump(
            {"brain_update_queue_entry": queue_entry},
            default_flow_style=False,
            allow_unicode=True,
        )
        append_md(
            run_dir / "brain_update_queue.md",
            f"---\n{queue_yaml}---",
            programme_id=programme_id,
        )

    if result.triage_tasks:
        triage_entry = {
            "vendor_id":   result.vendor_id,
            "queued_at":   now,
            "triage_items": result.triage_tasks,
        }
        triage_yaml = yaml.dump(
            {"triage_queue_entry": triage_entry}, default_flow_style=False, allow_unicode=True
        )
        append_md(
            run_dir / "triage_queue.md",
            f"---\n{triage_yaml}---",
            programme_id=programme_id,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_enrichment(
    vendor_id: str,
    programme_id: str,
    declared_depth: str = "STANDARD",
    workspace_root: Path | None = None,
    manual_override: bool = False,
    triggers: list[str] | None = None,
    brain: object = None,
) -> EnrichmentRunResult:
    """Process 2 entry point.  Never raises — always returns EnrichmentRunResult."""
    workspace = _resolve_workspace_root(workspace_root)
    now = _now_utc_iso()

    entity_data = _read_entity_md(programme_id, vendor_id, workspace)
    pcs_before = _read_pcs_from_coverage(programme_id, vendor_id, workspace)

    # ----------------------------------------------------------------
    # Gate: enrichment readiness check (outside the workflow)
    # ----------------------------------------------------------------
    try:
        readiness = check_enrichment_readiness(
            vendor_id=vendor_id,
            programme_id=programme_id,
            declared_depth=declared_depth,
            workspace_root=workspace,
            manual_override=manual_override,
            triggers=triggers,
        )
    except EnrichmentReadinessReadError as exc:
        logger.error(
            "EnrichmentReadinessReadError for %s/%s: %s", programme_id, vendor_id, exc
        )
        result = EnrichmentRunResult(
            vendor_id=vendor_id,
            workflow_id=None,
            status="FAILED",
            profile_status=None,
            overall_confidence=None,
            flags=["READINESS_READ_ERROR"],
            triage_tasks=[],
            brain_update_suggestions=[],
            pcs_before=pcs_before,
            pcs_after=pcs_before,
            error=str(exc),
            reason="readiness_read_error",
        )
        _write_programme_files(programme_id, workspace, result, now)
        return result

    # ----------------------------------------------------------------
    # Skip
    # ----------------------------------------------------------------
    if readiness.skip:
        logger.info(
            "Skipping enrichment for %s/%s: %s", programme_id, vendor_id, readiness.skip_reason
        )
        result = EnrichmentRunResult(
            vendor_id=vendor_id,
            workflow_id=None,
            status="SKIPPED",
            profile_status=None,
            overall_confidence=None,
            flags=readiness.flags,
            triage_tasks=[],
            brain_update_suggestions=[],
            pcs_before=pcs_before,
            pcs_after=pcs_before,
            error=None,
            reason=readiness.skip_reason,
        )
        _write_programme_files(programme_id, workspace, result, now)
        return result

    # ----------------------------------------------------------------
    # Create ENRICHMENT workflow via PlanningAgent
    # ----------------------------------------------------------------
    planning_agent = PlanningAgent()
    try:
        workflow = planning_agent.create_workflow(
            profile=None,
            workflow_type="ENRICHMENT",
            programme_id=programme_id,
            context_overrides={"vendor_id": vendor_id, "depth_tier": readiness.depth_tier},
        )
    except Exception as exc:
        logger.error(
            "Failed to create enrichment workflow for %s/%s: %s", programme_id, vendor_id, exc
        )
        result = EnrichmentRunResult(
            vendor_id=vendor_id,
            workflow_id=None,
            status="FAILED",
            profile_status=None,
            overall_confidence=None,
            flags=["WORKFLOW_CREATION_FAILED"],
            triage_tasks=[],
            brain_update_suggestions=[],
            pcs_before=pcs_before,
            pcs_after=pcs_before,
            error=str(exc),
            reason="workflow_creation_failed",
        )
        _write_programme_files(programme_id, workspace, result, now)
        return result

    workflow_id = workflow.workflow_id

    # ----------------------------------------------------------------
    # Build step registry and execute via RuntimeEngine
    # ----------------------------------------------------------------
    run_cache: dict = {}
    step_registry = _build_enrichment_step_registry(
        readiness=readiness,
        entity_data=entity_data,
        pcs_before=pcs_before,
        workspace_root=workspace,
        programme_id=programme_id,
        vendor_id=vendor_id,
        brain=brain,
        run_cache=run_cache,
    )

    engine = RuntimeEngine(
        planner=planning_agent,
        step_registry=step_registry,
        plan_renderer=None,
    )
    outcome = engine.execute_workflow(workflow_id=workflow_id, programme_id=programme_id)

    result = _assemble_result(vendor_id, workflow_id, outcome, pcs_before, run_cache)
    _write_programme_files(programme_id, workspace, result, now)
    return result


# ---------------------------------------------------------------------------
# Batch wrapper
# ---------------------------------------------------------------------------

def _read_confirmed_vendor_ids(register_path: Path) -> list[str]:
    """Return vendor_ids from vendor_register.md (all entries are CONFIRMED)."""
    if not register_path.exists():
        return []
    data = read_md(register_path)
    if not data:
        return []
    vendors = data.get("vendors") or []
    return [str(v["vendor_id"]) for v in vendors if v.get("vendor_id")]


def _log_batch_summary(results: list[EnrichmentRunResult]) -> None:
    by_status: dict[str, int] = {}
    by_profile_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        ps = r.profile_status or "—"
        by_profile_status[ps] = by_profile_status.get(ps, 0) + 1
    logger.info("=" * 60)
    logger.info("Enrichment batch summary")
    logger.info("  Total: %d", len(results))
    for status, count in sorted(by_status.items()):
        logger.info("  Status %s: %d", status, count)
    logger.info("  ---")
    for ps, count in sorted(by_profile_status.items()):
        logger.info("  Profile %s: %d", ps, count)
    logger.info("=" * 60)


def enrich_all_confirmed(
    programme_id: str,
    workspace_root: Path | None = None,
    declared_depth: str = "STANDARD",
    *,
    skip_already_enriched: bool = True,
    max_vendors: int | None = None,
    on_progress: Callable[[str, str, EnrichmentRunResult], None] | None = None,
) -> list[EnrichmentRunResult]:
    """Run Process 2 enrichment for every CONFIRMED vendor in the programme.

    Reads vendor_register.md, extracts all vendor_ids (all entries are
    CONFIRMED), and calls run_enrichment() for each.  Never raises — each
    vendor's run is wrapped in try/except so a single failure cannot abort
    the batch.
    """
    workspace = _resolve_workspace_root(workspace_root)
    register_path = workspace / programme_id / "programme_run" / "vendor_register.md"

    confirmed_vendor_ids = _read_confirmed_vendor_ids(register_path)
    if max_vendors is not None:
        confirmed_vendor_ids = confirmed_vendor_ids[:max_vendors]

    if not confirmed_vendor_ids:
        logger.warning("No CONFIRMED vendors found in %s", register_path)
        return []

    logger.info(
        "Enriching %d CONFIRMED vendors for programme %s",
        len(confirmed_vendor_ids), programme_id,
    )

    results: list[EnrichmentRunResult] = []
    for idx, vendor_id in enumerate(confirmed_vendor_ids, start=1):
        logger.info("[%d/%d] enriching %s ...", idx, len(confirmed_vendor_ids), vendor_id)
        try:
            result = run_enrichment(
                vendor_id=vendor_id,
                programme_id=programme_id,
                declared_depth=declared_depth,
                workspace_root=workspace,
                manual_override=not skip_already_enriched,
            )
        except Exception as exc:
            logger.exception("Unexpected error enriching %s", vendor_id)
            result = EnrichmentRunResult(
                vendor_id=vendor_id,
                workflow_id=None,
                status="FAILED",
                profile_status=None,
                overall_confidence=None,
                flags=["BATCH_WRAPPER_ERROR"],
                triage_tasks=[],
                brain_update_suggestions=[],
                pcs_before=0.0,
                pcs_after=0.0,
                error=str(exc),
                reason="batch_wrapper_unexpected_failure",
            )

        results.append(result)
        logger.info(
            "[%d/%d] %s → %s (profile=%s, conf=%s)",
            idx, len(confirmed_vendor_ids), vendor_id,
            result.status, result.profile_status, result.overall_confidence,
        )
        if on_progress is not None:
            try:
                on_progress(vendor_id, result.status, result)
            except Exception:
                logger.exception("on_progress callback raised")

    _log_batch_summary(results)
    return results
