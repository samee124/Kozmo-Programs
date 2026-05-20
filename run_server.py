"""Cobalt Pipeline Web UI Server.

Serves the browser UI and streams real-time pipeline logs via SSE.

Usage:
    pip install flask
    python run_server.py
    Open http://localhost:5050
"""

import json
import logging
import os
import queue
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

# ── Logger → display label mapping ────────────────────────────────────────────
_LABELS = {
    "cobalt.orchestrator.intake_orchestrator":      "Intake",
    "cobalt.orchestrator.enrichment_orchestrator":  "Enrichment",
    "cobalt.orchestrator.rs_orchestrator":          "RS Pipeline",
    "cobalt.agents.planning_agent":                 "Planning Agent",
    "cobalt.agents.research_agent":                 "Research Agent",
    "cobalt.agents.analysis_agent":                 "Analysis Agent",
    "cobalt.agents.vw_agent":                       "VW Agent",
    "cobalt.tools.structured_data_collector":       "Tool 1 · Data Collector",
    "cobalt.tools.document_intelligence":           "Tool 2 · Doc Intelligence",
    "cobalt.tools.spend_aggregator":                "Tool 3 · Spend Aggregator",
    "cobalt.tools.relationship_classifier":         "Tool 4 · Rel. Classifier",
    "cobalt.tools.rs_profile_assembler":            "Tool 5 · Profile Assembler",
    "cobalt.tools.source_intake":                   "Tool P1-1 · Source Intake",
    "cobalt.tools.candidate_screening":             "Tool P1-2 · Screening",
    "cobalt.tools.entity_resolution":               "Tool P1-3 · Entity Resolution",
    "cobalt.tools.external_validation":             "Tool P1-4 · Validation",
    "cobalt.tools.entity_decision_and_shell_creation": "Tool P1-5 · Shell Creation",
    "cobalt.workspace":                             "Workspace",
    "cobalt.brain":                                 "Brain",
}


def _label(name: str) -> str | None:
    """Return display label for a logger name, or None to suppress."""
    for prefix, label in _LABELS.items():
        if name.startswith(prefix):
            return label
    # Suppress azure, httpx, openai, sqlalchemy noise
    return None


# ── Custom log handler → SSE queue ────────────────────────────────────────────
class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        label = _label(record.name)
        if label is None:
            return
        try:
            self.q.put_nowait({
                "type":  "log",
                "level": record.levelname,
                "label": label,
                "msg":   record.getMessage(),
                "ts":    datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
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
    vendor_list_path: str,
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
            programme_name=programme_id,
        )

        # Step 1 — Intake
        ev("step", step="intake", status="running", progress=2)
        intake = run_intake(
            programme_id=programme_id,
            vendor_list_path=vendor_list_path,
            documents_path=docs_path or "",
            own_company=own_company,
        )
        ev("step", step="intake", status="done", progress=33,
           confirmed=len(intake.confirmed), triage=len(intake.triage))

        # Step 2 — Enrichment
        ev("step", step="enrichment", status="running", progress=36)
        enrich_results = enrich_all_confirmed(programme_id=programme_id, max_vendors=200)
        done_e = sum(1 for r in enrich_results if r.status == "COMPLETED")
        ev("step", step="enrichment", status="done", progress=66,
           completed=done_e, total=len(enrich_results))

        # Step 3 — RS Pipeline
        ev("step", step="rs", status="running", progress=68)
        rs_results = run_rs_all_confirmed(
            programme_id=programme_id,
            checkin_data={"currency": "USD"},
        )
        done_rs = sum(1 for r in rs_results if r.status == "COMPLETED")
        ev("step", step="rs", status="done", progress=100,
           completed=done_rs, total=len(rs_results))

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
    programme_id = request.form.get("programme_id", "").strip()
    own_company  = request.form.get("own_company", "My Company").strip()
    user_id      = os.getenv("COBALT_USER_ID", "user001")

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
            _pipeline_thread(run_id, programme_id, vendor_path, docs_path,
                             own_company, user_id, q)
        finally:
            cobalt_log.removeHandler(handler)
            _runs[run_id]["status"] = "done"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"run_id": run_id})


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
    print("\n  Cobalt UI  →  http://localhost:5050\n")
    app.run(debug=False, threaded=True, port=5050)
