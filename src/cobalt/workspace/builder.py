"""Workspace builder — creates and updates vendor workspace files.

build_workspace: called by Tool 5 on CONFIRMED intake.
write_vendor_profile: called by Process 2 enrichment.
append_enrichment_ledger_entry: appends an enrichment record to the ledger.

All file writes go through atomic_write() per Rule 3.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

import cobalt.core.file_system as _fs
from cobalt.core.atomic_write import atomic_write
from cobalt.db.queries import insert_vendor
from cobalt.models.schemas.intake_result_schema import IntakeResult, IntakeStatus
from cobalt.models.schemas.signal_profile_schema import ErpSignal

logger = logging.getLogger(__name__)

_LEGAL_SUFFIX_RE = re.compile(
    r",?\s*(Corporation|Corp\.?|Incorporated|Inc\.?|Limited|Ltd\.?|LLC|LLP|GmbH|Co\.?)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class WorkspaceBuildResult:
    success: bool
    workspace_path: Path
    files_written: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ws_root() -> Path:
    return Path(str(_fs.WORKSPACE_ROOT))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _make_slug(canonical_name: str) -> str:
    """Convert a canonical vendor name to a filesystem-safe slug."""
    name = _LEGAL_SUFFIX_RE.sub("", canonical_name).strip().rstrip(".,;: ")
    return name.lower().replace(" ", "_").strip("_")


def _pcs_band(score: int) -> str:
    if score >= 75:
        return "EXECUTION_READY"
    if score >= 50:
        return "GUIDED"
    if score >= 30:
        return "EXPLORATORY"
    return "INSUFFICIENT"


def _compute_pcs(erp_signal: ErpSignal | None, extracted_terms: dict | None) -> int:
    pcs = 0
    if erp_signal and erp_signal.spend is not None:
        pcs += 12
    if extracted_terms:
        if extracted_terms.get("renewal_date") is not None:
            pcs += 15
        if extracted_terms.get("auto_renewal") is not None:
            pcs += 10
        if extracted_terms.get("contract_value") is not None:
            pcs += 8
        if extracted_terms.get("price_escalation") is not None:
            pcs += 5
        if extracted_terms.get("baa_present") is True:
            pcs += 5
    return pcs


# ---------------------------------------------------------------------------
# _write_investigation_plan_file  (called by build_workspace)
# ---------------------------------------------------------------------------

def _write_investigation_plan_file(
    result: IntakeResult,
    vendor_id: str,
    programme_id: str,
    workspace_path: Path,
    canonical_name: str,
) -> Path:
    """Write plans/investigation_plan.md into the vendor workspace (P1)."""
    plan = result.investigation_plan
    plans_dir = workspace_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / "investigation_plan.md"

    depth = plan.depth.value if hasattr(plan.depth, "value") else str(plan.depth)
    fraud = plan.fraud_risk.value if hasattr(plan.fraud_risk, "value") else str(plan.fraud_risk)
    fm: dict = {
        "plan_type":          "investigation",
        "vendor_id":          vendor_id,
        "candidate_key":      result.raw_input,
        "depth":              depth,
        "steps":              list(plan.steps),
        "fraud_risk":         fraud,
        "require_human_gate": plan.require_human_gate,
        "reason":             plan.reason,
        "focus":              getattr(plan, "focus", "") or "",
        "planned_at":         _now_iso(),
    }
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True)
    body = f"\n# Investigation Plan — {canonical_name}\n"
    if plan.reason:
        body += f"\n{plan.reason}\n"
    focus = getattr(plan, "focus", "")
    if focus:
        body += f"\n**Focus:** {focus}\n"
    atomic_write(
        path,
        f"---\n{fm_yaml}---\n{body}",
        vendor_id=vendor_id,
        programme_id=programme_id,
    )
    return path


# ---------------------------------------------------------------------------
# build_workspace
# ---------------------------------------------------------------------------

def build_workspace(
    result: IntakeResult,
    programme_id: str,
    extracted_terms: dict | None = None,
    erp_signal: ErpSignal | None = None,
) -> WorkspaceBuildResult:
    """Create the vendor workspace directory and single .md file on CONFIRMED intake."""
    if result.status != IntakeStatus.CONFIRMED:
        raise ValueError(
            f"build_workspace only accepts CONFIRMED results, got {result.status}"
        )

    vendor_id = result.vendor_id
    canonical_name = result.canonical_name or result.raw_input
    slug = _make_slug(canonical_name)

    workspace_path = _ws_root() / programme_id / vendor_id
    workspace_path.mkdir(parents=True, exist_ok=True)

    pcs_score = _compute_pcs(erp_signal, extracted_terms)
    band = _pcs_band(pcs_score)

    # Financial section
    if erp_signal and erp_signal.spend is not None:
        financial = {
            "spend_status": "OBSERVED",
            "annual_spend": {
                "value": str(erp_signal.spend),
                "confidence": "HIGH",
            },
            "currency": getattr(erp_signal, "currency", None),
        }
    else:
        financial = {
            "spend_status": "INFERRED",
            "annual_spend": {
                "value": None,
                "confidence": "INSUF",
            },
            "currency": None,
        }

    # Legal section
    if extracted_terms:
        renewal = extracted_terms.get("renewal_date")
        contract_val = extracted_terms.get("contract_value")
        legal = {
            "renewal_date": {
                "value": renewal,
                "confidence": "HIGH" if renewal is not None else "INSUF",
            },
            "contract_value": {
                "value": contract_val,
                "confidence": "HIGH" if contract_val is not None else "INSUF",
            },
        }
    else:
        legal = {
            "renewal_date": {"value": None, "confidence": "INSUF"},
            "contract_value": {"value": None, "confidence": "INSUF"},
        }

    # Classification
    erp_cat = (erp_signal.category if erp_signal else None) or result.erp_category
    if erp_cat:
        classification = {
            "category": {"value": erp_cat, "confidence": "HIGH", "source": "ERP"}
        }
    else:
        classification = {
            "category": {"value": None, "confidence": "INSUF", "source": None}
        }

    # Commercial documents (only when extracted_terms provided)
    documents = []
    if extracted_terms and result.linked_doc_ids:
        for doc_id in result.linked_doc_ids:
            documents.append({
                "doc_id": doc_id,
                "type": "CONTRACT_DOCUMENT",
                "doc_type": extracted_terms.get("doc_type"),
            })

    change_log = [{
        "event": "INTAKE_COMPLETED",
        "at": _now_iso(),
        "pcs_score": pcs_score,
    }]

    data: dict = {
        "vendor_id": vendor_id,
        "vendor_name": canonical_name,
        "canonical_name": canonical_name,
        "programme_id": programme_id,
        "slug": slug,
        "status": "INTAKE_COMPLETED",
        "intake": {
            "input_name": result.raw_input,
            "resolution_method": result.resolution_method,
            "data_class": result.data_class,
            "confidence": result.confidence,
            "entity_type": result.entity_type,
            "country_code": result.country_code,
        },
        "financial": financial,
        "legal": legal,
        "pcs": {"score": pcs_score, "band": band},
        "classification": classification,
        "commercial": {"documents": documents},
        "change_log": change_log,
    }

    data["enrichment"] = None
    data["relationship"] = None

    file_path = workspace_path / f"{slug}_profile.md"
    fm_yaml = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    body = f"\n# {canonical_name}\n\nWorkspace created at intake completion.\n"
    atomic_write(
        file_path,
        f"---\n{fm_yaml}---\n{body}",
        vendor_id=vendor_id,
        programme_id=programme_id,
    )

    # Insert vendor row into DB projection (idempotent — safe on re-run)
    insert_vendor(
        vendor_id=vendor_id,
        programme_id=programme_id,
        vendor_name=canonical_name,
        input_name=result.raw_input,
        data_class=result.data_class or "CLASS_D",
        identity_confidence=result.confidence or 0.0,
    )

    # Write plans/investigation_plan.md
    ip_path = _write_investigation_plan_file(result, vendor_id, programme_id, workspace_path, canonical_name)

    return WorkspaceBuildResult(
        success=True,
        workspace_path=workspace_path,
        files_written=[str(file_path), str(ip_path)],
    )


# ---------------------------------------------------------------------------
# write_vendor_profile  (Process 2 — enrichment)
# ---------------------------------------------------------------------------

def write_vendor_profile(
    profile: object,
    programme_id: str,
    vendor_id: str,
    workspace_root: Path,
) -> Path:
    """Merge P2 enrichment data into the consolidated {slug}_profile.md.

    Reads the existing consolidated file, sets the enrichment: key to the full
    profile dict, updates status and change_log, then writes back atomically.
    Returns the path to the consolidated file.
    """
    from cobalt.core.file_system import _find_vendor_file, read_md

    vendor_dir = Path(workspace_root) / programme_id / vendor_id
    vendor_dir.mkdir(parents=True, exist_ok=True)

    profile_data = profile.to_dict() if hasattr(profile, "to_dict") else {}
    canonical = getattr(profile, "canonical_name", vendor_id)
    now = getattr(profile, "enriched_at", None) or _now_iso()
    profile_status = getattr(profile, "profile_status", "ENRICHED")
    pcs_meta = getattr(profile, "enrichment_metadata", {}) or {}

    # Find (or default-name) the consolidated profile file
    existing_path = _find_vendor_file(programme_id, vendor_id, workspace_root)
    if existing_path is None:
        existing_path = vendor_dir / f"{vendor_id}_profile.md"

    existing = read_md(existing_path) or {}

    change_log = list(existing.get("change_log") or [])
    change_log.append({
        "event": "ENRICHMENT_COMPLETED",
        "enriched_at": now,
        "profile_status": profile_status,
        "pcs_before": pcs_meta.get("pcs_before", 0),
        "pcs_after": pcs_meta.get("pcs_after", 0),
    })
    existing["status"] = profile_status
    existing["change_log"] = change_log
    existing["enrichment"] = profile_data

    fm_yaml = yaml.dump(existing, default_flow_style=False, allow_unicode=True)
    intake_canonical = existing.get("canonical_name", canonical)
    atomic_write(
        existing_path,
        f"---\n{fm_yaml}---\n\n# {intake_canonical}\n\nUpdated by P2 enrichment at {now}.\n",
        vendor_id=vendor_id,
        programme_id=programme_id,
    )

    return existing_path


# ---------------------------------------------------------------------------
# write_rs_profile  (Process 3 — relationship & spend)
# ---------------------------------------------------------------------------

def write_rs_profile(
    relationship_data: dict,
    programme_id: str,
    vendor_id: str,
) -> Path:
    """Merge P3 relationship & spend data into the consolidated {slug}_profile.md.

    Reads the existing consolidated file, sets the relationship: key to the
    provided dict, updates status and change_log, then writes back atomically.
    Returns the path to the consolidated file.
    """
    from cobalt.core.file_system import _find_vendor_file, read_md

    existing_path = _find_vendor_file(programme_id, vendor_id)
    if existing_path is None:
        vendor_dir = _ws_root() / programme_id / vendor_id
        vendor_dir.mkdir(parents=True, exist_ok=True)
        existing_path = vendor_dir / f"{vendor_id}_profile.md"

    existing = read_md(existing_path) or {}
    now = relationship_data.get("last_updated") or _now_iso()

    change_log = list(existing.get("change_log") or [])
    change_log.append({
        "event": "RS_COMPLETED",
        "rs_completed_at": now,
        "profile_status": relationship_data.get("profile_status"),
        "pcs_total": relationship_data.get("pcs_total"),
    })
    existing["status"] = "RS_COMPLETED"
    existing["change_log"] = change_log
    existing["relationship"] = relationship_data

    intake_canonical = existing.get("canonical_name", vendor_id)
    fm_yaml = yaml.dump(existing, default_flow_style=False, allow_unicode=True)
    atomic_write(
        existing_path,
        f"---\n{fm_yaml}---\n\n# {intake_canonical}\n\nUpdated by P3 relationship & spend at {now}.\n",
        vendor_id=vendor_id,
        programme_id=programme_id,
    )

    return existing_path


# ---------------------------------------------------------------------------
# append_enrichment_ledger_entry
# ---------------------------------------------------------------------------

def append_enrichment_ledger_entry(
    programme_id: str,
    vendor_id: str,
    workspace_root: Path,
    profile_status: str,
    overall_confidence: str,
    depth_tier: str,
    sources_used: list,
    flags: list,
    pcs_before: float,
    pcs_after: float,
    now: str,
) -> None:
    """Append an ENRICHMENT_COMPLETED entry to the vendor ledger."""
    from cobalt.core.atomic_write import append_md

    ledger = (
        Path(workspace_root) / programme_id / vendor_id / "execution" / "ledger.md"
    )
    entry = (
        f"### ENRICHMENT_COMPLETED — {now}\n"
        f"profile_status: {profile_status}\n"
        f"overall_confidence: {overall_confidence}\n"
        f"depth_tier: {depth_tier}\n"
        f"pcs_before: {pcs_before}\n"
        f"pcs_after: {pcs_after}\n"
        f"flags: {', '.join(flags)}\n"
    )
    append_md(ledger, entry, vendor_id=vendor_id, programme_id=programme_id)
