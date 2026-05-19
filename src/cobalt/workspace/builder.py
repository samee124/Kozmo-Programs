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
        "canonical_name": canonical_name,
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

    file_path = workspace_path / f"{slug}.md"
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

    return WorkspaceBuildResult(
        success=True,
        workspace_path=workspace_path,
        files_written=[str(file_path)],
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
    """Update (or create) the vendor .md file with enrichment profile data."""
    from cobalt.core.file_system import _find_vendor_file, read_md

    file_path = _find_vendor_file(programme_id, vendor_id, workspace_root)
    if file_path is None:
        vendor_dir = Path(workspace_root) / programme_id / vendor_id
        vendor_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9_]", "_", vendor_id.lower().lstrip("v-"))
        file_path = vendor_dir / f"{slug or 'vendor'}_profile.md"

    existing = read_md(file_path) or {}

    profile_data = profile.to_dict() if hasattr(profile, "to_dict") else {}

    updated = {**existing, **profile_data}
    # Expose profile_status as top-level status so callers can check fm["status"]
    updated["status"] = getattr(profile, "profile_status", updated.get("status", ""))

    change_log = list(updated.get("change_log") or [])
    change_log.append({
        "event": "ENRICHMENT_COMPLETED",
        "enriched_at": getattr(profile, "enriched_at", _now_iso()),
        "profile_status": getattr(profile, "profile_status", ""),
        "pcs_before": (getattr(profile, "enrichment_metadata", {}) or {}).get("pcs_before", 0),
        "pcs_after": (getattr(profile, "enrichment_metadata", {}) or {}).get("pcs_after", 0),
    })
    updated["change_log"] = change_log

    fm_yaml = yaml.dump(updated, default_flow_style=False, allow_unicode=True)
    canonical = getattr(profile, "canonical_name", vendor_id)
    body = f"\n# {canonical}\n\nEnrichment profile.\n"
    atomic_write(
        file_path,
        f"---\n{fm_yaml}---\n{body}",
        vendor_id=vendor_id,
        programme_id=programme_id,
    )
    return file_path


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
