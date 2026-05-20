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
        from cobalt.orchestrator.intake_orchestrator import run_intake
        from cobalt.orchestrator.enrichment_orchestrator import enrich_all_confirmed
        from cobalt.orchestrator.rs_orchestrator import run_rs_all_confirmed
        from cobalt.orchestrator.analysis_orchestrator import run_analysis_all_confirmed

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
        ev("step", step="enrichment", status="running", progress=27)
        enrich_results = enrich_all_confirmed(programme_id=programme_id, max_vendors=200)
        done_e = sum(1 for r in enrich_results if r.status == "COMPLETED")
        ev("step", step="enrichment", status="done", progress=50,
           completed=done_e, total=len(enrich_results))

        # Step 3 — RS Pipeline
        ev("step", step="rs", status="running", progress=53)
        rs_results = run_rs_all_confirmed(
            programme_id=programme_id,
            checkin_data={"currency": "USD"},
        )
        done_rs = sum(1 for r in rs_results if r.status == "COMPLETED")
        ev("step", step="rs", status="done", progress=75,
           completed=done_rs, total=len(rs_results))

        # Step 4 — Analysis & Intelligence
        ev("step", step="analysis", status="running", progress=78)
        an_results = run_analysis_all_confirmed(programme_id=programme_id)
        done_an = sum(1 for r in an_results if r.status == "COMPLETED")
        ev("step", step="analysis", status="done", progress=100,
           completed=done_an, total=len(an_results))

        ev("pipeline_done")

    except Exception as exc:
        ev("pipeline_error", msg=str(exc))
    finally:
        q.put(None)  # sentinel — SSE generator exits


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


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


if __name__ == "__main__":
    Path("static").mkdir(exist_ok=True)
    print("\n  Cobalt UI  ->  http://localhost:5050\n")
    app.run(debug=False, threaded=True, port=5050)
