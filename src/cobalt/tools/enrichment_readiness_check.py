"""Tool 1 (Process 2) — enrichment_readiness_check: gate before external enrichment.

Reads entity.md, coverage.md, and spend.md from the vendor workspace.
Applies gate rules and returns an EnrichmentReadinessResult. No network calls,
no LLM calls, no workspace writes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from cobalt.core.exceptions import EnrichmentReadinessReadError
from cobalt.models.schemas.enrichment_schema import EnrichmentReadinessResult, KnownFacts

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CANONICAL_FIELDS: list[str] = [
    "canonical_name", "domain", "website", "hq_country", "hq_city",
    "founding_year", "company_status", "category", "subcategory",
    "vendor_type", "employee_count_range", "company_size_band",
    "funding_stage", "description", "parent_company",
]

_CONFIRMED_CONFIDENCE: frozenset[str] = frozenset({"HIGH", "MEDIUM", "OBSERVED", "CONFIRMED"})
_CONFLICT_CONFIDENCE:  frozenset[str] = frozenset({"CONFLICT"})
_GAP_CONFIDENCE:       frozenset[str] = frozenset({"LOW", "INFERRED", "MISSING"})

_TIER_SOURCES: dict[str, tuple[list[str], int]] = {
    "BASIC":       (["company_website"], 1),
    "STANDARD":    (["web_search", "company_website", "linkedin", "news",
                     "wikidata", "wikipedia", "gleif", "opensanctions",
                     "search_discovery"], 2),
    "DEEP":        (["web_search", "company_website", "linkedin",
                     "registry", "financial", "news", "wikidata", "wikipedia",
                     "gleif", "opensanctions", "search_discovery"], 3),
    "PROVISIONAL": (["web_search"], 1),
}

_OVERRIDE_TRIGGERS: frozenset[str] = frozenset({
    "REBRAND_DETECTED", "CONFLICTING_FACTS",
    "SPEND_TIER_CROSSED", "DATA_CLASS_UPGRADE",
})


# ---------------------------------------------------------------------------
# Private internal dataclasses
# ---------------------------------------------------------------------------

@dataclass
class _GateOutcome:
    blocked:       bool
    allowed_depth: str | None
    flags:         list[str]


@dataclass
class _StalenessOutcome:
    skip:             bool
    skip_reason:      str | None = None
    last_enriched_at: str | None = None


# ---------------------------------------------------------------------------
# Private helpers — workspace file reading
# ---------------------------------------------------------------------------

def _resolve_workspace_root(workspace_root: Path | None) -> Path:
    if workspace_root is not None:
        return workspace_root
    return Path(os.getenv("WORKSPACE_ROOT", "./workspace"))


def _read_markdown_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a markdown workspace file.

    Returns None if the file does not exist.
    Returns an empty dict if the file exists but contains no data.
    Raises EnrichmentReadinessReadError if the file contains invalid YAML.
    """
    if not path.exists():
        return None

    content = path.read_text(encoding="utf-8")

    # Try frontmatter block (---...---) first; fall back to whole-file YAML.
    parts = content.split("---\n", 2)
    raw_yaml = parts[1] if len(parts) >= 3 else content

    try:
        result = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise EnrichmentReadinessReadError(
            f"Malformed YAML in {path}: {exc}"
        ) from exc

    if result is None:
        return {}
    if not isinstance(result, dict):
        raise EnrichmentReadinessReadError(
            f"Expected mapping from {path}, got {type(result).__name__}"
        )
    return result


def _find_single_vendor_file(vp: Path) -> Path | None:
    """Find the single *.md vendor file directly in vp (not subdirs)."""
    if not vp.is_dir():
        return None
    md_files = [f for f in vp.iterdir() if f.suffix == ".md" and f.is_file()]
    return md_files[0] if md_files else None


def _flatten_vendor_data(data: dict) -> dict:
    """Flatten a nested vendor file to the flat keys expected by readiness checks.

    If data is already flat (old test format or legacy files), returns as-is.
    If it has nested sections (new single-file format), flattens them.
    """
    if not data:
        return {}
    # Flat detection: new-format files store confidence inside 'intake'
    if "confidence" in data or "identity_confidence" in data:
        return data

    intake = data.get("intake") or {}
    identity = data.get("identity") or {}
    classification = data.get("classification") or {}
    operational = data.get("operational") or {}
    financial = data.get("financial") or {}
    digital = data.get("digital") or {}
    pcs = data.get("pcs") or {}
    compliance = data.get("compliance") or {}

    flat: dict[str, Any] = {
        "confidence":         intake.get("confidence"),
        "identity_confidence": intake.get("confidence") or intake.get("identity_confidence"),
        "status":             "CONFIRMED",
        "canonical_name":     data.get("canonical_name"),
        "last_enriched_at":   data.get("last_enriched_at"),
        "overall_pcs":        float(pcs.get("score", 0)),
        "pcs_band":           pcs.get("band"),
        "data_class":         intake.get("data_class"),
        "flags":              compliance.get("flags") or [],
    }

    # Identity fields
    for fname in ("hq_country", "hq_city", "website", "description",
                  "founding_year", "company_status"):
        fd = identity.get(fname)
        _unpack_field(flat, fname, fd)

    # Digital
    fd = digital.get("domain")
    _unpack_field(flat, "domain", fd)

    # Classification
    for fname in ("category", "subcategory", "industry", "primary_use_case"):
        fd = classification.get(fname)
        _unpack_field(flat, fname, fd)

    # Operational
    for fname in ("vendor_type", "employee_count_range", "company_size_band", "parent_company"):
        fd = operational.get(fname)
        _unpack_field(flat, fname, fd)

    # Financial
    for fname in ("funding_stage", "revenue_range"):
        fd = financial.get(fname)
        _unpack_field(flat, fname, fd)

    annual_spend_fd = financial.get("annual_spend")
    flat["annual_spend"] = (
        annual_spend_fd.get("value") if isinstance(annual_spend_fd, dict) else annual_spend_fd
    )
    flat["total_spend_tier"] = financial.get("total_spend_tier")

    return {k: v for k, v in flat.items() if v is not None}


def _unpack_field(flat: dict, fname: str, fd: Any) -> None:
    """Unpack a {value, source, confidence} field dict into flat keys."""
    if isinstance(fd, dict):
        val = fd.get("value")
        conf = fd.get("confidence")
        flat[fname] = val
        if conf and conf not in ("INSUF", None):
            flat[f"{fname}_confidence"] = conf
    else:
        flat[fname] = fd


def _read_entity(vendor_path: Path) -> dict[str, Any] | None:
    # Try new single-file location (*.md directly in vendor_path)
    single = _find_single_vendor_file(vendor_path)
    if single is not None:
        data = _read_markdown_frontmatter(single)
        if data is not None:
            return _flatten_vendor_data(data)
    # Fall back to legacy entity.md
    return _read_markdown_frontmatter(vendor_path / "identity" / "entity.md")


def _read_coverage(vendor_path: Path) -> dict[str, Any] | None:
    # Try new single-file location
    single = _find_single_vendor_file(vendor_path)
    if single is not None:
        data = _read_markdown_frontmatter(single)
        if data is not None:
            return _flatten_vendor_data(data)
    # Fall back to legacy coverage.md
    return _read_markdown_frontmatter(vendor_path / "cost_file" / "coverage.md")


def _read_spend(vendor_path: Path) -> dict[str, Any] | None:
    # Try new single-file location
    single = _find_single_vendor_file(vendor_path)
    if single is not None:
        data = _read_markdown_frontmatter(single)
        if data is not None:
            return _flatten_vendor_data(data)
    # Fall back to legacy spend.md
    return _read_markdown_frontmatter(vendor_path / "cost_file" / "spend.md")


_CONTRACT_DOC_TYPES: frozenset[str] = frozenset({
    "MSA", "SOW", "AMENDMENT", "DPA", "FRAMEWORK", "LICENCE",
})
_CONTRACT_FILENAME_KEYWORDS: tuple[str, ...] = (
    "msa", "contract", "agreement", "sow", "dpa", "nda", "framework", "licence",
)
_CONTRACT_DIRS: tuple[str, ...] = ("evidence", "contracts", "uploads")

# Fields the DE pipeline extracts from contracts — RS fields are excluded.
_CONTRACT_DE_FIELDS: frozenset[str] = frozenset({
    "counterparty_legal_name", "counterparty_registration_number",
    "counterparty_jurisdiction", "counterparty_registered_address",
    "counterparty_governing_law", "contract_type",
})


def _read_contract_evidence(vendor_path: Path) -> list[dict]:
    """Scan vendor workspace for contract evidence usable by DE pipeline.

    Scenario A: reads document_extractions from relationship_spend_profile.md if RS-02
    has already run.  Scenario B: discovers uploaded PDF/DOCX contract files for
    later LLM extraction.  Returns [] on any error — never raises.
    """
    results: list[dict] = []
    try:
        # Scenario A — RS-02 already ran; reuse its extracted entity fields.
        rsp_path = vendor_path / "relationship" / "relationship_spend_profile.md"
        if rsp_path.exists():
            data = _read_markdown_frontmatter(rsp_path) or {}
            for item in (data.get("document_extractions") or []):
                if item.get("document_type") in _CONTRACT_DOC_TYPES:
                    entry: dict[str, Any] = {
                        "source": "rs_extracted",
                        "document_type": item.get("document_type"),
                    }
                    for field_name in _CONTRACT_DE_FIELDS:
                        if field_name in item:
                            entry[field_name] = item[field_name]
                    results.append(entry)

        # Scenario B — uploaded contract files awaiting extraction.
        for dir_name in _CONTRACT_DIRS:
            dir_path = vendor_path / dir_name
            if not dir_path.is_dir():
                continue
            for f in dir_path.iterdir():
                if f.suffix.lower() not in (".pdf", ".docx"):
                    continue
                if not any(kw in f.name.lower() for kw in _CONTRACT_FILENAME_KEYWORDS):
                    continue
                results.append({
                    "source": "uploaded_file",
                    "file_path": str(f),
                    "needs_extraction": True,
                })
    except Exception as exc:
        logger.warning("_read_contract_evidence failed for %s: %s", vendor_path, exc)
    return results


# ---------------------------------------------------------------------------
# Skill 1 — Identity confidence gate
# ---------------------------------------------------------------------------

def _identity_confidence_gate(entity_data: dict[str, Any], declared_depth: str) -> _GateOutcome:
    confidence: float = float(
        entity_data.get("identity_confidence") or entity_data.get("confidence") or 0.0
    )
    status: str = str(entity_data.get("status", ""))
    flags: list[str] = []

    if status in {"TRIAGE_REQUIRED", "UNRESOLVED"}:
        return _GateOutcome(blocked=True, allowed_depth=None, flags=["ENRICHMENT_BLOCKED"])

    if confidence >= 0.80:
        allowed_depth = declared_depth
    elif confidence >= 0.60:
        allowed_depth = "BASIC"
        flags.append("LOW_IDENTITY_CONFIDENCE")
        if declared_depth != "BASIC":
            flags.append("DEPTH_DOWNGRADED")
    else:
        allowed_depth = "PROVISIONAL"
        flags.append("LOW_IDENTITY_CONFIDENCE")

    return _GateOutcome(blocked=False, allowed_depth=allowed_depth, flags=flags)


# ---------------------------------------------------------------------------
# Skill 2 — Depth tier decision
# ---------------------------------------------------------------------------

def _depth_tier_decision(
    allowed_depth: str,
    entity_data: dict[str, Any],
    coverage_data: dict[str, Any] | None,
    spend_data: dict[str, Any] | None,
) -> tuple[str, list[str]]:
    if allowed_depth in ("PROVISIONAL", "BASIC"):
        return allowed_depth, []

    # allowed_depth in {"STANDARD", "DEEP"}
    data_class: str = str((coverage_data or {}).get("data_class", ""))
    spend_tier: str = str((spend_data or {}).get("total_spend_tier", ""))

    if data_class == "CLASS_A" or spend_tier == "TIER_1":
        return "DEEP", []

    return allowed_depth, []


# ---------------------------------------------------------------------------
# Skill 3 — Staleness check
# ---------------------------------------------------------------------------

def _staleness_check(
    coverage_data: dict[str, Any] | None,
    triggers: list[str],
) -> _StalenessOutcome:
    if coverage_data is None:
        return _StalenessOutcome(skip=False, last_enriched_at=None)

    last_enriched: str | None = coverage_data.get("last_enriched_at")
    if last_enriched is None:
        return _StalenessOutcome(skip=False, last_enriched_at=None)

    if any(t in _OVERRIDE_TRIGGERS for t in triggers):
        return _StalenessOutcome(skip=False, last_enriched_at=last_enriched)

    last_dt = datetime.fromisoformat(str(last_enriched).replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    age_days = (now - last_dt).days

    if age_days < 90:
        return _StalenessOutcome(
            skip=True,
            skip_reason=f"Enriched {age_days} days ago, within 90-day staleness window",
            last_enriched_at=last_enriched,
        )

    return _StalenessOutcome(skip=False, last_enriched_at=last_enriched)


# ---------------------------------------------------------------------------
# Skill 4 — Internal fact review
# ---------------------------------------------------------------------------

def _internal_fact_review(
    entity_data: dict[str, Any],
    coverage_data: dict[str, Any] | None,
    spend_data: dict[str, Any] | None,
) -> KnownFacts:
    combined: dict[str, Any] = {}
    for src in (entity_data, coverage_data or {}, spend_data or {}):
        combined.update(src)

    confirmed: list[str] = []
    gaps: list[str] = []
    conflicts: list[str] = []

    for field_name in _CANONICAL_FIELDS:
        value = combined.get(field_name)
        if value is None:
            gaps.append(field_name)
            continue

        conf = str(combined.get(f"{field_name}_confidence", ""))

        if conf in _CONFLICT_CONFIDENCE:
            conflicts.append(field_name)
        elif conf in _GAP_CONFIDENCE:
            gaps.append(field_name)
        else:
            confirmed.append(field_name)

    return KnownFacts(confirmed=confirmed, gaps=gaps, conflicts=conflicts)


# ---------------------------------------------------------------------------
# Skill 5 — Source scope determination
# ---------------------------------------------------------------------------

def _source_scope(
    depth_tier: str,
    known_facts: KnownFacts,
    entity_data: dict[str, Any],
    coverage_data: dict[str, Any] | None,
    contract_evidence: list[dict] | None = None,
) -> tuple[list[str], int]:
    base_sources, base_count = _TIER_SOURCES.get(depth_tier, (["web_search"], 1))
    source_list: list[str] = list(base_sources)
    query_count: int = base_count

    entity_flags: list[str] = list(entity_data.get("flags") or [])

    if "MISSING_CATEGORY" in entity_flags or "category" in known_facts.gaps:
        if "web_search" not in source_list:
            source_list.append("web_search")
        query_count += 1

    if "POSSIBLY_DEFUNCT" in entity_flags:
        for src in ("registry", "news"):
            if src not in source_list:
                source_list.append(src)

    if contract_evidence:
        if "contract" not in source_list:
            source_list.append("contract")

    return source_list, query_count


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------

def _blocked_result(vendor_id: str, flag: str) -> EnrichmentReadinessResult:
    return EnrichmentReadinessResult(
        vendor_id=vendor_id,
        proceed=False,
        skip=False,
        skip_reason=None,
        depth_tier="PROVISIONAL",
        source_list=[],
        query_count=0,
        known_facts=KnownFacts(),
        confidence_floor=0.0,
        flags=[flag],
    )


def _build_result(
    *,
    vendor_id: str,
    proceed: bool,
    skip: bool,
    skip_reason: str | None,
    depth_tier: str,
    source_list: list[str],
    query_count: int,
    known_facts: KnownFacts,
    confidence_floor: float,
    flags: list[str],
    contract_evidence: list[dict] | None = None,
) -> EnrichmentReadinessResult:
    return EnrichmentReadinessResult(
        vendor_id=vendor_id,
        proceed=proceed,
        skip=skip,
        skip_reason=skip_reason,
        depth_tier=depth_tier,
        source_list=source_list,
        query_count=query_count,
        known_facts=known_facts,
        confidence_floor=confidence_floor,
        flags=flags,
        contract_evidence=contract_evidence or [],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_enrichment_readiness(
    vendor_id: str,
    programme_id: str,
    declared_depth: str = "STANDARD",
    workspace_root: Path | None = None,
    manual_override: bool = False,
    triggers: list[str] | None = None,
) -> EnrichmentReadinessResult:
    """Read entity.md, coverage.md, spend.md from the vendor workspace.
    Apply gate rules per spec. Return EnrichmentReadinessResult.

    Never raises on missing data — produces an EnrichmentReadinessResult
    with proceed=False and a descriptive flag.
    Raises EnrichmentReadinessReadError only for malformed (invalid YAML)
    workspace files.
    """
    workspace_root = _resolve_workspace_root(workspace_root)
    vendor_path = workspace_root / programme_id / vendor_id

    entity_data = _read_entity(vendor_path)
    coverage_data = _read_coverage(vendor_path)
    spend_data = _read_spend(vendor_path)
    contract_evidence = _read_contract_evidence(vendor_path)

    if entity_data is None:
        return _blocked_result(vendor_id, "MISSING_ENTITY_FILE")

    # Skill 1 — identity confidence gate
    gate = _identity_confidence_gate(entity_data, declared_depth)
    if gate.blocked:
        return _build_result(
            vendor_id=vendor_id,
            proceed=False,
            skip=False,
            skip_reason=None,
            depth_tier="PROVISIONAL",
            source_list=[],
            query_count=0,
            known_facts=KnownFacts(),
            confidence_floor=float(entity_data.get("identity_confidence") or entity_data.get("confidence") or 0.0),
            flags=gate.flags,
        )

    # Skill 3 — staleness check (before depth so SKIP short-circuits)
    if not manual_override:
        staleness = _staleness_check(coverage_data, triggers or [])
        if staleness.skip:
            return _build_result(
                vendor_id=vendor_id,
                proceed=False,
                skip=True,
                skip_reason=staleness.skip_reason,
                depth_tier="PROVISIONAL",
                source_list=[],
                query_count=0,
                known_facts=KnownFacts(),
                confidence_floor=float(entity_data.get("identity_confidence") or entity_data.get("confidence") or 0.0),
                flags=["SKIP"],
            )

    # Skill 2 — depth tier decision
    depth_tier, _ = _depth_tier_decision(
        gate.allowed_depth, entity_data, coverage_data, spend_data  # type: ignore[arg-type]
    )

    # Skill 4 — internal fact review
    known_facts = _internal_fact_review(entity_data, coverage_data, spend_data)

    # Skill 5 — source scope determination
    source_list, query_count = _source_scope(
        depth_tier, known_facts, entity_data, coverage_data,
        contract_evidence=contract_evidence,
    )

    # Skill 6 — assemble result
    all_flags = list(gate.flags)
    if contract_evidence:
        all_flags.append("CONTRACT_EVIDENCE_FOUND")

    return _build_result(
        vendor_id=vendor_id,
        proceed=True,
        skip=False,
        skip_reason=None,
        depth_tier=depth_tier,
        source_list=source_list,
        query_count=query_count,
        known_facts=known_facts,
        confidence_floor=float(entity_data.get("identity_confidence") or entity_data.get("confidence") or 0.0),
        flags=all_flags,
        contract_evidence=contract_evidence,
    )
