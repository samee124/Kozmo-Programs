"""Process 3 (Relationship & Spend) orchestrator.

Wires the 5 P3 tools end-to-end via the RuntimeEngine. Handles gate checks,
crash recovery, and SKIP / BLOCKED / FAILED routing.

Entry point: run_rs() / run_rs_all_confirmed()
Never raises — always returns RSRunResult.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from cobalt.core.atomic_write import append_md
from cobalt.core.file_system import (
    _find_vendor_file,
    entity_path,
    programme_run_path,
)
from cobalt.core.staleness import is_stale
from cobalt.models.schemas.rs_schema import (
    DocumentIntelligenceResult,
    RSRunResult,
    RSRunStatus,
    RelationshipClassification,
    SpendAggregationResult,
    StructuredDataBundle,
)
from cobalt.runtime.execution_state import ExecutionState
from cobalt.runtime.runtime_engine import (
    ReplanDecision,
    RuntimeEngine,
    StepFatal,
    WorkflowOutcome,
)
from cobalt.runtime.workflow_definition import (
    RetryPolicy,
    StepStatus,
    WorkflowDefinition,
    WorkflowStep,
)
from cobalt.tools import (
    document_intelligence,
    relationship_classifier,
    rs_profile_assembler,
    spend_aggregator,
    structured_data_collector,
)

logger = logging.getLogger(__name__)

# Maximum age (days) before a profile is considered stale and re-run
PROFILE_MAX_AGE_DAYS = 30


# ---------------------------------------------------------------------------
# Passthrough planner — P3 workflow doesn't use LLM-based replanning
# ---------------------------------------------------------------------------

class _RSPlanner:
    """Minimal PlannerProtocol implementation for P3 workflows.

    P3 is a linear pipeline with no conditional branching or replanning.
    Always returns CONTINUE so the RuntimeEngine runs every step in sequence.
    """

    def evaluate_step(
        self,
        step: WorkflowStep,
        result: dict,
        state: ExecutionState,
    ) -> ReplanDecision:
        return ReplanDecision(action="CONTINUE", reason="rs_passthrough")

    def replan(
        self,
        workflow: WorkflowDefinition,
        completed_step: WorkflowStep,
        step_result: dict,
        accumulated_signals: dict,
    ) -> list[WorkflowStep]:
        return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_md_frontmatter(path: Path) -> dict:
    """Read YAML frontmatter from a .md file. Returns {} on error."""
    if not path.exists():
        return {}
    try:
        import yaml
        content = path.read_text(encoding="utf-8")
        parts = content.split("---\n", 2)
        if len(parts) >= 3:
            return yaml.safe_load(parts[1]) or {}
        return yaml.safe_load(content) or {}
    except Exception:
        return {}


def _read_pcs(programme_id: str, vendor_id: str) -> float:
    """Read current PCS from consolidated profile's relationship: key. Returns 0.0 if unavailable."""
    from cobalt.core.file_system import _find_vendor_file
    vf = _find_vendor_file(programme_id, vendor_id)
    if vf and vf.exists():
        data = _read_md_frontmatter(vf)
        rel = data.get("relationship") or {}
        pcs_total = rel.get("pcs_total")
        if pcs_total is not None:
            return float(pcs_total)
        pcs = data.get("pcs") or {}
        score = pcs.get("score")
        if score is not None:
            return float(score) / 100.0
    return 0.0


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------

def _check_gates(
    vendor_id: str,
    programme_id: str,
    uploaded_files: list[dict] | None,
    checkin_data: dict | None,
    connector_config: dict | None,
) -> RSRunResult | None:
    """Run gate checks. Returns RSRunResult if run should be aborted, else None."""
    ep = entity_path(programme_id, vendor_id)

    # Gate 1: entity.md must exist with CONFIRMED status
    if not ep.exists():
        return RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.BLOCKED.value,
            pcs_before=None, pcs_after=None,
            tools_run=[], flags_raised=[],
            profile_status=None,
            skip_reason=None,
            error="entity_not_confirmed",
        )

    entity_data = _read_md_frontmatter(ep)
    status = (entity_data.get("intake") or {}).get("status") or entity_data.get("status", "")
    _REJECTED = {"TRIAGE", "TRIAGE_REQUIRED", "BLOCKED", "REJECTED", "UNRESOLVED"}
    if not status or status.upper() in _REJECTED:
        return RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.BLOCKED.value,
            pcs_before=None, pcs_after=None,
            tools_run=[], flags_raised=[],
            profile_status=None,
            skip_reason=None,
            error="entity_not_confirmed",
        )

    # Read consolidated profile for enrichment/relationship data
    vf = _find_vendor_file(programme_id, vendor_id)
    consolidated = _read_md_frontmatter(vf) if vf and vf.is_file() else {}

    # Gate 2: P2 enrichment data — warn only, do not block
    if not consolidated.get("enrichment"):
        logger.warning("P2 enrichment data missing for %s/%s — continuing without enrichment data", programme_id, vendor_id)

    # Gate 3: Data available
    has_data = bool(
        (uploaded_files and any(f.get("path") for f in uploaded_files))
        or checkin_data
        or (connector_config and _connector_dir_has_files(programme_id, vendor_id))
    )
    if not has_data:
        return RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.SKIPPED.value,
            pcs_before=_read_pcs(programme_id, vendor_id), pcs_after=None,
            tools_run=[], flags_raised=[],
            profile_status=None,
            skip_reason="no_data_available",
            error=None,
        )

    # Gate 4: Profile freshness (< 30 days old) — check relationship.last_updated
    rel_data = consolidated.get("relationship") or {}
    last_updated = rel_data.get("last_updated")
    if last_updated and not is_stale(last_updated, PROFILE_MAX_AGE_DAYS):
        return RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.SKIPPED.value,
            pcs_before=_read_pcs(programme_id, vendor_id), pcs_after=None,
            tools_run=[], flags_raised=[],
            profile_status=None,
            skip_reason="profile_fresh",
            error=None,
        )

    return None  # All gates passed


def _connector_dir_has_files(programme_id: str, vendor_id: str) -> bool:
    from cobalt.core.file_system import connectors_path
    d = connectors_path(programme_id, vendor_id)
    return d.is_dir() and bool(list(d.glob("*.json")))


# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------

def _build_rs_step_registry(
    vendor_id: str,
    programme_id: str,
    arrival_modes: list[str] | None,
    uploaded_files: list[dict] | None,
    checkin_data: dict | None,
    connector_config: dict | None,
    document_paths: list[str],
    entity_profile: dict,
    known_facts: dict,
    current_pcs: float,
    run_cache: dict,
) -> dict:
    """Build step adapters with closures for crash recovery."""

    def _collect_rs_data_step(workflow, state, step) -> dict:
        bundle = structured_data_collector.collect_structured_data(
            vendor_id=vendor_id,
            programme_id=programme_id,
            arrival_modes=arrival_modes,
            connector_config=connector_config,
            uploaded_files=uploaded_files,
            checkin_data=checkin_data,
        )
        run_cache["structured_bundle"] = bundle
        return {"structured_bundle": bundle.to_dict()}

    def _process_documents_step(workflow, state, step) -> dict:
        result = document_intelligence.process_documents(
            vendor_id=vendor_id,
            programme_id=programme_id,
            document_paths=document_paths,
        )
        run_cache["doc_intelligence"] = result
        return {"doc_intelligence": result.to_dict()}

    def _aggregate_spend_step(workflow, state, step) -> dict:
        bundle = run_cache.get("structured_bundle")
        if bundle is None:
            s1 = state.completed_steps.get("s1_collect")
            if s1 and s1.result:
                bundle = StructuredDataBundle.from_dict(s1.result["structured_bundle"])
                run_cache["structured_bundle"] = bundle
        if bundle is None:
            raise StepFatal("s1_collect result missing — cannot aggregate spend")

        doc_intel = run_cache.get("doc_intelligence")
        if doc_intel is None:
            s2 = state.completed_steps.get("s2_documents")
            if s2 and s2.result:
                doc_intel = DocumentIntelligenceResult.from_dict(s2.result["doc_intelligence"])
                run_cache["doc_intelligence"] = doc_intel
        if doc_intel is None:
            raise StepFatal("s2_documents result missing — cannot aggregate spend")

        result = spend_aggregator.aggregate_spend(
            vendor_id=vendor_id,
            raw_records=bundle.raw_spend_records,
            contract_terms=doc_intel.extracted_contracts,
        )
        run_cache["spend_aggregation"] = result
        return {"spend_aggregation": result.to_dict()}

    def _classify_relationship_step(workflow, state, step) -> dict:
        agg = run_cache.get("spend_aggregation")
        if agg is None:
            s3 = state.completed_steps.get("s3_aggregate")
            if s3 and s3.result:
                agg = SpendAggregationResult.from_dict(s3.result["spend_aggregation"])
                run_cache["spend_aggregation"] = agg
        if agg is None:
            raise StepFatal("s3_aggregate result missing — cannot classify relationship")

        doc_intel = run_cache.get("doc_intelligence")
        if doc_intel is None:
            s2 = state.completed_steps.get("s2_documents")
            if s2 and s2.result:
                doc_intel = DocumentIntelligenceResult.from_dict(s2.result["doc_intelligence"])
                run_cache["doc_intelligence"] = doc_intel
        if doc_intel is None:
            raise StepFatal("s2_documents result missing — cannot classify relationship")

        result = relationship_classifier.classify_relationship(
            vendor_id=vendor_id,
            spend_summary=agg.summary,
            contract_terms=doc_intel.extracted_contracts,
            entity_profile=entity_profile,
            known_facts=known_facts,
        )
        run_cache["classification"] = result
        return {"classification": result.to_dict()}

    def _assemble_rs_profile_step(workflow, state, step) -> dict:
        bundle = run_cache.get("structured_bundle")
        if bundle is None:
            s1 = state.completed_steps.get("s1_collect")
            if s1 and s1.result:
                bundle = StructuredDataBundle.from_dict(s1.result["structured_bundle"])
        if bundle is None:
            raise StepFatal("s1_collect result missing — cannot assemble profile")

        doc_intel = run_cache.get("doc_intelligence")
        if doc_intel is None:
            s2 = state.completed_steps.get("s2_documents")
            if s2 and s2.result:
                doc_intel = DocumentIntelligenceResult.from_dict(s2.result["doc_intelligence"])
        if doc_intel is None:
            raise StepFatal("s2_documents result missing — cannot assemble profile")

        agg = run_cache.get("spend_aggregation")
        if agg is None:
            s3 = state.completed_steps.get("s3_aggregate")
            if s3 and s3.result:
                agg = SpendAggregationResult.from_dict(s3.result["spend_aggregation"])
        if agg is None:
            raise StepFatal("s3_aggregate result missing — cannot assemble profile")

        cls = run_cache.get("classification")
        if cls is None:
            s4 = state.completed_steps.get("s4_classify")
            if s4 and s4.result:
                cls = RelationshipClassification.from_dict(s4.result["classification"])
        if cls is None:
            raise StepFatal("s4_classify result missing — cannot assemble profile")

        profile = rs_profile_assembler.assemble_rs_profile(
            vendor_id=vendor_id,
            programme_id=programme_id,
            structured_bundle=bundle,
            doc_intelligence=doc_intel,
            spend_aggregation=agg,
            classification=cls,
            current_pcs=current_pcs,
        )
        run_cache["profile"] = profile
        return {
            "profile_status":    profile.profile_status,
            "pcs_contribution":  profile.pcs_contribution,
            "pcs_total":         profile.pcs_total,
            "flags":             profile.flags,
        }

    return {
        "COLLECT_RS_DATA":       _collect_rs_data_step,
        "PROCESS_DOCUMENTS":     _process_documents_step,
        "AGGREGATE_SPEND":       _aggregate_spend_step,
        "CLASSIFY_RELATIONSHIP": _classify_relationship_step,
        "ASSEMBLE_RS_PROFILE":   _assemble_rs_profile_step,
    }


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

def _build_rs_workflow(vendor_id: str, programme_id: str, context: dict) -> WorkflowDefinition:
    """Build and persist a WorkflowDefinition for the P3 pipeline."""
    workflow_id = f"wf-rs-{vendor_id}-{int(time.time())}"
    steps = [
        WorkflowStep(
            step_id="s1_collect",
            step_type="COLLECT_RS_DATA",
            status=StepStatus.PENDING,
            depends_on=[],
            condition=None,
            retry_policy=RetryPolicy(),
            planning_rationale="Collect structured spend data from all configured sources.",
        ),
        WorkflowStep(
            step_id="s2_documents",
            step_type="PROCESS_DOCUMENTS",
            status=StepStatus.PENDING,
            depends_on=["s1_collect"],
            condition=None,
            retry_policy=RetryPolicy(),
            planning_rationale="Extract contract terms from uploaded documents.",
        ),
        WorkflowStep(
            step_id="s3_aggregate",
            step_type="AGGREGATE_SPEND",
            status=StepStatus.PENDING,
            depends_on=["s1_collect", "s2_documents"],
            condition=None,
            retry_policy=RetryPolicy(),
            planning_rationale="Aggregate raw spend records into a spend summary.",
        ),
        WorkflowStep(
            step_id="s4_classify",
            step_type="CLASSIFY_RELATIONSHIP",
            status=StepStatus.PENDING,
            depends_on=["s3_aggregate"],
            condition=None,
            retry_policy=RetryPolicy(),
            planning_rationale="Classify vendor relationship type and dependency tier.",
        ),
        WorkflowStep(
            step_id="s5_assemble",
            step_type="ASSEMBLE_RS_PROFILE",
            status=StepStatus.PENDING,
            depends_on=["s1_collect", "s2_documents", "s3_aggregate", "s4_classify"],
            condition=None,
            retry_policy=RetryPolicy(),
            planning_rationale="Assemble and write the relationship_spend_profile.md.",
        ),
    ]
    return WorkflowDefinition(
        workflow_id=workflow_id,
        programme_id=programme_id,
        vendor_key=None,
        vendor_id=vendor_id,
        workflow_type="RS_DATA_GATHERING",
        created_by="rs_orchestrator",
        created_at=_now_iso(),
        context=context,
        steps=steps,
    )


# ---------------------------------------------------------------------------
# Vendor-level plan write
# ---------------------------------------------------------------------------

def _write_rs_plan(
    programme_id: str,
    vendor_id: str,
    result: RSRunResult,
    profile: object,
    now: str,
) -> None:
    """Write plans/rs_plan.md into the vendor workspace (P3)."""
    try:
        import os as _os
        import yaml as _yaml
        from cobalt.core.atomic_write import atomic_write
        from cobalt.core.file_system import vendor_path

        plans_dir = vendor_path(programme_id, vendor_id) / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        path = plans_dir / "rs_plan.md"

        rel_type      = getattr(profile, "relationship_type", None) or "UNKNOWN"
        dep_tier      = getattr(profile, "dependency_tier", None) or "UNKNOWN"
        dep_score     = getattr(profile, "dependency_score", None)
        contract_cov  = getattr(profile, "contract_coverage", None) or "UNCOVERED"
        single_source = getattr(profile, "single_source_risk", False)

        fm: dict = {
            "plan_type":          "rs",
            "vendor_id":          vendor_id,
            "relationship_type":  rel_type,
            "dependency_tier":    dep_tier,
            "dependency_score":   dep_score,
            "contract_coverage":  contract_cov,
            "single_source_risk": single_source,
            "profile_status":     result.profile_status,
            "pcs_before":         result.pcs_before,
            "pcs_after":          result.pcs_after,
            "flags":              result.flags_raised,
            "planned_at":         now,
        }
        fm_yaml = _yaml.dump(fm, default_flow_style=False, allow_unicode=True)
        body = (
            f"\n# Relationship & Spend Plan — {vendor_id}\n\n"
            f"Relationship: **{rel_type}**  "
            f"Dependency tier: **{dep_tier}**  "
            f"Contract coverage: {contract_cov}\n"
        )
        atomic_write(path, f"---\n{fm_yaml}---\n{body}", vendor_id=vendor_id, programme_id=programme_id)
    except Exception as exc:
        logger.warning("rs_plan write failed for %s/%s: %s", programme_id, vendor_id, exc)


# ---------------------------------------------------------------------------
# Programme-level outputs
# ---------------------------------------------------------------------------

def _update_programme_logs(
    programme_id: str,
    vendor_id: str,
    result: RSRunResult,
) -> None:
    """Append to rs_log.md and optionally triage_queue.md."""
    try:
        log_path = programme_run_path(programme_id) / "rs_log.md"
        log_entry = (
            f"| {vendor_id} | {result.status} | "
            f"{result.pcs_before} | {result.pcs_after} | "
            f"{', '.join(result.flags_raised)} |\n"
        )
        append_md(log_path, log_entry, programme_id=programme_id)
    except Exception as exc:
        logger.warning("rs_log write failed for %s: %s", vendor_id, exc)

    triage_flags = {"CLASSIFICATION_INCOMPLETE", "PROFILE_ASSEMBLY_FAILED"}
    if any(f in result.flags_raised for f in triage_flags):
        try:
            triage_path = programme_run_path(programme_id) / "triage_queue.md"
            triage_entry = (
                f"| {vendor_id} | RS_TRIAGE | "
                f"{', '.join(f for f in result.flags_raised if f in triage_flags)} |\n"
            )
            append_md(triage_path, triage_entry, programme_id=programme_id)
        except Exception as exc:
            logger.warning("triage_queue write failed for %s: %s", vendor_id, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_rs(
    vendor_id: str,
    programme_id: str,
    arrival_modes: list[str] | None = None,
    uploaded_files: list[dict] | None = None,
    checkin_data: dict | None = None,
    connector_config: dict | None = None,
) -> RSRunResult:
    """Drive the full Process 3 pipeline for one vendor.

    Never raises — always returns RSRunResult.
    """
    pcs_before = _read_pcs(programme_id, vendor_id)

    # Gate checks
    gate_result = _check_gates(
        vendor_id, programme_id, uploaded_files, checkin_data, connector_config
    )
    if gate_result is not None:
        return gate_result

    # Load entity + known_facts (enrichment data is nested under entity_profile)
    entity_profile = _read_md_frontmatter(entity_path(programme_id, vendor_id))
    known_facts = entity_profile.get("enrichment") or {}

    # Extract document paths from uploaded_files
    document_paths = [
        f["path"] for f in (uploaded_files or [])
        if f.get("path")
    ]

    run_cache: dict = {}
    context = {
        "arrival_modes":     arrival_modes,
        "uploaded_files":    uploaded_files,
        "checkin_data":      checkin_data,
        "connector_config":  connector_config,
        "document_paths":    document_paths,
        "entity_profile":    entity_profile,
        "known_facts":       known_facts,
        "current_pcs":       pcs_before,
    }

    workflow_def = _build_rs_workflow(vendor_id, programme_id, context)
    workflow_id = workflow_def.workflow_id

    try:
        workflow_def.save()
    except Exception as exc:
        logger.error("Failed to save RS workflow for %s/%s: %s", programme_id, vendor_id, exc)
        result = RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.FAILED.value,
            pcs_before=pcs_before, pcs_after=pcs_before,
            tools_run=[],
            flags_raised=["PROFILE_ASSEMBLY_FAILED"],
            profile_status="FAILED",
            skip_reason=None,
            error=f"workflow_save_error: {exc}",
        )
        _update_programme_logs(programme_id, vendor_id, result)
        return result

    step_registry = _build_rs_step_registry(
        vendor_id=vendor_id,
        programme_id=programme_id,
        arrival_modes=arrival_modes,
        uploaded_files=uploaded_files,
        checkin_data=checkin_data,
        connector_config=connector_config,
        document_paths=document_paths,
        entity_profile=entity_profile,
        known_facts=known_facts,
        current_pcs=pcs_before,
        run_cache=run_cache,
    )

    try:
        engine = RuntimeEngine(planner=_RSPlanner(), step_registry=step_registry)
        outcome: WorkflowOutcome = engine.execute_workflow(
            workflow_id=workflow_id,
            programme_id=programme_id,
        )
    except Exception as exc:
        logger.exception("RuntimeEngine failed for RS %s/%s: %s", programme_id, vendor_id, exc)
        result = RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.FAILED.value,
            pcs_before=pcs_before, pcs_after=pcs_before,
            tools_run=[],
            flags_raised=["PROFILE_ASSEMBLY_FAILED"],
            profile_status="FAILED",
            skip_reason=None,
            error=str(exc),
        )
        _update_programme_logs(programme_id, vendor_id, result)
        return result

    # Assemble result
    profile = run_cache.get("profile")
    # Determine which tools ran from run_cache keys (populated by step adapters)
    _cache_to_step = {
        "structured_bundle": "s1_collect",
        "doc_intelligence":  "s2_documents",
        "spend_aggregation": "s3_aggregate",
        "classification":    "s4_classify",
        "profile":           "s5_assemble",
    }
    tools_run = [step for key, step in _cache_to_step.items() if key in run_cache]

    if outcome.status == "COMPLETED" and profile is not None:
        result = RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.COMPLETED.value,
            pcs_before=pcs_before,
            pcs_after=profile.pcs_total,
            tools_run=tools_run,
            flags_raised=profile.flags,
            profile_status=profile.profile_status,
            skip_reason=None,
            error=None,
        )
    else:
        error_msg = (outcome.outcome or {}).get("error") if outcome.outcome else str(outcome.status)
        result = RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.FAILED.value,
            pcs_before=pcs_before,
            pcs_after=pcs_before,
            tools_run=tools_run,
            flags_raised=[],
            profile_status="FAILED",
            skip_reason=None,
            error=error_msg,
        )

    _update_programme_logs(programme_id, vendor_id, result)
    if result.status == RSRunStatus.COMPLETED.value and profile is not None:
        _write_rs_plan(programme_id, vendor_id, result, profile, _now_iso())
    return result


def _read_vendor_ids_from_register(programme_id: str) -> list[str]:
    """Read vendor_ids from vendor_register.md (same source as enrichment)."""
    from cobalt.core.file_system import programme_run_path, read_md
    register_path = programme_run_path(programme_id) / "vendor_register.md"
    if not register_path.exists():
        return []
    data = read_md(register_path)
    if not data:
        return []
    vendors = data.get("vendors") or []
    return [str(v["vendor_id"]) for v in vendors if v.get("vendor_id")]


def run_rs_all_confirmed(
    programme_id: str,
    **kwargs,
) -> list[RSRunResult]:
    """Run Process 3 for every CONFIRMED vendor in the programme.

    Reads from vendor_register.md (same source as enrichment orchestrator).
    Falls back to DB query if register is missing. Sequential — failures in
    one vendor do not affect others.
    """
    vendor_ids = _read_vendor_ids_from_register(programme_id)
    if not vendor_ids:
        logger.warning("vendor_register.md empty or missing for %s — falling back to DB", programme_id)
        try:
            from cobalt.db.queries import get_confirmed_vendors
            vendor_ids = get_confirmed_vendors(programme_id)
        except Exception as exc:
            logger.warning("Could not query confirmed vendors for %s: %s", programme_id, exc)
            vendor_ids = []

    if not vendor_ids:
        logger.warning("No vendors found for RS pipeline — programme %s", programme_id)
        return []

    logger.info("RS pipeline: processing %d vendors for programme %s", len(vendor_ids), programme_id)
    results: list[RSRunResult] = []
    for idx, vendor_id in enumerate(vendor_ids, start=1):
        logger.info("[%d/%d] RS processing %s ...", idx, len(vendor_ids), vendor_id)
        result = run_rs(vendor_id, programme_id, **kwargs)
        results.append(result)

    return results
