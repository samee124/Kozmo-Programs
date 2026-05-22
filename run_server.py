import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from dotenv import load_dotenv
load_dotenv()

try:
    from flask import Flask, Response, jsonify, request, send_from_directory
except ImportError:
    print("Flask not installed. Run:  pip install flask")
    sys.exit(1)

app = Flask(__name__, static_folder="static")

# Active run registry:  run_id -> {"queue": Queue, "status": str}
_runs: dict = {}


def _slugify(text: str) -> str:
    """Convert a free-form programme name into a filesystem-safe slug.

    e.g. "New Vendor clean up" -> "new-vendor-clean-up"
    """
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:50] or "programme"

# ── Logger → (label, entry_type) mapping ──────────────────────────────────────
_LABELS: dict[str, tuple[str, str]] = {
    # Orchestrators
    "cobalt.orchestrator.intake_orchestrator":         ("Intake",                  "orchestrator"),
    "cobalt.orchestrator.enrichment_orchestrator":     ("Enrichment",              "orchestrator"),
    "cobalt.orchestrator.rs_orchestrator":             ("RS Pipeline",             "orchestrator"),
    "cobalt.orchestrator.analysis_orchestrator":       ("Analysis",                "orchestrator"),
    "cobalt.orchestrator.pa_orchestrator":             ("Plan & Actions",          "orchestrator"),
    # Agents
    "cobalt.agents.planning_agent":                    ("Planning Agent",          "agent"),
    "cobalt.agents.research_agent":                    ("Research Agent",          "agent"),
    "cobalt.agents.analysis_agent":                    ("Analysis Agent",          "agent"),
    "cobalt.agents.vw_agent":                          ("VW Agent",                "agent"),
    # P3 Tools
    "cobalt.tools.structured_data_collector":          ("Tool 1 · Data Collector", "tool"),
    "cobalt.tools.document_intelligence":              ("Tool 2 · Doc Intel",      "tool"),
    "cobalt.tools.spend_aggregator":                   ("Tool 3 · Spend Agg",      "tool"),
    "cobalt.tools.relationship_classifier":            ("Tool 4 · Rel. Class",     "tool"),
    "cobalt.tools.rs_profile_assembler":               ("Tool 5 · RS Assembler",   "tool"),
    # P1 Tools
    "cobalt.tools.source_intake":                      ("P1-T1 · Source Intake",   "tool"),
    "cobalt.tools.candidate_screening":                ("P1-T2 · Screening",       "tool"),
    "cobalt.tools.entity_resolution":                  ("P1-T3 · Entity Res.",     "tool"),
    "cobalt.tools.external_validation":                ("P1-T4 · Validation",      "tool"),
    "cobalt.tools.entity_decision_and_shell_creation": ("P1-T5 · Shell Create",    "tool"),
    # P4 Tools (Analysis & Intelligence)
    "cobalt.tools.evidence_validator":                 ("P4-T1 · Evidence Val.",   "tool"),
    "cobalt.tools.commercial_analyser":                ("P4-T2 · Commercial",      "tool"),
    "cobalt.tools.inquiry_engine":                     ("P4-T3 · Inquiry",         "tool"),
    "cobalt.tools.scoring_engine":                     ("P4-T4 · Scoring",         "tool"),
    "cobalt.tools.trend_analyser":                     ("P4-T5 · Trend",           "tool"),
    "cobalt.tools.finding_engine":                     ("P4-T6 · Findings",        "tool"),
    "cobalt.tools.narrative_engine":                   ("P4-T7 · Narrative",       "tool"),
    # P5 Tools (Plan & Actions)
    "cobalt.tools.action_planner":                     ("P5-T1 · Action Planner",  "tool"),
    "cobalt.tools.task_manager":                       ("P5-T2 · Task Manager",    "tool"),
    "cobalt.tools.communication_composer":             ("P5-T3 · Comms Composer",  "tool"),
    "cobalt.tools.execution_monitor":                  ("P5-T4 · Exec Monitor",    "tool"),
    # Skills (utility modules)
    "cobalt.core.name_matching":                       ("name_matching",           "skill"),
    "cobalt.core.confidence_scorer":                   ("confidence_scorer",       "skill"),
    "cobalt.core.gap_analyzer":                        ("gap_analyzer",            "skill"),
    "cobalt.core.staleness":                           ("staleness",               "skill"),
    "cobalt.core.atomic_write":                        ("atomic_write",            "skill"),
    # Workspace / Brain
    "cobalt.workspace":                                ("Workspace",               "workspace"),
    "cobalt.brain":                                    ("Brain",                   "workspace"),
}


def _classify(name: str) -> tuple[str, str] | None:
    """Return (label, entry_type) for a logger name, or None to suppress."""
    for prefix, info in _LABELS.items():
        if name.startswith(prefix):
            return info
    return None  # suppress azure, httpx, openai, sqlalchemy noise


# ── Custom log handler → SSE queue ────────────────────────────────────────────
class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        info = _classify(record.name)
        if info is None:
            return
        label, entry_type = info
        try:
            self.q.put_nowait({
                "type":       "log",
                "level":      record.levelname,
                "label":      label,
                "entry_type": entry_type,
                "msg":        record.getMessage(),
                "ts":         datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            })
        except Exception:
            pass


# ── Programme list (scan workspace) ───────────────────────────────────────────
def _list_programmes() -> list[dict]:
    workspace = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))
    out: list[dict] = []
    if not workspace.is_dir():
        return out
    for d in sorted(workspace.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        count = 0
        register = d / "programme_run" / "vendor_register.md"
        if register.exists():
            try:
                import yaml
                parts = register.read_text(encoding="utf-8").split("---\n", 2)
                data = yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
                count = len((data or {}).get("vendors") or [])
            except Exception:
                pass
        out.append({
            "programme_id": d.name,
            "last_run":     datetime.fromtimestamp(d.stat().st_mtime).strftime("%d %b %Y, %H:%M"),
            "vendor_count": count,
        })
    return out


# ── Pipeline thread ────────────────────────────────────────────────────────────
def _pipeline_thread(
    run_id: str,
    programme_id: str,
    programme_name: str,
    vendor_list_path: str,
    vendor_filename: str,
    docs_path: str | None,
    own_company: str,
    user_id: str,
    q: queue.Queue,
) -> None:
    def ev(t: str, **kw) -> None:
        q.put_nowait({"type": t, **kw})

    try:
        from cobalt.db.queries import insert_user, insert_programme

        # Bootstrap DB (idempotent)
        insert_user(
            user_id=user_id,
            user_name="Cobalt User",
            email=f"{user_id}@cobalt.local",
            subscription_tier="PROFESSIONAL",
        )
        insert_programme(
            programme_id=programme_id,
            user_id=user_id,
            programme_name=programme_name,
            input_file=vendor_filename,
        )

        # Step 1 — Intake
        from cobalt.orchestrator.intake_orchestrator import run_intake
        ev("step", step="intake", status="running", progress=2)
        intake = run_intake(
            programme_id=programme_id,
            vendor_list_path=vendor_list_path,
            documents_path=docs_path or "",
            own_company=own_company,
        )
        ev("step", step="intake", status="done", progress=25,
           confirmed=len(intake.confirmed), triage=len(intake.triage))

        # Step 2 — Enrichment
        from cobalt.orchestrator.enrichment_orchestrator import enrich_all_confirmed
        ev("step", step="enrichment", status="running", progress=27)
        enrich_results = enrich_all_confirmed(programme_id=programme_id, max_vendors=200)
        done_e = sum(1 for r in enrich_results if r.status == "COMPLETED")
        ev("step", step="enrichment", status="done", progress=50,
           completed=done_e, total=len(enrich_results))

        # Step 3 — RS Pipeline
        from cobalt.orchestrator.rs_orchestrator import run_rs_all_confirmed
        ev("step", step="rs", status="running", progress=53)
        rs_results = run_rs_all_confirmed(
            programme_id=programme_id,
            checkin_data={"currency": "USD"},
        )
        done_rs = sum(1 for r in rs_results if r.status == "COMPLETED")
        ev("step", step="rs", status="done", progress=75,
           completed=done_rs, total=len(rs_results))

        # Step 4 — Analysis & Intelligence (isolated: failure does not abort pipeline)
        # force=True: UI-triggered pipeline always re-analyses regardless of freshness gate
        try:
            from cobalt.orchestrator.analysis_orchestrator import run_analysis_all_confirmed
            ev("step", step="analysis", status="running", progress=78)
            an_results = run_analysis_all_confirmed(programme_id=programme_id, force=True)
            done_an    = sum(1 for r in an_results if r.status == "COMPLETED")
            skipped_an = sum(1 for r in an_results if r.status == "SKIPPED")
            ev("step", step="analysis", status="done", progress=88,
               completed=done_an, skipped=skipped_an, total=len(an_results))
        except Exception as an_exc:
            import traceback
            ev("step", step="analysis", status="error", progress=88)
            ev("log", level="ERROR", label="Analysis", entry_type="orchestrator",
               msg=f"Analysis step failed: {an_exc}",
               ts=datetime.now().strftime("%H:%M:%S"))
            logging.getLogger("cobalt").error("Analysis step exception: %s", traceback.format_exc())

        # Step 5 — Plan & Actions (isolated: failure does not abort pipeline)
        # force=True: always re-plans on UI-triggered runs
        try:
            from cobalt.orchestrator.pa_orchestrator import run_plan_all_confirmed
            ev("step", step="pa", status="running", progress=90)
            pa_results    = run_plan_all_confirmed(programme_id=programme_id, force=True)
            done_pa       = sum(1 for r in pa_results if r.status == "COMPLETED")
            skipped_pa    = sum(1 for r in pa_results if r.status == "SKIPPED")
            escalated_pa  = sum(1 for r in pa_results if r.escalation_required)
            ev("step", step="pa", status="done", progress=100,
               completed=done_pa, skipped=skipped_pa, escalated=escalated_pa,
               total=len(pa_results))
        except Exception as pa_exc:
            import traceback
            ev("step", step="pa", status="error", progress=100)
            ev("log", level="ERROR", label="Plan & Actions", entry_type="orchestrator",
               msg=f"Plan & Actions step failed: {pa_exc}",
               ts=datetime.now().strftime("%H:%M:%S"))
            logging.getLogger("cobalt").error("PA step exception: %s", traceback.format_exc())

        ev("pipeline_done")

    except Exception as exc:
        ev("pipeline_error", msg=str(exc))
    finally:
        q.put(None)  # sentinel — SSE generator exits


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "Kozmo_Workspace.html")


@app.route("/workflow")
def workflow():
    return send_from_directory("static", "Kozmo_workflow.html")


@app.route("/api/programmes")
def api_programmes():
    return jsonify(_list_programmes())


@app.route("/api/check-programme")
def api_check_programme():
    pid = request.args.get("id", "").strip()
    workspace = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))
    exists = bool(pid and (workspace / pid).is_dir())
    return jsonify({"exists": exists, "programme_id": pid})


@app.route("/api/run", methods=["POST"])
def api_run():
    programme_name = request.form.get("programme_id", "").strip()  # user-entered name
    programme_id   = _slugify(programme_name)                       # filesystem-safe slug
    own_company    = request.form.get("own_company", "My Company").strip()
    user_id        = os.getenv("COBALT_USER_ID", "user001")

    if not programme_id:
        return jsonify({"error": "programme_id is required"}), 400

    vf = request.files.get("vendor_list")
    if not vf or not vf.filename:
        return jsonify({"error": "vendor_list file is required"}), 400

    upload_dir = Path(tempfile.mkdtemp(prefix="cobalt_run_"))
    vendor_path = str(upload_dir / vf.filename)
    vf.save(vendor_path)

    docs_path: str | None = None
    doc_files = request.files.getlist("documents")
    if any(f.filename for f in doc_files):
        docs_dir = upload_dir / "docs"
        docs_dir.mkdir()
        for f in doc_files:
            if f.filename:
                f.save(str(docs_dir / f.filename))
        docs_path = str(docs_dir)

    run_id = uuid.uuid4().hex[:8]
    q: queue.Queue = queue.Queue()
    _runs[run_id] = {"queue": q, "status": "running"}

    handler = _QueueHandler(q)
    cobalt_log = logging.getLogger("cobalt")
    cobalt_log.setLevel(logging.INFO)
    cobalt_log.addHandler(handler)

    def _run():
        try:
            _pipeline_thread(run_id, programme_id, programme_name,
                             vendor_path, vf.filename or "",
                             docs_path, own_company, user_id, q)
        finally:
            cobalt_log.removeHandler(handler)
            _runs[run_id]["status"] = "done"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"run_id": run_id, "programme_id": programme_id, "programme_name": programme_name})


@app.route("/api/run/<run_id>/stream")
def api_stream(run_id: str):
    run = _runs.get(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404

    def _generate():
        q = run["queue"]
        while True:
            try:
                msg = q.get(timeout=25)
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'
                continue
            if msg is None:
                yield 'data: {"type":"done"}\n\n'
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/programmes/<path:programme_id>/vendors")
def api_programme_vendors(programme_id: str):
    """List all vendors in a programme with their pipeline stage indicators."""
    import yaml as _yaml
    workspace = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))
    prog_dir  = workspace / programme_id

    if not prog_dir.is_dir():
        return jsonify([])

    # Read vendor_register.md for ordered vendor list
    register = prog_dir / "programme_run" / "vendor_register.md"
    vendor_ids: list[str] = []
    if register.exists():
        try:
            parts = register.read_text(encoding="utf-8").split("---\n", 2)
            data = _yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
            vendor_ids = [str(v["vendor_id"]) for v in (data or {}).get("vendors") or [] if v.get("vendor_id")]
        except Exception:
            pass

    # Fall back to scanning directory
    if not vendor_ids:
        vendor_ids = sorted(
            d.name for d in prog_dir.iterdir()
            if d.is_dir() and d.name != "programme_run"
        )

    out: list[dict] = []
    for vid in vendor_ids:
        vdir = prog_dir / vid

        # Find single-file root .md
        main_md = next((f for f in vdir.iterdir() if f.suffix == ".md" and f.is_file()), None) \
            if vdir.is_dir() else None

        # Read frontmatter for display fields
        name = vid; status = "UNKNOWN"; data_class = "CLASS_D"
        cri_score = None; health_band = None
        if main_md and main_md.exists():
            try:
                parts = main_md.read_text(encoding="utf-8").split("---\n", 2)
                fm = _yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
                name       = (fm or {}).get("vendor_name") or (fm or {}).get("input_name") or vid
                status     = (fm or {}).get("status") or (fm or {}).get("intake_status") or "UNKNOWN"
                data_class = (fm or {}).get("data_class") or "CLASS_D"
            except Exception:
                pass

        # Read P4 analysis_result.md for CRI score / health band
        analysis_md = vdir / "analysis_result.md"
        if analysis_md.exists():
            try:
                parts = analysis_md.read_text(encoding="utf-8").split("---\n", 2)
                afm = _yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
                cri_score   = (afm or {}).get("cri_score")
                health_band = (afm or {}).get("health_band")
            except Exception:
                pass

        # Check enrichment/relationship sections in the consolidated profile
        _main_fm: dict = {}
        if main_md and main_md.exists():
            try:
                _parts2 = main_md.read_text(encoding="utf-8").split("---\n", 2)
                _main_fm = _yaml.safe_load(_parts2[1]) if len(_parts2) >= 3 else {}
            except Exception:
                pass
        out.append({
            "vendor_id":   vid,
            "vendor_name": name,
            "status":      status,
            "data_class":  data_class,
            "cri_score":   cri_score,
            "health_band": health_band,
            "files": {
                "main":           main_md is not None,
                "vendor_profile": bool((_main_fm or {}).get("enrichment")),
                "rs_profile":     bool((_main_fm or {}).get("relationship")),
                "analysis":       analysis_md.exists(),
                "action_plan":    (vdir / "action_plan.md").exists(),
                "ledger":         (vdir / "execution" / "ledger.md").exists(),
            },
        })

    return jsonify(out)


@app.route("/api/programmes/<path:programme_id>/vendors/<path:vendor_id>")
def api_vendor_detail(programme_id: str, vendor_id: str):
    """Return full vendor detail — all workspace files aggregated into one JSON object."""
    import json as _json
    import yaml
    workspace = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))
    vdir = workspace / programme_id / vendor_id

    if not vdir.is_dir():
        return jsonify({"error": "vendor not found"}), 404

    def _read_yaml_md(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            parts = path.read_text(encoding="utf-8").split("---\n", 2)
            return yaml.safe_load(parts[1]) or {} if len(parts) >= 3 else {}
        except Exception:
            return {}

    def _read_json(path: Path):
        if not path.exists():
            return None
        try:
            return _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _read_md_body(path: Path) -> str:
        if not path.exists():
            return ""
        try:
            parts = path.read_text(encoding="utf-8").split("---\n", 2)
            return parts[2].strip() if len(parts) >= 3 else path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    # ── 1. Consolidated profile — {slug}_profile.md (P1 + P2 + P3) ──────────
    # Prefer *_profile.md; fall back to old {vendor_id}.md; skip known non-intake files.
    _SKIP_MDS = {"action_plan.md", "analysis_result.md", "execution_state.md", "task_list.md"}
    _new_profile = next(
        (f for f in vdir.iterdir()
         if f.suffix == ".md" and f.is_file() and f.name.endswith("_profile.md")),
        None,
    ) if vdir.is_dir() else None
    if _new_profile is None:
        _direct = vdir / f"{vendor_id}.md"
        if _direct.is_file():
            main_md = _direct
        else:
            main_md = next(
                (f for f in vdir.iterdir() if f.suffix == ".md" and f.is_file() and f.name not in _SKIP_MDS),
                None,
            ) if vdir.is_dir() else None
    else:
        main_md = _new_profile

    intake_fm = _read_yaml_md(main_md) if main_md else {}

    # P2 enrichment and P3 relationship are nested keys in the consolidated profile
    vp_fm = intake_fm.get("enrichment") or {}
    rs_fm = intake_fm.get("relationship") or {}

    # ── 4. Analysis — analysis_result.md (P4) ───────────────────────────────
    an_fm   = _read_yaml_md(vdir / "analysis_result.md")
    an_body = _read_md_body(vdir / "analysis_result.md")

    # ── 5. History ──────────────────────────────────────────────────────────
    score_hist     = _read_json(vdir / "history" / "score_history.json")
    qa_hist        = _read_json(vdir / "history" / "qa_history.json")
    evidence_state = _read_json(vdir / "history" / "evidence_state.json")
    commercial_st  = _read_json(vdir / "history" / "commercial_state.json")
    action_hist    = _read_json(vdir / "history" / "action_history.json")
    plan_hist      = _read_json(vdir / "history" / "plan_history.json")

    # ── 6. Execution (P5) ───────────────────────────────────────────────────
    exec_fm    = _read_yaml_md(vdir / "execution_state.md")
    plan_fm    = _read_yaml_md(vdir / "action_plan.md")
    tasks_fm   = _read_yaml_md(vdir / "task_list.md")
    ledger_txt = _read_md_body(vdir / "execution" / "ledger.md")

    # ── 7. Plans ────────────────────────────────────────────────────────────
    inv_plan = _read_yaml_md(vdir / "plans" / "investigation_plan.md")
    enr_plan = _read_yaml_md(vdir / "plans" / "enrichment_plan.md")
    rs_plan  = _read_yaml_md(vdir / "plans" / "rs_plan.md")

    # ── 8. Derive lifecycle_stage if P5 hasn't run yet ──────────────────────
    lifecycle_stage = exec_fm.get("lifecycle_stage")
    if not lifecycle_stage:
        if (vdir / "analysis_result.md").exists():
            lifecycle_stage = "P4_COMPLETE"
        elif rs_fm:
            lifecycle_stage = "P3_COMPLETE"
        elif vp_fm:
            lifecycle_stage = "P2_COMPLETE"
        else:
            lifecycle_stage = "P1_COMPLETE"

    # ── 9. Files present flags ──────────────────────────────────────────────
    files = {
        "main":           main_md is not None,
        "vendor_profile": bool(vp_fm),
        "rs_profile":     bool(rs_fm),
        "analysis":       (vdir / "analysis_result.md").exists(),
        "action_plan":    (vdir / "action_plan.md").exists(),
        "task_list":      (vdir / "task_list.md").exists(),
        "ledger":         (vdir / "execution" / "ledger.md").exists(),
    }

    return jsonify({
        "vendor_id":   vendor_id,
        "programme_id": programme_id,
        "lifecycle_stage": lifecycle_stage,
        "files": files,

        "identity": {
            "vendor_id":      intake_fm.get("vendor_id") or vendor_id,
            "vendor_name":    intake_fm.get("vendor_name") or intake_fm.get("canonical_name") or vendor_id,
            "canonical_name": intake_fm.get("canonical_name") or vendor_id,
            "programme_id":   intake_fm.get("programme_id") or programme_id,
            "status":         intake_fm.get("status"),
            "data_class":     (intake_fm.get("intake") or {}).get("data_class"),
            "entity_type":    (intake_fm.get("intake") or {}).get("entity_type"),
            "country_code":   (intake_fm.get("intake") or {}).get("country_code"),
            "resolution_method": (intake_fm.get("intake") or {}).get("resolution_method"),
            "confidence":     (intake_fm.get("intake") or {}).get("confidence"),
            "financial":      intake_fm.get("financial"),
            "legal":          intake_fm.get("legal"),
            "classification": intake_fm.get("classification"),
        },

        "enrichment": {
            "profile_status":     vp_fm.get("profile_status"),
            "overall_confidence": vp_fm.get("overall_confidence"),
            "enriched_at":        vp_fm.get("enriched_at"),
            "depth_tier":         (vp_fm.get("enrichment_metadata") or {}).get("depth_tier"),
            "identity":           vp_fm.get("identity"),
            "classification":     vp_fm.get("classification"),
            "size":               vp_fm.get("size"),
            "organisation":       vp_fm.get("organisation"),
            "certifications":     vp_fm.get("certifications") or [],
            "products_and_services": vp_fm.get("products_and_services") or [],
            "reputation_signals": vp_fm.get("reputation_signals") or [],
            "lifecycle_signals":  vp_fm.get("lifecycle_signals") or [],
            "flags":              vp_fm.get("flags") or [],
            "gaps":               vp_fm.get("gaps"),
        } if vp_fm else None,

        "relationship": {
            "relationship_type":  rs_fm.get("relationship_type"),
            "dependency_tier":    rs_fm.get("dependency_tier"),
            "spend_total_ttm_usd": rs_fm.get("spend_total_ttm_usd"),
            "contract_count":     rs_fm.get("contract_count"),
            "contract_coverage":  None,  # in body text
            "pcs_contribution":   rs_fm.get("pcs_contribution"),
            "pcs_total":          rs_fm.get("pcs_total"),
            "flags":              rs_fm.get("flags") or [],
            "profile_status":     rs_fm.get("profile_status") if "profile_status" in rs_fm else None,
        } if rs_fm else None,

        "analysis": {
            "cri_score":       an_fm.get("cri_score"),
            "health_band":     an_fm.get("health_band"),
            "vendor_state":    an_fm.get("vendor_state"),
            "finding_count":   an_fm.get("finding_count"),
            "nba_action":      an_fm.get("nba_action"),
            "last_analysed_at": an_fm.get("last_analysed_at"),
            "flags":           an_fm.get("flags") or [],
            "findings":        an_fm.get("findings") or [],
            "body":            an_body,
        } if an_fm else None,

        "scores":          score_hist,
        "qa":              qa_hist,
        "evidence_state":  evidence_state,
        "commercial_state": commercial_st,
        "action_history":  action_hist,
        "plan_history":    plan_hist,

        "execution": {
            "plan_id":           exec_fm.get("plan_id"),
            "completion_pct":    exec_fm.get("completion_pct"),
            "tasks_total":       exec_fm.get("tasks_total"),
            "tasks_completed":   exec_fm.get("tasks_completed"),
            "tasks_blocked":     exec_fm.get("tasks_blocked"),
            "tasks_overdue":     exec_fm.get("tasks_overdue"),
            "overdue_flag":      exec_fm.get("overdue_flag"),
            "escalation_required": exec_fm.get("escalation_required"),
            "escalation_reason": exec_fm.get("escalation_reason"),
            "comms_pending_review": exec_fm.get("comms_pending_review"),
            "closure_recommended": exec_fm.get("closure_recommended"),
            "lifecycle_stage":   lifecycle_stage,
            "stages_complete":   exec_fm.get("stages_complete") or [],
            "task_statuses":     exec_fm.get("task_statuses") or [],
            "last_checked_at":   exec_fm.get("last_checked_at"),
        } if exec_fm else {"lifecycle_stage": lifecycle_stage, "stages_complete": []},

        "action_plan": {
            "plan_id":           plan_fm.get("plan_id"),
            "plan_title":        plan_fm.get("plan_title"),
            "plan_objective":    plan_fm.get("plan_objective"),
            "selected_playbook": plan_fm.get("selected_playbook"),
            "playbook_rationale": plan_fm.get("playbook_rationale"),
            "confidence":        plan_fm.get("confidence"),
            "created_at":        plan_fm.get("created_at"),
            "steps":             plan_fm.get("steps") or [],
        } if plan_fm else None,

        "tasks": tasks_fm.get("tasks") or [] if tasks_fm else [],

        "plans": {
            "investigation": inv_plan or None,
            "enrichment":    enr_plan or None,
            "rs":            rs_plan  or None,
        },

        "ledger": ledger_txt,
    })


@app.route("/api/vendor-file")
def api_vendor_file():
    """Return raw content of a vendor workspace MD file."""
    programme_id = request.args.get("programme", "").strip()
    vendor_id    = request.args.get("vendor", "").strip()
    file_type    = request.args.get("file", "main").strip()

    if not programme_id or not vendor_id:
        return jsonify({"error": "programme and vendor required"}), 400

    workspace = Path(os.getenv("WORKSPACE_ROOT", "./workspace"))
    vdir = workspace / programme_id / vendor_id

    if file_type in ("main", "vendor_profile", "rs_profile"):
        # All three live in the consolidated *_profile.md
        target = next(
            (f for f in vdir.iterdir()
             if f.suffix == ".md" and f.is_file() and f.name.endswith("_profile.md")),
            None,
        ) if vdir.is_dir() else None
        if target is None and vdir.is_dir():
            target = next(
                (f for f in vdir.iterdir() if f.suffix == ".md" and f.is_file()), None
            )
    else:
        path_map = {
            "analysis": vdir / "analysis_result.md",
            "ledger":   vdir / "execution" / "ledger.md",
        }
        target = path_map.get(file_type)

    if target is None or not target.exists():
        return jsonify({"content": "", "exists": False, "filename": ""})

    try:
        return jsonify({
            "content":  target.read_text(encoding="utf-8"),
            "exists":   True,
            "filename": target.name,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "exists": False}), 500


if __name__ == "__main__":
    Path("static").mkdir(exist_ok=True)
    print("\n  Cobalt UI  ->  http://localhost:5050\n")
    app.run(debug=False, threaded=True, port=5050)
