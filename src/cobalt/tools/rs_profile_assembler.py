"""Tool 5 (Process 3) — rs_profile_assembler.

Merge all P3 outputs into the canonical relationship_spend_profile.md.
Atomic write. PCS update. The ONLY P3 tool that writes to the workspace.

No LLM. No external calls. Pure assembly and atomic write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from cobalt.core import gap_analyzer
from cobalt.core.atomic_write import append_md, atomic_write
from cobalt.core.exceptions import LedgerWriteError
from cobalt.core.file_system import coverage_path, ledger_path, rs_profile_path
from cobalt.models.schemas.rs_schema import (
    ContractTerms,
    DocumentIntelligenceResult,
    GapReport,
    RelationshipClassification,
    RelationshipSpendProfile,
    SpendAggregationResult,
    SpendSummary,
    StructuredDataBundle,
)

logger = logging.getLogger(__name__)

# Required P3 fields for gap analysis
_REQUIRED_FIELDS = [
    "spend_total_ttm_usd",
    "relationship_type",
    "dependency_tier",
]

_AGE_THRESHOLDS: dict[str, int] = {
    "last_updated": 30,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# PCS contribution
# ---------------------------------------------------------------------------

def _compute_pcs_contribution(
    spend_summary: SpendSummary,
    classification: RelationshipClassification,
    doc_intelligence: DocumentIntelligenceResult,
) -> float:
    """Compute P3 PCS contribution. Max 0.20."""
    contribution = 0.0

    # Spend data component (exclusive)
    if spend_summary.data_completeness == "FULL":
        contribution += 0.10
    elif spend_summary.data_completeness in ("PARTIAL", "SPARSE"):
        contribution += 0.06

    # Contract present
    has_contract = any(
        ct.total_value is not None or ct.effective_date is not None
        for ct in doc_intelligence.extracted_contracts
    )
    if has_contract:
        contribution += 0.05

    # Classification done
    if classification.relationship_type != "UNKNOWN":
        contribution += 0.03

    # High confidence
    if classification.classification_confidence == "HIGH":
        contribution += 0.02

    return min(round(contribution, 4), 0.20)


# ---------------------------------------------------------------------------
# Conflict reconciliation
# ---------------------------------------------------------------------------

def _reconcile_conflicts(
    spend_summary: SpendSummary,
    contract_terms: list[ContractTerms],
    classification: RelationshipClassification,
) -> list[str]:
    """Check for semantic inconsistencies and return flag list."""
    flags: list[str] = []

    total = spend_summary.total_usd_all_time
    has_spend_records = spend_summary.invoice_count > 0

    # CONTRACT_DEVIATION
    contract_values = [ct.total_value for ct in contract_terms if ct.total_value is not None]
    if contract_values and total is not None:
        contract_sum = sum(contract_values)
        if contract_sum > 0:
            deviation = abs(total - contract_sum) / contract_sum
            if deviation > 0.20:
                flags.append("CONTRACT_DEVIATION")

    # UNCOVERED_SPEND
    if has_spend_records and not contract_terms:
        flags.append("UNCOVERED_SPEND")

    # SPEND_BELOW_CONTRACT
    if (total is None or total == 0) and contract_values:
        flags.append("SPEND_BELOW_CONTRACT")

    # CLASSIFICATION_INCOMPLETE
    if classification.relationship_type == "UNKNOWN":
        flags.append("CLASSIFICATION_INCOMPLETE")

    # CONTRACT_RENEWAL_URGENT
    if classification.renewal_urgency == "URGENT":
        flags.append("CONTRACT_RENEWAL_URGENT")

    # LOW_DATA_CONFIDENCE — all spend records LOW/UNMATCHED match confidence
    # (no direct access to raw records here; checked via aggregation confidence)
    if spend_summary.confidence in ("LOW", "NONE") and has_spend_records:
        flags.append("LOW_DATA_CONFIDENCE")

    return flags


# ---------------------------------------------------------------------------
# Gap severity elevation
# ---------------------------------------------------------------------------

def _compute_assembled_gap_severity(
    gap_report: GapReport,
    data_completeness: str,
) -> str:
    """Apply CRITICAL elevation: MAJOR + NONE completeness → CRITICAL."""
    if gap_report.gap_severity == "MAJOR" and data_completeness == "NONE":
        return "CRITICAL"
    return gap_report.gap_severity


# ---------------------------------------------------------------------------
# Profile status
# ---------------------------------------------------------------------------

def _classify_profile_status(
    spend_summary: SpendSummary,
    classification: RelationshipClassification,
    assembled_gap_severity: str,
) -> str:
    if spend_summary.data_completeness == "NONE" and not spend_summary.invoice_count:
        return "MINIMAL"
    if (
        spend_summary.data_completeness == "FULL"
        and classification.relationship_type != "UNKNOWN"
        and assembled_gap_severity in ("NONE", "MINOR")
    ):
        return "COMPLETE"
    if spend_summary.data_completeness in ("PARTIAL", "SPARSE"):
        return "PARTIAL"
    if assembled_gap_severity == "MINOR":
        return "PARTIAL"
    return "MINIMAL"


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def _build_data_sources_list(
    bundle: StructuredDataBundle,
    doc_result: DocumentIntelligenceResult,
) -> list[str]:
    sources: list[str] = []
    for r in bundle.raw_spend_records:
        if r.source_id and r.source_id not in sources:
            sources.append(r.source_id)
    for ct in doc_result.extracted_contracts:
        if ct.document_id and ct.document_id not in sources:
            sources.append(ct.document_id)
    return sources


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------

def _read_prior_version(path: Path) -> tuple[int, str | None]:
    """Return (version, created_at) from prior profile, or (0, None) if none."""
    if not path.exists():
        return 0, None
    try:
        content = path.read_text(encoding="utf-8")
        parts = content.split("---\n", 2)
        if len(parts) >= 3:
            import yaml as _yaml
            data = _yaml.safe_load(parts[1]) or {}
            return data.get("profile_version", 0), data.get("created_at")
    except Exception:
        pass
    return 0, None


# ---------------------------------------------------------------------------
# Markdown builder
# ---------------------------------------------------------------------------

def _build_rs_profile_md(profile: RelationshipSpendProfile) -> str:
    """Render the full relationship_spend_profile.md content."""
    summary = profile.spend_summary
    cls = profile.relationship_classification

    # YAML front-matter
    fm_data = {
        "vendor_id":          profile.vendor_id,
        "programme_id":       profile.programme_id,
        "profile_version":    profile.profile_version,
        "created_at":         profile.created_at,
        "last_updated":       profile.last_updated,
        "pcs_contribution":   profile.pcs_contribution,
        "pcs_total":          profile.pcs_total,
        "dependency_tier":    cls.dependency_tier,
        "relationship_type":  cls.relationship_type,
        "spend_total_ttm_usd": summary.total_usd_ttm,
        "contract_count":     profile.contract_count,
        "flags":              profile.flags,
    }
    fm_yaml = yaml.dump(fm_data, default_flow_style=False, allow_unicode=True)

    lines: list[str] = [f"---\n{fm_yaml}---\n"]

    # Spend Summary section
    lines.append("\n## Spend Summary\n")
    lines.append(f"- **Total (all time):** ${summary.total_usd_all_time or 0:,.0f}\n")
    lines.append(f"- **TTM:** ${summary.total_usd_ttm or 0:,.0f}\n")
    lines.append(f"- **YTD:** ${summary.total_usd_ytd or 0:,.0f}\n")
    lines.append(f"- **Completeness:** {summary.data_completeness}\n")
    lines.append(f"- **Confidence:** {summary.confidence}\n")

    if summary.by_period:
        lines.append("\n### Period Breakdown\n\n| Period | Spend USD |\n|---|---|\n")
        for period, amt in summary.by_period.items():
            lines.append(f"| {period} | ${amt:,.0f} |\n")

    if summary.by_category:
        lines.append("\n### Category Breakdown\n\n| Category | Spend USD |\n|---|---|\n")
        for cat, amt in summary.by_category.items():
            lines.append(f"| {cat} | ${amt:,.0f} |\n")

    if profile.spend_summary.anomalies if hasattr(profile, '_anomalies') else False:
        pass  # handled below via profile dict

    # Contract Terms section
    lines.append("\n## Contract Terms\n")
    if not profile.contract_terms:
        lines.append("*No contracts extracted.*\n")
    for i, ct in enumerate(profile.contract_terms, 1):
        eff = ct.effective_date or "?"
        exp = ct.expiry_date or "?"
        lines.append(f"\n### Contract {i}: {ct.document_type} ({eff} – {exp})\n\n")
        lines.append(f"- **Value:** {ct.total_value} {ct.currency or ''}\n")
        lines.append(f"- **Payment terms:** {ct.payment_terms_days} days\n")
        lines.append(f"- **Auto-renewal:** {ct.auto_renews}\n")
        lines.append(f"- **Notice period:** {ct.notice_period_days} days\n")
        lines.append(f"- **Governing law:** {ct.governing_law}\n")
        lines.append(f"- **SLA:** {ct.sla_summary}\n")
        lines.append(f"- **Extraction confidence:** {ct.extraction_confidence}\n")
        if ct.key_obligations:
            lines.append("- **Obligations:** " + "; ".join(ct.key_obligations) + "\n")
        if ct.termination_clauses:
            lines.append("- **Termination:** " + "; ".join(ct.termination_clauses) + "\n")

    # Relationship Classification section
    lines.append("\n## Relationship Classification\n\n")
    lines.append(f"| Field | Value |\n|---|---|\n")
    lines.append(f"| Type | {cls.relationship_type} |\n")
    lines.append(f"| Dependency score | {cls.dependency_score:.4f} |\n")
    lines.append(f"| Dependency tier | {cls.dependency_tier} |\n")
    lines.append(f"| Single source risk | {cls.single_source_risk} |\n")
    lines.append(f"| Contract coverage | {cls.contract_coverage} |\n")
    lines.append(f"| Renewal urgency | {cls.renewal_urgency} |\n")
    lines.append(f"| Relationship age | {cls.relationship_age_days} days |\n")
    lines.append(f"| Classification confidence | {cls.classification_confidence} |\n")
    lines.append(f"| LLM used | {cls.llm_used} |\n")
    if cls.reasoning:
        lines.append(f"\n*Reasoning: {cls.reasoning}*\n")

    # Data Quality section
    lines.append("\n## Data Quality\n\n")
    lines.append(f"- **Completeness:** {summary.data_completeness}\n")
    gr = GapReport.from_dict(profile.gap_report)
    if gr.missing_fields:
        lines.append(f"- **Missing fields:** {', '.join(gr.missing_fields)}\n")
    if gr.low_confidence_fields:
        lines.append(f"- **Low confidence:** {', '.join(gr.low_confidence_fields)}\n")

    # Gaps section
    lines.append("\n## Gaps\n\n")
    lines.append(f"- **Gap severity:** {gr.gap_severity}\n")
    if gr.missing_fields:
        lines.append(f"- **Missing:** {', '.join(gr.missing_fields)}\n")
    if gr.recommended_actions:
        lines.append("- **Actions:** " + "; ".join(gr.recommended_actions) + "\n")

    return "".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def assemble_rs_profile(
    vendor_id: str,
    programme_id: str,
    structured_bundle: StructuredDataBundle,
    doc_intelligence: DocumentIntelligenceResult,
    spend_aggregation: SpendAggregationResult,
    classification: RelationshipClassification,
    current_pcs: float,
) -> RelationshipSpendProfile:
    """Assemble the full P3 profile and write to workspace.

    Never raises (except LedgerWriteError from append_md which must HALT).
    """
    profile_path = rs_profile_path(programme_id, vendor_id)
    now = _now_iso()

    try:
        # Step 1: contract_count
        contract_count = len(doc_intelligence.extracted_contracts)

        # Step 2: version management
        prior_version, prior_created_at = _read_prior_version(profile_path)
        profile_version = prior_version + 1
        created_at = prior_created_at or now

        # Step 3: Build gap analysis inputs
        summary = spend_aggregation.summary
        gap_profile_dict: dict = {
            "spend_total_ttm_usd": summary.total_usd_ttm,
            "relationship_type":   classification.relationship_type if classification.relationship_type != "UNKNOWN" else None,
            "dependency_tier":     classification.dependency_tier,
            "contract_count":      contract_count if contract_count > 0 else None,
            "renewal_urgency":     classification.renewal_urgency if classification.renewal_urgency != "UNKNOWN" else None,
            "spend_total_all_time_usd": summary.total_usd_all_time,
            "last_updated":        now,
        }

        gap_report = gap_analyzer.analyse_gaps(
            profile_dict=gap_profile_dict,
            required_fields=_REQUIRED_FIELDS,
            age_thresholds=_AGE_THRESHOLDS,
        )

        # Step 4: Assembled gap severity (CRITICAL elevation)
        assembled_gap_severity = _compute_assembled_gap_severity(gap_report, summary.data_completeness)

        # Step 5: Conflict reconciliation
        flags = _reconcile_conflicts(summary, doc_intelligence.extracted_contracts, classification)

        # Step 6: PCS
        pcs_contribution = _compute_pcs_contribution(summary, classification, doc_intelligence)
        pcs_total = min(1.0, current_pcs + pcs_contribution)

        # Step 7: Profile status
        profile_status = _classify_profile_status(summary, classification, assembled_gap_severity)

        # Step 8: Data sources
        data_sources = _build_data_sources_list(structured_bundle, doc_intelligence)

        # Assemble profile object
        profile = RelationshipSpendProfile(
            vendor_id=vendor_id,
            programme_id=programme_id,
            profile_version=profile_version,
            profile_status=profile_status,
            created_at=created_at,
            last_updated=now,
            contract_count=contract_count,
            spend_summary=summary,
            contract_terms=doc_intelligence.extracted_contracts,
            relationship_classification=classification,
            gap_report=gap_report.to_dict(),
            pcs_contribution=pcs_contribution,
            pcs_total=pcs_total,
            flags=flags,
            data_sources=data_sources,
        )

        # Step 8: Write markdown
        md_content = _build_rs_profile_md(profile)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            profile_path,
            md_content,
            vendor_id=vendor_id,
            programme_id=programme_id,
        )

        # Step 9: DB sync (explicit — not auto-triggered)
        try:
            from cobalt.db.sync_to_db import sync_to_db
            sync_to_db(profile_path, vendor_id, programme_id)
        except Exception as exc:
            logger.warning("sync_to_db failed for %s: %s", profile_path, exc)

        # Step 10: Ledger entry (LedgerWriteError propagates — HALT per Rule 4)
        ledger = ledger_path(programme_id, vendor_id)
        ledger_entry = (
            f"## P3 Profile Assembled\n\n"
            f"- **Version:** {profile_version}\n"
            f"- **Status:** {profile_status}\n"
            f"- **Relationship type:** {classification.relationship_type}\n"
            f"- **PCS contribution:** {pcs_contribution}\n"
            f"- **PCS total:** {pcs_total}\n"
            f"- **Flags:** {', '.join(flags) if flags else 'none'}\n"
            f"- **Timestamp:** {now}\n"
        )
        append_md(ledger, ledger_entry, vendor_id=vendor_id, programme_id=programme_id)

        return profile

    except LedgerWriteError:
        raise  # HALT per Rule 4

    except Exception as exc:
        logger.exception("Profile assembly failed for %s: %s", vendor_id, exc)
        _write_minimal_failed_profile(profile_path, vendor_id, str(exc), now)
        # Return a minimal failed profile
        return RelationshipSpendProfile(
            vendor_id=vendor_id,
            programme_id=programme_id,
            profile_version=1,
            profile_status="FAILED",
            created_at=now,
            last_updated=now,
            contract_count=0,
            spend_summary=spend_aggregation.summary,
            contract_terms=[],
            relationship_classification=classification,
            gap_report={},
            pcs_contribution=0.0,
            pcs_total=current_pcs,
            flags=["PROFILE_ASSEMBLY_FAILED"],
            data_sources=[],
        )


def _write_minimal_failed_profile(
    path: Path,
    vendor_id: str,
    error: str,
    now: str,
) -> None:
    """Write a minimal FAILED profile. Best-effort — swallows exceptions."""
    try:
        fm_data = {
            "vendor_id":      vendor_id,
            "profile_version": 1,
            "last_updated":   now,
            "pcs_contribution": 0.0,
            "flags":          ["PROFILE_ASSEMBLY_FAILED"],
            "error":          error[:500],
        }
        fm_yaml = yaml.dump(fm_data, default_flow_style=False, allow_unicode=True)
        content = f"---\n{fm_yaml}---\n\n# Profile Assembly Failed\n\nError: {error[:500]}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, content)
    except Exception:
        pass
