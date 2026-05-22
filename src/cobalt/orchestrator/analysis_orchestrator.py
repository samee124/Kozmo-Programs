"""Process 4 (Analysis & Intelligence) orchestrator.

Wires the 7 P4 tools end-to-end in strict sequential order. Handles gate
checks, crash-resistant step execution, and SKIP / BLOCKED / FAILED routing.

Entry point: run_analysis() / run_analysis_all_confirmed()
Never raises — always returns ANRunResult.

Step order (enforced):
  s1_validate → s2_commercial → s3_inquire → s4_score →
  s5_trend → s6_findings → s7_narrative
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from cobalt.core.atomic_write import append_md, atomic_write
from cobalt.core.file_system import (
    _find_vendor_file,
    entity_path,
    ledger_path,
    programme_run_path,
    vendor_path,
)
from cobalt.core.pcs import compute_pcs
from cobalt.core.staleness import is_stale
from cobalt.core.state_classifier import classify_vendor_state
from cobalt.core.triage import generate_triage_tasks
from cobalt.models.schemas.an_schema import (
    ANRunResult,
    ANRunStatus,
    ActionOutcomeHistory,
    CommercialAnalysisResult,
    FindingsBundle,
    HistoricalCommercialState,
    HistoricalEvidenceState,
    HistoricalQAState,
    HistoricalScoreState,
    NarrativeBundle,
    QAPair,
    ScoreBundle,
    ScoringConfig,
    TrendReport,
    ValidatedEvidenceAssembly,
)
from cobalt.tools import (
    commercial_analyser,
    evidence_validator,
    finding_engine,
    inquiry_engine,
    narrative_engine,
    scoring_engine,
    trend_analyser,
)

logger = logging.getLogger(__name__)

ANALYSIS_MAX_AGE_DAYS = 30


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_md_frontmatter(path: Path) -> dict:
    """Read YAML frontmatter from a .md file. Returns {} on error."""
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
        parts = content.split("---\n", 2)
        if len(parts) >= 3:
            return yaml.safe_load(parts[1]) or {}
        return yaml.safe_load(content) or {}
    except Exception:
        return {}


def _load_history_json(path: Path, cls):
    """Load a history JSON file and return a typed object, or None if missing/invalid."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)
    except Exception:
        return None


def _read_pcs(programme_id: str, vendor_id: str) -> float:
    """Read current PCS from consolidated profile's relationship: key. Returns 0.0 if unavailable."""
    vf = _find_vendor_file(programme_id, vendor_id)
    if vf and vf.exists():
        data = _read_md_frontmatter(vf)
        rel = data.get("relationship") or {}
        val = rel.get("pcs_total")
        if val is not None:
            return float(val)
    return 0.0


def _default_scoring_config() -> ScoringConfig:
    return ScoringConfig(
        dimension_weights={
            "delivery_reliability": 0.25,
            "responsiveness":       0.20,
            "commercial_value":     0.25,
            "risk_compliance":      0.20,
            "relationship_trend":   0.10,
        },
        health_band_thresholds={
            "HEALTHY":  80,
            "WATCH":    65,
            "AT_RISK":  50,
            "CRITICAL":  0,
        },
        tier_cri_thresholds={
            "STRATEGIC":     70,
            "PREFERRED":     60,
            "TRANSACTIONAL": 50,
        },
        spike_multiplier=1.5,
    )


def _load_rs_profile_object(programme_id: str, vendor_id: str):
    """Load relationship data from the consolidated profile and return a RelationshipSpendProfile.

    Contract terms are not stored in the frontmatter — defaults to [].
    Relationship classification fields are reconstructed from the relationship: key.
    """
    from cobalt.models.schemas.rs_schema import RelationshipSpendProfile

    vf_path = _find_vendor_file(programme_id, vendor_id)
    all_data = _read_md_frontmatter(vf_path) if vf_path and vf_path.exists() else {}
    data = all_data.get("relationship") or {}

    full_data = {
        "vendor_id":       data.get("vendor_id") or vendor_id,
        "programme_id":    data.get("programme_id") or programme_id,
        "profile_version": data.get("profile_version") or 1,
        "profile_status":  data.get("profile_status") or "COMPLETE",
        "created_at":      data.get("created_at") or "",
        "last_updated":    data.get("last_updated") or "",
        "contract_count":  data.get("contract_count") or 0,
        "spend_summary": {
            "total_usd_all_time": data.get("spend_total_ttm_usd"),
            "total_usd_ttm":      data.get("spend_total_ttm_usd"),
            "data_completeness":  "PARTIAL",
            "confidence":         "MEDIUM",
        },
        "contract_terms": [],  # not persisted in frontmatter
        "relationship_classification": {
            "vendor_id":                 vendor_id,
            "relationship_type":         data.get("relationship_type") or "TRANSACTIONAL",
            "dependency_score":          0.0,
            "dependency_tier":           data.get("dependency_tier"),
            "single_source_risk":        False,
            "contract_coverage":         "PARTIAL",
            "renewal_urgency":           "NORMAL",
            "relationship_age_days":     None,
            "classification_confidence": "MEDIUM",
            "llm_used":                  False,
            "reasoning":                 None,
        },
        "gap_report":       {},
        "pcs_contribution": data.get("pcs_contribution") or 0.0,
        "pcs_total":        data.get("pcs_total") or 0.0,
        "flags":            data.get("flags") or [],
        "data_sources":     [],
    }
    return RelationshipSpendProfile.from_dict(full_data)


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------

def _check_gates(
    vendor_id: str,
    programme_id: str,
    force: bool,
) -> ANRunResult | None:
    """Run gate checks in order. Returns ANRunResult if run should abort, else None."""

    # Gate 1: vendor workspace file must exist.
    # Status is NOT checked strictly here — after P1 enrichment the status
    # becomes PARTIALLY_ENRICHED / ENRICHED, but the vendor is already
    # confirmed by being in vendor_register.md. Only block explicit failures.
    ep = entity_path(programme_id, vendor_id)
    if not ep.exists():
        logger.warning("Gate 1 BLOCKED: no entity file for %s/%s", programme_id, vendor_id)
        return ANRunResult(
            vendor_id=vendor_id, programme_id=programme_id,
            status=ANRunStatus.BLOCKED.value,
            cri_score=None, health_band=None,
            finding_count=0, nba_action=None,
            pcs_before=None, pcs_after=None,
            tools_run=[], skip_reason=None,
            error="entity_file_missing",
            analysed_at=_now_iso(),
        )

    entity_data = _read_md_frontmatter(ep) or {}
    # Block unconfirmed/failed intake statuses.
    # Reads intake.status (single-file arch), intake_status (old multi-file), and
    # top-level status (old entity.md arch where status=CONFIRMED/PENDING).
    # Does NOT block PARTIALLY_ENRICHED/ENRICHED — those are post-intake statuses.
    _BLOCKED_STATUSES = frozenset({"TRIAGE", "DISCARDED", "FAILED", "BLOCKED", "PENDING"})
    raw_status = (
        (entity_data.get("intake") or {}).get("status")
        or entity_data.get("intake_status")
        or entity_data.get("status", "")
    )
    if raw_status and raw_status.upper() in _BLOCKED_STATUSES:
        logger.warning("Gate 1 BLOCKED: vendor %s has intake status %s", vendor_id, raw_status)
        return ANRunResult(
            vendor_id=vendor_id, programme_id=programme_id,
            status=ANRunStatus.BLOCKED.value,
            cri_score=None, health_band=None,
            finding_count=0, nba_action=None,
            pcs_before=None, pcs_after=None,
            tools_run=[], skip_reason=None,
            error=f"intake_status_{raw_status.lower()}",
            analysed_at=_now_iso(),
        )

    # Gate 2: consolidated profile must have relationship: data (P3 must be complete)
    profile_path = _find_vendor_file(programme_id, vendor_id)
    if profile_path is None or not profile_path.exists():
        return ANRunResult(
            vendor_id=vendor_id, programme_id=programme_id,
            status=ANRunStatus.BLOCKED.value,
            cri_score=None, health_band=None,
            finding_count=0, nba_action=None,
            pcs_before=None, pcs_after=None,
            tools_run=[], skip_reason=None,
            error="rs_profile_missing",
            analysed_at=_now_iso(),
        )
    profile_data = _read_md_frontmatter(profile_path)
    if not profile_data.get("relationship"):
        return ANRunResult(
            vendor_id=vendor_id, programme_id=programme_id,
            status=ANRunStatus.BLOCKED.value,
            cri_score=None, health_band=None,
            finding_count=0, nba_action=None,
            pcs_before=None, pcs_after=None,
            tools_run=[], skip_reason=None,
            error="rs_profile_missing",
            analysed_at=_now_iso(),
        )

    # Gate 3: enrichment data missing → warn only, do not block.
    if not profile_data.get("enrichment"):
        logger.warning(
            "enrichment data missing for %s/%s — continuing without P2 data",
            programme_id, vendor_id,
        )

    # Gate 4: Freshness check (skip unless force=True)
    if not force:
        result_md = vendor_path(programme_id, vendor_id) / "analysis_result.md"
        if result_md.exists():
            result_data = _read_md_frontmatter(result_md)
            last_analysed = result_data.get("last_analysed_at")
            if isinstance(last_analysed, datetime):
                last_analysed = last_analysed.isoformat()
            if last_analysed and not is_stale(last_analysed, ANALYSIS_MAX_AGE_DAYS):
                return ANRunResult(
                    vendor_id=vendor_id, programme_id=programme_id,
                    status=ANRunStatus.SKIPPED.value,
                    cri_score=result_data.get("cri_score"),
                    health_band=result_data.get("health_band"),
                    finding_count=result_data.get("finding_count") or 0,
                    nba_action=result_data.get("nba_action"),
                    pcs_before=_read_pcs(programme_id, vendor_id),
                    pcs_after=None,
                    tools_run=[],
                    skip_reason="analysis_fresh",
                    error=None,
                    analysed_at=last_analysed,
                )

    return None  # All gates passed


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------

def _load_context(vendor_id: str, programme_id: str) -> dict:
    """Load all input data for the P4 pipeline from workspace files."""
    from cobalt.core.file_system import _find_vendor_file

    history_dir = vendor_path(programme_id, vendor_id) / "history"

    # Vendor single-file (root .md)
    vf_path = _find_vendor_file(programme_id, vendor_id)
    vendor_file = _read_md_frontmatter(vf_path) if vf_path else {}

    # P3 intermediate outputs — optional, None if not persisted
    doc_intelligence = None
    doc_intel_path = vendor_path(programme_id, vendor_id) / "doc_intelligence.json"
    if doc_intel_path.exists():
        try:
            from cobalt.models.schemas.rs_schema import DocumentIntelligenceResult
            doc_intelligence = DocumentIntelligenceResult.from_dict(
                json.loads(doc_intel_path.read_text(encoding="utf-8"))
            )
        except Exception:
            pass

    structured_bundle = None
    sb_path = vendor_path(programme_id, vendor_id) / "structured_bundle.json"
    if sb_path.exists():
        try:
            from cobalt.models.schemas.rs_schema import StructuredDataBundle
            structured_bundle = StructuredDataBundle.from_dict(
                json.loads(sb_path.read_text(encoding="utf-8"))
            )
        except Exception:
            pass

    # Historical state — 5 JSON files
    historical_scores     = _load_history_json(history_dir / "score_history.json",    HistoricalScoreState)
    historical_qa         = _load_history_json(history_dir / "qa_history.json",       HistoricalQAState)
    historical_evidence   = _load_history_json(history_dir / "evidence_state.json",   HistoricalEvidenceState)
    historical_commercial = _load_history_json(history_dir / "commercial_state.json", HistoricalCommercialState)
    action_history        = _load_history_json(history_dir / "action_history.json",   ActionOutcomeHistory)

    return {
        "rs_profile":            _load_rs_profile_object(programme_id, vendor_id),
        "doc_intelligence":      doc_intelligence,
        "structured_bundle":     structured_bundle,
        "signal_bundle":         None,  # signal_processor not yet implemented
        "vendor_file":           vendor_file,
        "historical_scores":     historical_scores,
        "historical_qa":         historical_qa,
        "historical_evidence":   historical_evidence,
        "historical_commercial": historical_commercial,
        "action_history":        action_history,
    }


# ---------------------------------------------------------------------------
# Step execution — sequential, crash-resistant
# ---------------------------------------------------------------------------

def _run_steps(
    vendor_id: str,
    programme_id: str,
    context: dict,
    scoring_config: ScoringConfig,
) -> tuple[dict, list[str], str | None]:
    """Execute the 7 P4 steps in strict order.

    Returns (outputs, tools_run, error).
    outputs keys: validated_assembly, commercial_result, qa_pairs,
                  score_bundle, trend_report, findings_bundle, narrative_bundle.
    error is None on full success; set on first step failure.
    """
    outputs: dict = {}
    tools_run: list[str] = []

    # s1 — evidence_validator (no deps)
    try:
        outputs["validated_assembly"] = evidence_validator.validate_evidence(
            vendor_id=vendor_id,
            programme_id=programme_id,
            doc_intelligence=context.get("doc_intelligence"),
            structured_bundle=context.get("structured_bundle"),
            signal_bundle=context.get("signal_bundle"),
            vendor_file=context.get("vendor_file") or {},
            historical_state=context.get("historical_evidence"),
        )
        tools_run.append("s1_validate")
    except Exception as exc:
        logger.error("s1_validate failed for %s/%s: %s", programme_id, vendor_id, exc)
        return outputs, tools_run, f"s1_validate: {exc}"

    # s2 — commercial_analyser (deps: s1)
    try:
        outputs["commercial_result"] = commercial_analyser.analyse_commercial(
            vendor_id=vendor_id,
            validated_assembly=outputs["validated_assembly"],
            rs_profile=context.get("rs_profile"),
            structured_bundle=context.get("structured_bundle"),
            historical_state=context.get("historical_commercial"),
            scoring_config=scoring_config,
        )
        tools_run.append("s2_commercial")
    except Exception as exc:
        logger.error("s2_commercial failed for %s/%s: %s", programme_id, vendor_id, exc)
        return outputs, tools_run, f"s2_commercial: {exc}"

    # s3 — inquiry_engine (deps: s1, s2)
    try:
        outputs["qa_pairs"] = inquiry_engine.run_inquiry(
            vendor_id=vendor_id,
            validated_assembly=outputs["validated_assembly"],
            commercial_result=outputs["commercial_result"],
            rs_profile=context.get("rs_profile"),
            historical_qa=context.get("historical_qa"),
            scoring_config=scoring_config,
        )
        tools_run.append("s3_inquire")
    except Exception as exc:
        logger.error("s3_inquire failed for %s/%s: %s", programme_id, vendor_id, exc)
        return outputs, tools_run, f"s3_inquire: {exc}"

    # s4 — scoring_engine (deps: s2, s3)
    try:
        outputs["score_bundle"] = scoring_engine.compute_scores(
            vendor_id=vendor_id,
            qa_pairs=outputs["qa_pairs"],
            commercial_result=outputs["commercial_result"],
            rs_profile=context.get("rs_profile"),
            historical_scores=context.get("historical_scores"),
            scoring_config=scoring_config,
        )
        tools_run.append("s4_score")
    except Exception as exc:
        logger.error("s4_score failed for %s/%s: %s", programme_id, vendor_id, exc)
        return outputs, tools_run, f"s4_score: {exc}"

    # s5 — trend_analyser (deps: s4)
    try:
        outputs["trend_report"] = trend_analyser.analyse_trends(
            vendor_id=vendor_id,
            current_scores=outputs["score_bundle"],
            current_commercial=outputs["commercial_result"],
            historical_scores=context.get("historical_scores"),
            action_history=context.get("action_history"),
        )
        tools_run.append("s5_trend")
    except Exception as exc:
        logger.error("s5_trend failed for %s/%s: %s", programme_id, vendor_id, exc)
        return outputs, tools_run, f"s5_trend: {exc}"

    # s6 — finding_engine (deps: s1, s2, s3, s4, s5)
    try:
        outputs["findings_bundle"] = finding_engine.detect_findings(
            vendor_id=vendor_id,
            programme_id=programme_id,
            score_bundle=outputs["score_bundle"],
            qa_pairs=outputs["qa_pairs"],
            trend_report=outputs["trend_report"],
            commercial_result=outputs["commercial_result"],
            validated_assembly=outputs["validated_assembly"],
            rs_profile=context.get("rs_profile"),
            scoring_config=scoring_config,
        )
        tools_run.append("s6_findings")
    except Exception as exc:
        logger.error("s6_findings failed for %s/%s: %s", programme_id, vendor_id, exc)
        return outputs, tools_run, f"s6_findings: {exc}"

    # s7 — narrative_engine (deps: s1, s2, s3, s4, s6)
    try:
        outputs["narrative_bundle"] = narrative_engine.generate_narratives(
            vendor_id=vendor_id,
            findings_bundle=outputs["findings_bundle"],
            score_bundle=outputs["score_bundle"],
            commercial_result=outputs["commercial_result"],
            qa_pairs=outputs["qa_pairs"],
            validated_assembly=outputs["validated_assembly"],
            vendor_file=context.get("vendor_file") or {},
        )
        tools_run.append("s7_narrative")
    except Exception as exc:
        logger.error("s7_narrative failed for %s/%s: %s", programme_id, vendor_id, exc)
        return outputs, tools_run, f"s7_narrative: {exc}"

    return outputs, tools_run, None


# ---------------------------------------------------------------------------
# Post-run writes
# ---------------------------------------------------------------------------

def _build_analysis_result_md(
    vendor_id: str,
    programme_id: str,
    score_bundle: ScoreBundle,
    findings_bundle: FindingsBundle,
    narrative_bundle: NarrativeBundle,
    vendor_state: str,
    pcs_contribution: float,
    pcs_total: float,
    flags: list[str],
    now_iso: str,
) -> str:
    """Build analysis_result.md content with YAML frontmatter + markdown body."""
    findings = findings_bundle.findings

    fm_data = {
        "vendor_id":        vendor_id,
        "programme_id":     programme_id,
        "cri_score":        score_bundle.cri_score,
        "health_band":      score_bundle.health_band,
        "vendor_state":     vendor_state,
        "finding_count":    len(findings),
        "nba_action":       findings_bundle.nba.action if findings_bundle.nba else None,
        "pcs_contribution": pcs_contribution,
        "pcs_total":        pcs_total,
        "last_analysed_at": now_iso,
        "flags":            flags,
    }
    fm_str = yaml.dump(fm_data, default_flow_style=False, allow_unicode=True)

    lines: list[str] = [f"---\n{fm_str}---\n\n# Analysis Result\n\n"]

    lines.append("## Vendor Summary\n\n")
    lines.append(narrative_bundle.vendor_summary + "\n")

    lines.append("\n## Next Best Action\n\n")
    if findings_bundle.nba:
        nba = findings_bundle.nba
        lines.append(
            f"**Action:** {nba.action}  \n"
            f"**Why:** {nba.why}  \n"
            f"**Owner:** {nba.owner}  \n"
            f"**Timing:** {nba.timing}\n"
        )
    else:
        lines.append("No immediate action required.\n")

    lines.append("\n## Top Findings\n\n")
    lines.append("| Finding | Severity | Source |\n| --- | --- | --- |\n")
    for f in findings_bundle.top_findings:
        lines.append(f"| {f.title} | {f.severity} | {f.source} |\n")

    lines.append("\n## All Findings\n\n")
    lines.append("| ID | Title | Severity | Source | Status |\n| --- | --- | --- | --- | --- |\n")
    for f in findings:
        lines.append(f"| {f.finding_id} | {f.title} | {f.severity} | {f.source} | {f.status} |\n")

    lines.append("\n## Scores\n\n")
    lines.append(
        f"**CRI Score:** {score_bundle.cri_score}  "
        f"**Health Band:** {score_bundle.health_band}\n\n"
    )
    lines.append("| Dimension | Score | Prior | Delta |\n| --- | --- | --- | --- |\n")
    for ds in score_bundle.dimension_scores:
        lines.append(f"| {ds.dimension} | {ds.score} | {ds.prior_score} | {ds.delta} |\n")

    lines.append("\n## Evidence Gaps\n\n")
    lines.append("| Description | Severity | Suggested Action |\n| --- | --- | --- |\n")
    for gap in findings_bundle.gaps:
        lines.append(f"| {gap.description} | {gap.severity} | {gap.suggested_action} |\n")

    return "".join(lines)


def _write_history_files(
    vendor_id: str,
    programme_id: str,
    score_bundle: ScoreBundle,
    qa_pairs: list[QAPair],
    validated_assembly: ValidatedEvidenceAssembly,
    commercial_result: CommercialAnalysisResult,
    now_iso: str,
) -> None:
    """Write / update 5 history JSON files in history/ subdirectory."""
    history_dir = vendor_path(programme_id, vendor_id) / "history"

    def _safe_write(path: Path, data: dict) -> None:
        try:
            atomic_write(
                path,
                json.dumps(data, ensure_ascii=False, indent=2),
                vendor_id=vendor_id,
                programme_id=programme_id,
            )
        except Exception as exc:
            logger.warning("History JSON write failed for %s: %s", path, exc)

    # score_history: append current run to existing list
    score_hist_path = history_dir / "score_history.json"
    prior = _load_history_json(score_hist_path, HistoricalScoreState)
    runs = list(prior.runs) if prior else []
    runs.append({
        "run_at":           now_iso,
        "cri_score":        score_bundle.cri_score,
        "health_band":      score_bundle.health_band,
        "dimension_scores": {ds.dimension: ds.score for ds in score_bundle.dimension_scores},
    })
    _safe_write(score_hist_path, {"vendor_id": vendor_id, "runs": runs})

    # qa_history: current Q&A pairs as prior_pairs
    _safe_write(history_dir / "qa_history.json", {
        "vendor_id":   vendor_id,
        "prior_pairs": [
            {
                "question_id": qa.question_id,
                "answer_text": qa.answer_text,
                "confidence":  qa.confidence,
                "answered_at": qa.answered_at,
            }
            for qa in qa_pairs
        ],
    })

    # evidence_state: snapshot current facts
    fact_snapshot = {
        f.field_name: {
            "value":         f.value,
            "quality_score": f.quality_score,
            "validated_at":  f.validated_at,
        }
        for f in validated_assembly.facts
    }
    _safe_write(history_dir / "evidence_state.json", {
        "vendor_id":         vendor_id,
        "prior_assembly_at": now_iso,
        "fact_snapshot":     fact_snapshot,
    })

    # commercial_state: snapshot key commercial fields
    _safe_write(history_dir / "commercial_state.json", {
        "vendor_id":           vendor_id,
        "prior_analysis_at":   now_iso,
        "prior_contract_type": commercial_result.contract_type,
        "prior_risk_level":    commercial_result.commercial_risk_level,
    })

    # action_history: initialise if absent — VW Agent appends entries over time
    act_hist_path = history_dir / "action_history.json"
    if not act_hist_path.exists():
        _safe_write(act_hist_path, {"vendor_id": vendor_id, "actions": []})


def _write_triage_items(
    vendor_id: str,
    programme_id: str,
    triage_tasks: list[dict],
) -> None:
    """Insert TriageItem rows for BLOCKING gaps. Warning-only on failure."""
    if not triage_tasks:
        return
    try:
        import uuid
        from cobalt.db.sync_to_db import _get_session_factory
        from cobalt.db.models import TriageItem

        factory = _get_session_factory()
        if factory is None:
            logger.debug("_write_triage_items: DATABASE_URL not set — skipping")
            return

        with factory() as session:
            for task in triage_tasks:
                item = TriageItem(
                    triage_id=f"tri-{uuid.uuid4().hex[:12]}",
                    vendor_id=vendor_id,
                    programme_id=programme_id,
                    triage_type=task.get("triage_type"),
                    question=task.get("question"),
                    options=json.dumps(task),
                    status="PENDING",
                )
                session.add(item)
            session.commit()
    except Exception as exc:
        logger.warning("TriageItem insert failed for %s/%s: %s", programme_id, vendor_id, exc)


def _update_programme_logs(
    programme_id: str,
    vendor_id: str,
    result: ANRunResult,
) -> None:
    """Append one row to programme_run/analysis_log.md."""
    try:
        log_path = programme_run_path(programme_id) / "analysis_log.md"
        log_entry = (
            f"| {vendor_id} | {result.status} | "
            f"{result.cri_score} | {result.health_band} | "
            f"{result.finding_count} | {result.analysed_at} |\n"
        )
        append_md(log_path, log_entry, programme_id=programme_id)
    except Exception as exc:
        logger.warning("analysis_log write failed for %s: %s", vendor_id, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_analysis(
    vendor_id: str,
    programme_id: str,
    force: bool = False,
) -> ANRunResult:
    """Drive the full Process 4 pipeline for one vendor.

    Never raises — always returns ANRunResult.
    """
    pcs_before = _read_pcs(programme_id, vendor_id)

    gate_result = _check_gates(vendor_id, programme_id, force)
    if gate_result is not None:
        return gate_result

    try:
        context = _load_context(vendor_id, programme_id)
        scoring_config = _default_scoring_config()

        outputs, tools_run, step_error = _run_steps(
            vendor_id=vendor_id,
            programme_id=programme_id,
            context=context,
            scoring_config=scoring_config,
        )
    except Exception as exc:
        logger.exception("Unhandled error in P4 pipeline for %s/%s", programme_id, vendor_id)
        return ANRunResult(
            vendor_id=vendor_id, programme_id=programme_id,
            status=ANRunStatus.FAILED.value,
            cri_score=None, health_band=None,
            finding_count=0, nba_action=None,
            pcs_before=pcs_before, pcs_after=pcs_before,
            tools_run=[], skip_reason=None,
            error=str(exc),
            analysed_at=_now_iso(),
        )

    score_bundle:      ScoreBundle | None          = outputs.get("score_bundle")
    findings_bundle:   FindingsBundle | None       = outputs.get("findings_bundle")
    narrative_bundle:  NarrativeBundle | None      = outputs.get("narrative_bundle")
    validated_assembly: ValidatedEvidenceAssembly | None = outputs.get("validated_assembly")
    commercial_result: CommercialAnalysisResult | None   = outputs.get("commercial_result")
    qa_pairs: list[QAPair] = outputs.get("qa_pairs") or []

    if step_error or score_bundle is None or findings_bundle is None:
        result = ANRunResult(
            vendor_id=vendor_id, programme_id=programme_id,
            status=ANRunStatus.FAILED.value,
            cri_score=None, health_band=None,
            finding_count=0, nba_action=None,
            pcs_before=pcs_before, pcs_after=pcs_before,
            tools_run=tools_run, skip_reason=None,
            error=step_error or "pipeline_incomplete",
            analysed_at=_now_iso(),
        )
        _update_programme_logs(programme_id, vendor_id, result)
        return result

    # -----------------------------------------------------------------------
    # Post-run writes (7 operations)
    # -----------------------------------------------------------------------
    now_iso = _now_iso()

    # Derive flags for PCS contribution
    flags: list[str] = []
    if score_bundle.cri_score is not None:
        flags.append("CRI_COMPUTED")
    if findings_bundle.findings:
        flags.append("FINDINGS_DETECTED")
    if len(score_bundle.dimension_scores) >= 5:
        flags.append("ALL_DIMS_SCORED")

    pcs_contribution, pcs_total = compute_pcs(pcs_before, flags)

    # Classify vendor state
    open_findings = sum(1 for f in findings_bundle.findings if f.status == "OPEN")
    trend_dir: str | None = None
    trend_report: TrendReport | None = outputs.get("trend_report")
    if trend_report and trend_report.dimension_trends:
        dirs = [
            v.get("direction")
            for v in trend_report.dimension_trends.values()
            if isinstance(v, dict)
        ]
        trend_dir = dirs[0] if dirs else None

    renewal_days: int | None = None
    vf = context.get("vendor_file") or {}
    try:
        rd = vf.get("renewal_days") or vf.get("days_to_renewal")
        renewal_days = int(rd) or None
    except (TypeError, ValueError):
        pass

    vendor_state = classify_vendor_state(
        cri_score=score_bundle.cri_score,
        open_findings=open_findings,
        trend_direction=trend_dir,
        renewal_days=renewal_days,
        flags=flags,
    )

    # Minimal fallbacks for optional outputs (shouldn't happen on full run)
    nb = narrative_bundle or _minimal_narrative(vendor_id)
    va = validated_assembly or _minimal_validated_assembly(vendor_id, programme_id, now_iso)
    cr = commercial_result or _minimal_commercial(vendor_id, now_iso)

    # Write 1: analysis_result.md
    ar_path = vendor_path(programme_id, vendor_id) / "analysis_result.md"
    try:
        content = _build_analysis_result_md(
            vendor_id=vendor_id,
            programme_id=programme_id,
            score_bundle=score_bundle,
            findings_bundle=findings_bundle,
            narrative_bundle=nb,
            vendor_state=vendor_state,
            pcs_contribution=pcs_contribution,
            pcs_total=pcs_total,
            flags=flags,
            now_iso=now_iso,
        )
        atomic_write(ar_path, content, vendor_id=vendor_id, programme_id=programme_id)
    except Exception as exc:
        logger.warning("analysis_result.md write failed for %s/%s: %s", programme_id, vendor_id, exc)

    # Write 2: 5 history JSON files
    _write_history_files(
        vendor_id=vendor_id,
        programme_id=programme_id,
        score_bundle=score_bundle,
        qa_pairs=qa_pairs,
        validated_assembly=va,
        commercial_result=cr,
        now_iso=now_iso,
    )

    # Write 3: ledger.md
    try:
        ledger_entry = (
            f"P4 analysis completed — CRI={score_bundle.cri_score}, "
            f"health={score_bundle.health_band}, findings={len(findings_bundle.findings)}, "
            f"state={vendor_state}, analysed_at={now_iso}\n"
        )
        append_md(
            ledger_path(programme_id, vendor_id),
            ledger_entry,
            vendor_id=vendor_id,
            programme_id=programme_id,
        )
    except Exception as exc:
        logger.warning("ledger.md append failed for %s/%s: %s", programme_id, vendor_id, exc)

    # Write 4: DB sync — triggered automatically by atomic_write on analysis_result.md
    # sync_to_db handler for analysis_result.md updates P4 columns on VendorIntelligence

    # Write 5: TriageItem rows for BLOCKING gaps
    gaps_dicts = [g.to_dict() for g in findings_bundle.gaps]
    triage_tasks = generate_triage_tasks(
        gaps=gaps_dicts,
        flags=flags,
        vendor_id=vendor_id,
        programme_id=programme_id,
    )
    _write_triage_items(vendor_id, programme_id, triage_tasks)

    # Write 6: PCS contribution — computed above (pcs_contribution, pcs_total)

    # Write 7: analysis_log.md
    result = ANRunResult(
        vendor_id=vendor_id, programme_id=programme_id,
        status=ANRunStatus.COMPLETED.value,
        cri_score=score_bundle.cri_score,
        health_band=score_bundle.health_band,
        finding_count=len(findings_bundle.findings),
        nba_action=findings_bundle.nba.action if findings_bundle.nba else None,
        pcs_before=pcs_before,
        pcs_after=pcs_total,
        tools_run=tools_run,
        skip_reason=None,
        error=None,
        analysed_at=now_iso,
    )
    _update_programme_logs(programme_id, vendor_id, result)
    return result


def _read_vendor_ids_from_register(programme_id: str) -> list[str]:
    """Read vendor IDs from vendor_register.md (filesystem, same as RS/enrichment orchestrators)."""
    from cobalt.core.file_system import programme_run_path, read_md
    register_path = programme_run_path(programme_id) / "vendor_register.md"
    if not register_path.exists():
        return []
    data = read_md(register_path)
    if not data:
        return []
    vendors = data.get("vendors") or []
    return [str(v["vendor_id"]) for v in vendors if v.get("vendor_id")]


def run_analysis_all_confirmed(
    programme_id: str,
    **kwargs,
) -> list[ANRunResult]:
    """Run Process 4 for every CONFIRMED vendor in the programme.

    Sequential. A failure on one vendor does not affect others.
    Reads vendor IDs from vendor_register.md; falls back to DB if empty.
    """
    vendor_ids = _read_vendor_ids_from_register(programme_id)
    if not vendor_ids:
        try:
            from cobalt.db.queries import get_confirmed_vendors
            vendor_ids = get_confirmed_vendors(programme_id)
        except Exception as exc:
            logger.warning("Could not query confirmed vendors for %s: %s", programme_id, exc)
            vendor_ids = []

    logger.info("Analysis pipeline: processing %d vendors for programme %s", len(vendor_ids), programme_id)

    results: list[ANRunResult] = []
    for idx, vendor_id in enumerate(vendor_ids, start=1):
        logger.info("[%d/%d] Analysis processing %s ...", idx, len(vendor_ids), vendor_id)
        result = run_analysis(vendor_id, programme_id, **kwargs)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Minimal fallback objects (used only when optional steps returned None)
# ---------------------------------------------------------------------------

def _minimal_narrative(vendor_id: str) -> NarrativeBundle:
    return NarrativeBundle(
        vendor_id=vendor_id,
        vendor_summary="Narrative generation did not complete.",
        finding_narratives=[],
        commercial_summary=None,
        qa_summaries=[],
        evidence_citations=[],
        redaction_flags=[],
        generated_at=_now_iso(),
    )


def _minimal_validated_assembly(vendor_id: str, programme_id: str, now_iso: str) -> ValidatedEvidenceAssembly:
    return ValidatedEvidenceAssembly(
        vendor_id=vendor_id,
        programme_id=programme_id,
        facts=[],
        completeness_pct=0.0,
        conflict_count=0,
        stale_count=0,
        missing_count=0,
        validated_at=now_iso,
    )


def _minimal_commercial(vendor_id: str, now_iso: str) -> CommercialAnalysisResult:
    return CommercialAnalysisResult(
        vendor_id=vendor_id,
        contract_type="UNKNOWN",
        contract_type_confidence="LOW",
        utilisation_score=None,
        licence_waste_pct=None,
        cost_per_seat=None,
        shelfware_flag=False,
        sla_adherence_pct=None,
        delivery_score=None,
        milestone_status=None,
        penalty_exposure=None,
        uptime_pct=None,
        incident_trend=None,
        mttr_days=None,
        commercial_risk_level="LOW",
        commercial_findings=[],
        spend_efficiency_score=None,
        renewal_risk_scenarios=[],
        spend_efficiency_narrative=None,
        analysed_at=now_iso,
    )
