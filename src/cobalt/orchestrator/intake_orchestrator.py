"""Intake Orchestrator — drives the full intake pipeline by reading plans.

Flow
----
1. Planning Agent writes programme_plan.md (strategy + step list).
2. Orchestrator executes each step in order, updating the plan as it goes.
3. For INVESTIGATE vendors, Planning Agent writes an investigation plan .md.
4. Orchestrator executes each investigation step (dispatching to Research /
   Analysis agents via the tool registry in external_validation).
5. entity_decision_and_shell_creation creates the vendor workspace.
6. Programme report files are written unconditionally at the end.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cobalt.agents.analysis_agent import AnalysisAgent
from cobalt.agents.planning_agent import PlanningAgent
from cobalt.agents.research_agent import ResearchAgent
from cobalt.core.atomic_write import atomic_write
from cobalt.core.exceptions import CheckpointParseError
from cobalt.core.file_system import init_programme_workspace, programme_path
from cobalt.models.schemas.signal_profile_schema import SignalProfile
from cobalt.orchestrator.batch_context_builder import build_batch_context
from cobalt.tools.candidate_screening import ScreenedCandidate, screen_all
from cobalt.tools.entity_decision_and_shell_creation import EntityDecision, decide_and_create
from cobalt.tools.entity_resolution import resolve_all
from cobalt.tools.external_validation import validate
from cobalt.tools.source_intake import DocRecord, RawCandidate, run as source_intake_run
from cobalt.workspace.plan_writer import finalise_plan, update_plan_step

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Checkpoint — crash-recovery for intake runs
# ---------------------------------------------------------------------------

@dataclass
class IntakeCheckpoint:
    programme_id:         str
    started_at:           str
    last_updated:         str
    status:               str  # "IN_PROGRESS" / "COMPLETED" / "FAILED"
    completed_candidates: list[str]
    failed_candidates:    list[str]
    total_candidates:     int

    def save(self, path: Path) -> None:
        now = _now()
        self.last_updated = now
        data = {
            "programme_id":         self.programme_id,
            "started_at":           self.started_at,
            "last_updated":         now,
            "status":               self.status,
            "completed_candidates": self.completed_candidates,
            "failed_candidates":    self.failed_candidates,
            "total_candidates":     self.total_candidates,
        }
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "IntakeCheckpoint | None":
        """Return None if file is missing. Raises CheckpointParseError if malformed."""
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CheckpointParseError(
                f"Malformed checkpoint JSON at {path}: {exc}"
            ) from exc
        try:
            return cls(
                programme_id=raw["programme_id"],
                started_at=raw["started_at"],
                last_updated=raw["last_updated"],
                status=raw["status"],
                completed_candidates=list(raw.get("completed_candidates") or []),
                failed_candidates=list(raw.get("failed_candidates") or []),
                total_candidates=int(raw.get("total_candidates", 0)),
            )
        except KeyError as exc:
            raise CheckpointParseError(
                f"Checkpoint at {path} is missing required field: {exc}"
            ) from exc


def _checkpoint_path(programme_id: str) -> Path:
    return programme_path(programme_id) / "programme_run" / "checkpoint.json"


@dataclass
class IntakeRunResult:
    programme_id: str
    confirmed:    list[EntityDecision]
    triage:       list[EntityDecision]
    discarded:    list[EntityDecision]
    blocked:      list[EntityDecision]
    summary:      dict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_discard_decision() -> EntityDecision:
    return EntityDecision(
        vendor_id=None,
        status="DISCARDED",
        workspace_path=None,
        data_class="CLASS_D",
        initial_pcs=0,
        confidence=0.0,
        fraud_risk="LOW",
        triage_question=None,
        block_reason=None,
        files_written=[],
    )


def _route_decision(
    decision: EntityDecision,
    confirmed: list,
    triage: list,
    discarded: list,
    blocked: list,
) -> None:
    if decision.status == "CONFIRMED":
        confirmed.append(decision)
    elif decision.status == "TRIAGE_REQUIRED":
        triage.append(decision)
    elif decision.status == "DISCARDED":
        discarded.append(decision)
    elif decision.status == "BLOCKED":
        blocked.append(decision)
    else:
        triage.append(decision)


def _get_doc_terms(
    profile: SignalProfile,
    doc_registry: dict[str, DocRecord],
    analysis_agent: AnalysisAgent,
) -> dict | None:
    """Extract and consolidate contract terms from linked documents."""
    if not profile.linked_doc_ids:
        return None
    terms_list: list[dict] = []
    for doc_id in profile.linked_doc_ids:
        doc = doc_registry.get(doc_id)
        if doc is None or not doc.raw_text:
            continue
        try:
            terms = analysis_agent.extract_contract_terms(doc.raw_text, profile.normalized)
            if terms:
                terms_list.append(terms)
        except Exception as exc:
            logger.warning("extract_contract_terms failed for doc %r: %s", doc_id, exc)
    if not terms_list:
        return None
    if len(terms_list) == 1:
        return terms_list[0]
    try:
        return analysis_agent.consolidate_documents(terms_list)
    except Exception as exc:
        logger.warning("consolidate_documents failed: %s", exc)
        return terms_list[0]


def _write_programme_files(
    programme_id: str,
    confirmed: list[EntityDecision],
    triage: list[EntityDecision],
    discarded: list[EntityDecision],
    blocked: list[EntityDecision],
    total_input: int,
) -> None:
    run_dir = programme_path(programme_id) / "programme_run"

    atomic_write(
        run_dir / "vendor_register.md",
        {
            "programme_id": programme_id,
            "created_at": _now(),
            "vendors": [
                {
                    "vendor_id":   d.vendor_id,
                    "data_class":  d.data_class,
                    "initial_pcs": d.initial_pcs,
                    "confidence":  d.confidence,
                }
                for d in confirmed
            ],
        },
        programme_id=programme_id,
    )

    atomic_write(
        run_dir / "deduplication_report.md",
        {
            "programme_id": programme_id,
            "created_at":   _now(),
            "total_input":  total_input,
            "confirmed":    len(confirmed),
            "triage":       len(triage),
            "discarded":    len(discarded),
            "blocked":      len(blocked),
        },
        programme_id=programme_id,
    )

    atomic_write(
        run_dir / "triage_queue.md",
        {
            "programme_id": programme_id,
            "created_at":   _now(),
            "triage_items": [
                {"triage_question": d.triage_question, "vendor_id": d.vendor_id}
                for d in triage
            ],
        },
        programme_id=programme_id,
    )

    atomic_write(
        run_dir / "run_log.md",
        {
            "programme_id": programme_id,
            "status":       "INTAKE_COMPLETED",
            "completed_at": _now(),
            "summary": {
                "total_input": total_input,
                "confirmed":   len(confirmed),
                "triage":      len(triage),
                "discarded":   len(discarded),
                "blocked":     len(blocked),
            },
        },
        programme_id=programme_id,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_intake(
    programme_id: str,
    vendor_list_path: str | None = None,
    documents_path: str | None = None,
    own_company: str | None = None,
) -> IntakeRunResult:
    """Drive the full intake pipeline. Never raises — always returns a result."""
    confirmed_list: list[EntityDecision] = []
    triage_list:    list[EntityDecision] = []
    discarded_list: list[EntityDecision] = []
    blocked_list:   list[EntityDecision] = []
    candidates:     list[RawCandidate]   = []

    # ----------------------------------------------------------------
    # Checkpoint — early exit if already completed
    # ----------------------------------------------------------------
    ckpt_path = _checkpoint_path(programme_id)
    checkpoint = IntakeCheckpoint.load(ckpt_path)
    if checkpoint is not None and checkpoint.status == "COMPLETED":
        logger.info("Intake for %r already completed — returning without re-running.", programme_id)
        return IntakeRunResult(
            programme_id=programme_id,
            confirmed=[], triage=[], discarded=[], blocked=[],
            summary={"total_input": checkpoint.total_candidates, "already_completed": True},
        )

    logger.info("=" * 60)
    logger.info("Starting intake run  programme=%r", programme_id)
    logger.info("=" * 60)

    try:
        # Boot agents
        planning_agent  = PlanningAgent()
        research_agent  = ResearchAgent()
        analysis_agent  = AnalysisAgent()
        logger.info("Agents initialised")

        init_programme_workspace(programme_id)
        logger.info("Workspace directories created")

        # Create or resume checkpoint (directory now guaranteed to exist)
        if checkpoint is None:
            checkpoint = IntakeCheckpoint(
                programme_id=programme_id,
                started_at=_now(),
                last_updated=_now(),
                status="IN_PROGRESS",
                completed_candidates=[],
                failed_candidates=[],
                total_candidates=0,
            )
        checkpoint.save(ckpt_path)

        # ----------------------------------------------------------------
        # Step 1 — Tool 1: source_intake
        # ----------------------------------------------------------------
        logger.info("Step 1 — source_intake  list=%r  drive=%r", vendor_list_path, documents_path)
        source_result = source_intake_run(
            vendor_list_path, documents_path, research_agent, analysis_agent, own_company
        )
        candidates   = source_result.candidates
        doc_registry = source_result.doc_registry
        s = source_result.source_summary
        logger.info(
            "Step 1 done — %d candidates  (%d from list | %d from docs | %d from both)  %d docs",
            s.get("candidates_total", 0),
            s.get("from_list_only", 0),
            s.get("from_docs_only", 0),
            s.get("from_both", 0),
            s.get("doc_count", 0),
        )

        # ----------------------------------------------------------------
        # Step 2 — Planning Agent writes the programme plan .md
        # ----------------------------------------------------------------
        logger.info("Step 2 — writing programme plan")
        try:
            planning_agent.write_programme_plan(programme_id, source_result.source_summary)
            logger.info("Step 2 done — programme_plan.md written")
        except Exception as exc:
            logger.warning("Programme plan write failed: %s", exc)

        # ----------------------------------------------------------------
        # Step 3 — Tool 2: candidate_screening
        # ----------------------------------------------------------------
        logger.info("Step 3 — candidate_screening  %d candidates", len(candidates))
        screened_list = screen_all([
            {
                "raw":           c.raw_name,
                "source_refs":   c.source_refs,
                "linked_doc_ids": c.linked_doc_ids,
                "spend_hint":    c.spend_hint,
                "category_hint": c.category_hint,
            }
            for c in candidates
        ])
        passed = sum(1 for s in screened_list if s.disposition == "PASS")
        rejected = len(screened_list) - passed
        logger.info("Step 3 done — %d passed  %d rejected", passed, rejected)

        # ----------------------------------------------------------------
        # Step 4 — Build BatchContext
        # ----------------------------------------------------------------
        logger.info("Step 4 — building batch context (ERP/AP scan + Brain load)")
        ctx = build_batch_context(candidates, research_agent, programme_id, doc_registry)
        logger.info("Step 4 done — batch context ready")

        # ----------------------------------------------------------------
        # Step 5 — Tool 3: entity_resolution
        # ----------------------------------------------------------------
        logger.info("Step 5 — entity_resolution  %d candidates", len(screened_list))
        resolutions = resolve_all(screened_list, ctx)
        rec_counts: dict[str, int] = {}
        for r in resolutions:
            rec_counts[r.recommendation] = rec_counts.get(r.recommendation, 0) + 1
        logger.info("Step 5 done — resolutions: %s", rec_counts)

        # ----------------------------------------------------------------
        # Step 6 — Route each candidate
        # ----------------------------------------------------------------
        total = len(resolutions)
        logger.info("Step 6 — routing %d candidates", total)
        checkpoint.total_candidates = total
        try:
            checkpoint.save(ckpt_path)
        except Exception as ck_exc:
            logger.warning("Checkpoint save failed (non-fatal): %s", ck_exc)

        for idx, (screened, resolution) in enumerate(zip(screened_list, resolutions), 1):
            ck = resolution.comparison_key

            # Skip candidates already processed in a previous run
            if ck in checkpoint.completed_candidates or ck in checkpoint.failed_candidates:
                logger.info("  [%d/%d] %s → SKIPPED (checkpoint)", idx, total, ck)
                continue

            try:
                recommendation = resolution.recommendation
                label = screened.raw[:40]

                if recommendation == "DISCARD":
                    logger.info("  [%d/%d] %-40s → DISCARD", idx, total, label)
                    discarded_list.append(_make_discard_decision())

                elif recommendation == "AUTO_CONFIRM":
                    logger.info("  [%d/%d] %-40s → AUTO_CONFIRM", idx, total, label)
                    profile = resolution.signal_profile
                    plan    = planning_agent.compute_investigation_plan(profile)
                    decision = decide_and_create(
                        entry=screened.raw,
                        profile=profile,
                        plan=plan,
                        step_results=[],
                        programme_id=programme_id,
                        extracted_terms=_get_doc_terms(profile, doc_registry, analysis_agent),
                        erp_signal=profile.erp_signal if profile.erp_signal.exists else None,
                    )
                    logger.info("  [%d/%d] %-40s → %s  vendor_id=%s", idx, total, label, decision.status, decision.vendor_id)
                    _route_decision(decision, confirmed_list, triage_list, discarded_list, blocked_list)

                else:
                    logger.info("  [%d/%d] %-40s → INVESTIGATE", idx, total, label)
                    profile = resolution.signal_profile

                    ip_path, plan = planning_agent.write_investigation_plan(
                        programme_id, profile.comparison_key, profile
                    )
                    logger.info("  [%d/%d] %-40s    plan=%s  steps=%s", idx, total, label, plan.depth.value, plan.steps)

                    validation = validate(screened.raw, profile, plan, research_agent)

                    for step_result in validation.step_results:
                        if ip_path:
                            update_plan_step(ip_path, step_result.step, step_result)

                    extracted_terms = _get_doc_terms(profile, doc_registry, analysis_agent)

                    decision = decide_and_create(
                        entry=screened.raw,
                        profile=profile,
                        plan=plan,
                        step_results=validation.step_results,
                        programme_id=programme_id,
                        extracted_terms=extracted_terms,
                        erp_signal=profile.erp_signal if profile.erp_signal.exists else None,
                    )

                    if ip_path:
                        finalise_plan(
                            ip_path, decision.status,
                            decision.vendor_id, decision.workspace_path,
                        )

                    logger.info("  [%d/%d] %-40s → %s  vendor_id=%s", idx, total, label, decision.status, decision.vendor_id)
                    _route_decision(decision, confirmed_list, triage_list, discarded_list, blocked_list)

                checkpoint.completed_candidates.append(ck)

            except Exception as exc:
                logger.error("Candidate processing failed for %r: %s", screened.raw, exc)
                checkpoint.failed_candidates.append(ck)

            try:
                checkpoint.save(ckpt_path)
            except Exception as ck_exc:
                logger.warning("Checkpoint save failed (non-fatal): %s", ck_exc)

    except Exception as exc:
        logger.error("run_intake failed unexpectedly: %s", exc)

    # ----------------------------------------------------------------
    # Step 7 — Programme report files (always runs, even after failures)
    # ----------------------------------------------------------------
    logger.info("Step 7 — writing programme report files")
    try:
        _write_programme_files(
            programme_id, confirmed_list, triage_list,
            discarded_list, blocked_list, len(candidates),
        )
        logger.info("Step 7 done — vendor_register.md  deduplication_report.md  triage_queue.md  run_log.md")
    except Exception as exc:
        logger.error("Programme files write failed: %s", exc)

    # Finalise checkpoint
    if checkpoint is not None:
        checkpoint.status = "COMPLETED"
        try:
            checkpoint.save(ckpt_path)
        except Exception as exc:
            logger.warning("Final checkpoint save failed: %s", exc)

    logger.info("=" * 60)
    logger.info(
        "Run complete  confirmed=%d  triage=%d  discarded=%d  blocked=%d  total=%d",
        len(confirmed_list), len(triage_list), len(discarded_list), len(blocked_list), len(candidates),
    )
    logger.info("=" * 60)

    return IntakeRunResult(
        programme_id=programme_id,
        confirmed=confirmed_list,
        triage=triage_list,
        discarded=discarded_list,
        blocked=blocked_list,
        summary={
            "total_input": len(candidates),
            "confirmed":   len(confirmed_list),
            "triage":      len(triage_list),
            "discarded":   len(discarded_list),
            "blocked":     len(blocked_list),
        },
    )
