"""
Cobalt UI API server.

Reads workspace markdown files and serves JSON over HTTP.
Also serves the /ui static files.

Run with:  python serve_ui.py
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ── Paths ────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
WORKSPACE = _HERE.parents[3] / "workspace"
UI_DIR = _HERE.parents[3] / "ui"

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="Cobalt API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Workspace readers ─────────────────────────────────────────────────────────

def _read_fm(path: Path) -> dict[str, Any]:
    """Parse YAML frontmatter from a markdown file. Returns {} if missing."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if m:
        return yaml.safe_load(m.group(1)) or {}
    return {}


def _vendor_dirs(prog_path: Path) -> list[Path]:
    return [
        p for p in prog_path.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "programme_run"
    ]


def _infer_stage(vp: Path) -> int:
    entity_path = vp / "identity" / "entity.md"
    if not entity_path.exists():
        return 1
    fm = _read_fm(entity_path)
    confidence: float = fm.get("identity_confidence") or 0.0

    campaigns_path = vp / "execution" / "campaigns"
    if campaigns_path.exists():
        try:
            next(campaigns_path.iterdir())
            return 4
        except StopIteration:
            pass

    contract_path = vp / "cost_file" / "contract.md"
    if contract_path.exists():
        cfm = _read_fm(contract_path)
        if cfm.get("status") == "OBSERVED":
            return 3

    if confidence >= 0.6:
        return 3
    if confidence > 0:
        return 2
    return 1


def _vendor_summary(vp: Path) -> dict[str, Any]:
    entity = _read_fm(vp / "identity" / "entity.md")
    spend = _read_fm(vp / "cost_file" / "spend.md")
    contract = _read_fm(vp / "cost_file" / "contract.md")
    coverage = _read_fm(vp / "cost_file" / "coverage.md")

    ev_dir = vp / "evidence"
    evidence_count = len(list(ev_dir.glob("ev-*.md"))) if ev_dir.exists() else 0

    effective = (contract.get("effective_terms") or {}) if isinstance(contract.get("effective_terms"), dict) else {}

    return {
        # identity
        "vendor_id": entity.get("vendor_id") or vp.name,
        "vendor_name": entity.get("vendor_name") or vp.name,
        "input_name": entity.get("input_name") or vp.name,
        "identity_confidence": entity.get("identity_confidence"),
        "resolution_method": entity.get("resolution_method"),
        "data_class": entity.get("data_class"),
        "category": entity.get("category"),
        "hq_country": entity.get("hq_country"),
        "legal_entity": entity.get("legal_entity"),
        # spend
        "annual_spend": spend.get("annual_spend"),
        "spend_status": spend.get("status"),
        "spend_confidence": spend.get("confidence"),
        # contract — core
        "contract_value": effective.get("contract_value"),
        "contract_value_type": effective.get("contract_value_type"),
        "contract_status": contract.get("status"),
        "contract_confidence": contract.get("overall_confidence"),
        "conflicts_detected": contract.get("conflicts_detected") or [],
        # contract — terms
        "counterparty_name": effective.get("counterparty_name"),
        "payment_terms": effective.get("payment_terms"),
        "early_termination": effective.get("early_termination"),
        "renewal_date": effective.get("renewal_date"),
        "auto_renewal": effective.get("auto_renewal"),
        "price_escalation": effective.get("price_escalation"),
        "escalation_rate_max": effective.get("escalation_rate_max"),
        "liability_cap": effective.get("liability_cap"),
        "baa_present": effective.get("baa_present"),
        "sla_uptime_pct": effective.get("sla_uptime_pct"),
        "nda_active": effective.get("nda_active"),
        "nda_expiry": effective.get("nda_expiry"),
        # coverage
        "pcs_band": coverage.get("pcs_band"),
        "overall_pcs": coverage.get("overall_pcs"),
        "blocking_gaps": coverage.get("blocking_gaps") or [],
        # meta
        "stage": _infer_stage(vp),
        "evidence_count": evidence_count,
    }


# ── Routes: programmes ────────────────────────────────────────────────────────

@app.get("/api/programmes")
def list_programmes() -> list[dict]:
    result = []
    for prog_dir in sorted(WORKSPACE.iterdir()):
        if not prog_dir.is_dir() or prog_dir.name.startswith("."):
            continue
        vendors = _vendor_dirs(prog_dir)
        result.append({
            "id": prog_dir.name,
            "label": prog_dir.name.replace("-", " ").title(),
            "vendor_count": len(vendors),
        })
    return result


@app.get("/api/programmes/{prog_id}")
def get_programme(prog_id: str) -> dict:
    prog_path = WORKSPACE / prog_id
    if not prog_path.exists():
        raise HTTPException(404, f"Programme '{prog_id}' not found")
    plan = _read_fm(prog_path / "programme_run" / "programme_plan.md")
    label = prog_id.replace("-", " ").title()
    return {
        "id": prog_id,
        "label": label,
        "vendor_count": len(_vendor_dirs(prog_path)),
        "planned_at": plan.get("planned_at"),
        "research_tier": plan.get("research_tier"),
    }


@app.get("/api/programmes/{prog_id}/stats")
def programme_stats(prog_id: str) -> dict:
    prog_path = WORKSPACE / prog_id
    if not prog_path.exists():
        raise HTTPException(404, f"Programme '{prog_id}' not found")

    stage_counts: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    pcs_bands: dict[str, int] = {}
    total_contract = 0.0
    total_spend = 0.0
    confidence_vals: list[float] = []
    with_contract = 0
    with_spend = 0
    with_category = 0
    data_classes: dict[str, int] = {}

    for vp in _vendor_dirs(prog_path):
        try:
            v = _vendor_summary(vp)
        except Exception:
            continue
        s = v["stage"]
        stage_counts[s] = stage_counts.get(s, 0) + 1
        band = v["pcs_band"] or "UNKNOWN"
        pcs_bands[band] = pcs_bands.get(band, 0) + 1
        dc = v["data_class"] or "UNKNOWN"
        data_classes[dc] = data_classes.get(dc, 0) + 1
        if v["contract_value"]:
            total_contract += v["contract_value"]
            with_contract += 1
        if v["annual_spend"]:
            total_spend += v["annual_spend"]
            with_spend += 1
        if v["category"]:
            with_category += 1
        if v["identity_confidence"] is not None:
            confidence_vals.append(v["identity_confidence"])

    total = sum(stage_counts.values())
    avg_conf = round(sum(confidence_vals) / len(confidence_vals), 3) if confidence_vals else 0.0

    return {
        "programme_id": prog_id,
        "total_vendors": total,
        "by_stage": stage_counts,
        "by_pcs_band": pcs_bands,
        "by_data_class": data_classes,
        "with_contract": with_contract,
        "without_contract": total - with_contract,
        "with_spend": with_spend,
        "without_spend": total - with_spend,
        "with_category": with_category,
        "without_category": total - with_category,
        "total_contract_value": round(total_contract, 2),
        "total_spend": round(total_spend, 2),
        "avg_identity_confidence": avg_conf,
    }


# ── Routes: vendors ───────────────────────────────────────────────────────────

@app.get("/api/programmes/{prog_id}/vendors")
def list_vendors(prog_id: str) -> list[dict]:
    prog_path = WORKSPACE / prog_id
    if not prog_path.exists():
        raise HTTPException(404, f"Programme '{prog_id}' not found")

    result = []
    for vp in _vendor_dirs(prog_path):
        try:
            result.append(_vendor_summary(vp))
        except Exception:
            pass
    return sorted(result, key=lambda v: (v["stage"] * -1, v["vendor_name"]))


@app.get("/api/programmes/{prog_id}/vendors/{vendor_id}")
def get_vendor(prog_id: str, vendor_id: str) -> dict:
    prog_path = WORKSPACE / prog_id
    vp = prog_path / vendor_id
    if not vp.exists():
        raise HTTPException(404, f"Vendor '{vendor_id}' not found in programme '{prog_id}'")

    summary = _vendor_summary(vp)

    # Full contract terms
    contract_raw = _read_fm(vp / "cost_file" / "contract.md")
    summary["contract_raw"] = contract_raw

    # Evidence list
    ev_dir = vp / "evidence"
    evidence = []
    if ev_dir.exists():
        for ev_file in sorted(ev_dir.glob("ev-*.md")):
            fm = _read_fm(ev_file)
            fm["file"] = ev_file.name
            evidence.append(fm)
    summary["evidence"] = evidence

    # Ledger (if exists)
    ledger_path = vp / "execution" / "ledger.md"
    summary["has_ledger"] = ledger_path.exists()

    return summary


# ── Ledger helper + endpoint ──────────────────────────────────────────────────

def _parse_ledger(path: Path) -> list[dict]:
    """Parse ledger.md into a list of dicts, one per action block."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    raw_blocks = re.split(r"\n## ", text)
    entries: list[dict] = []
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        # First non-empty line is the heading (action_type)
        heading = lines[0].strip() if lines else ""
        entry: dict[str, Any] = {}
        if heading:
            entry["action_type"] = heading
        for line in lines[1:]:
            line = line.strip()
            if ":" in line:
                key, _, val = line.partition(":")
                entry[key.strip()] = val.strip()
        if entry:
            entries.append(entry)
    return entries


@app.get("/api/programmes/{prog_id}/vendors/{vendor_id}/ledger")
def get_vendor_ledger(prog_id: str, vendor_id: str) -> list[dict]:
    prog_path = WORKSPACE / prog_id
    vp = prog_path / vendor_id
    if not vp.exists():
        raise HTTPException(404, f"Vendor '{vendor_id}' not found in programme '{prog_id}'")
    return _parse_ledger(vp / "execution" / "ledger.md")


# ── Static file serving ───────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(UI_DIR / "programmes.html")

@app.get("/programme")
def programme_page():
    return FileResponse(UI_DIR / "program-spend-profile.html")

@app.get("/vendor")
def vendor_page():
    return FileResponse(UI_DIR / "vendor-workspace.html")

# Mount /ui for direct file access (CSS/JS assets if ever split out)
if UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui_static")
