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


def _find_vendor_file(vp: Path) -> Path | None:
    """Find the single *.md vendor file directly in vp."""
    if not vp.is_dir():
        return None
    md_files = [f for f in vp.iterdir() if f.suffix == ".md" and f.is_file()]
    return md_files[0] if md_files else None


def _read_vendor(vp: Path) -> dict[str, Any]:
    """Read and flatten the single vendor file. Returns {} if missing."""
    f = _find_vendor_file(vp)
    if f:
        return _read_fm(f)
    # Legacy fallback
    return _read_fm(vp / "identity" / "entity.md")


def _infer_stage(vp: Path) -> int:
    fm = _read_vendor(vp)
    if not fm:
        return 1
    intake = fm.get("intake") or {}
    confidence: float = float(intake.get("identity_confidence") or fm.get("identity_confidence") or 0.0)
    legal = fm.get("legal") or {}
    renewal = legal.get("renewal_date") or {}
    has_contract = isinstance(renewal, dict) and renewal.get("value") is not None

    if has_contract:
        return 3
    if confidence >= 0.6:
        return 3
    if confidence > 0:
        return 2
    return 1


def _v(section: dict, key: str) -> Any:
    fd = section.get(key)
    return fd.get("value") if isinstance(fd, dict) else fd


def _vendor_summary(vp: Path) -> dict[str, Any]:
    fm = _read_vendor(vp)
    intake = fm.get("intake") or {}
    identity = fm.get("identity") or {}
    financial = fm.get("financial") or {}
    legal = fm.get("legal") or {}
    classification = fm.get("classification") or {}
    pcs = fm.get("pcs") or {}

    evidence_count = len(fm.get("commercial", {}).get("documents") or [])

    return {
        # identity
        "vendor_id": fm.get("vendor_id") or vp.name,
        "vendor_name": fm.get("canonical_name") or vp.name,
        "input_name": intake.get("input_name") or fm.get("canonical_name") or vp.name,
        "identity_confidence": intake.get("identity_confidence"),
        "resolution_method": intake.get("resolution_method"),
        "data_class": intake.get("data_class"),
        "category": _v(classification, "category"),
        "hq_country": _v(identity, "hq_country"),
        "hq_city": _v(identity, "hq_city"),
        "status": fm.get("status"),
        "overall_confidence": fm.get("overall_confidence"),
        # spend
        "annual_spend": _v(financial, "annual_spend"),
        "spend_status": financial.get("spend_status"),
        "spend_confidence": financial.get("spend_confidence"),
        "currency": financial.get("currency"),
        # contract — terms from legal section
        "contract_value": _v(legal, "contract_value"),
        "contract_value_type": _v(legal, "contract_value_type"),
        "contract_status": "OBSERVED" if _v(legal, "renewal_date") is not None else "NOT_FOUND",
        "counterparty_name": None,
        "payment_terms": _v(legal, "payment_terms"),
        "early_termination": _v(legal, "early_termination"),
        "renewal_date": _v(legal, "renewal_date"),
        "auto_renewal": _v(legal, "auto_renewal"),
        "price_escalation": _v(legal, "price_escalation"),
        "escalation_rate_max": _v(legal, "escalation_rate_max"),
        "liability_cap": _v(legal, "liability_cap"),
        "baa_present": _v(legal, "baa_present"),
        "sla_uptime_pct": _v(legal, "sla_uptime_pct"),
        "nda_active": _v(legal, "nda_active"),
        "nda_expiry": _v(legal, "nda_expiry"),
        # coverage
        "pcs_band": pcs.get("band"),
        "overall_pcs": pcs.get("score"),
        "blocking_gaps": (fm.get("profile_completeness") or {}).get("blocking_gaps") or [],
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
    vendor_data = _read_vendor(vp)

    # Full enriched profile sections
    summary["identity_full"] = vendor_data.get("identity") or {}
    summary["classification_full"] = vendor_data.get("classification") or {}
    summary["size_full"] = vendor_data.get("size") or {}
    summary["organisation"] = vendor_data.get("organisation") or {}
    summary["products_and_services"] = vendor_data.get("products_and_services") or []
    summary["key_people"] = vendor_data.get("key_people") or []
    summary["reputation_signals"] = vendor_data.get("reputation_signals") or []
    summary["lifecycle_signals"] = vendor_data.get("lifecycle_signals") or []
    summary["certifications"] = vendor_data.get("certifications") or []
    summary["competitors"] = vendor_data.get("competitors") or []
    summary["customer_segments"] = vendor_data.get("customer_segments") or []
    summary["flags"] = vendor_data.get("flags") or []
    summary["gaps"] = vendor_data.get("gaps") or {}
    summary["enrichment_metadata"] = vendor_data.get("enrichment_metadata") or {}
    summary["enriched_at"] = vendor_data.get("enriched_at")
    summary["legal_raw"] = vendor_data.get("legal") or {}
    summary["pcs_full"] = vendor_data.get("pcs") or {}

    # Evidence (documents stored inline)
    commercial = vendor_data.get("commercial") or {}
    summary["evidence"] = commercial.get("documents") or []

    # Change log
    change_log = vendor_data.get("change_log") or []
    summary["change_log"] = change_log
    summary["has_ledger"] = len(change_log) > 0

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
    vendor_data = _read_vendor(vp)
    return vendor_data.get("change_log") or []


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
