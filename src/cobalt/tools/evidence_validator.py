"""Tool 1 (Process 4) — evidence_validator.

Validates all incoming evidence before any reasoning begins. Scores source
confidence per item. Detects staleness, conflicts, and missing fields.
The quality gate — no reasoning runs on unvalidated evidence.

Writes to workspace: No — returns ValidatedEvidenceAssembly in memory.
LLM: None. Pure quality assessment.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from cobalt.core.staleness import is_stale
from cobalt.models.schemas.an_schema import (
    HistoricalEvidenceState,
    ValidatedEvidenceAssembly,
    ValidatedEvidenceFact,
)
from cobalt.models.schemas.rs_schema import (
    DocumentIntelligenceResult,
    StructuredDataBundle,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

TRUST_WEIGHTS: dict[str, float] = {
    "OFFICIAL":       1.00,
    "SYSTEM_EXPORT":  0.85,
    "USER_SUBMITTED": 0.65,
    "AI_INFERRED":    0.50,
}

RECENCY_WEIGHTS: dict[str, float] = {
    "CURRENT": 1.0,
    "STALE":   0.5,
    "MISSING": 0.0,
}

CONFLICT_PENALTY: dict[bool, float] = {
    True:  0.7,
    False: 1.0,
}

FRESHNESS_THRESHOLDS_DAYS: dict[str, int] = {
    "CONTRACT":       365,
    "MSA":            365,
    "SOW":            365,
    "SLA_EXHIBIT":     45,
    "COMPLIANCE_CERT": 180,
    "CHECK_IN":        30,
    "INVOICE":         90,
    "SPEND":           90,
    "QBR":             90,
    "DEFAULT":         90,
}

EXPECTED_FIELDS: frozenset[str] = frozenset({
    "contract_term_start",
    "contract_term_end",
    "auto_renew",
    "notice_period_days",
    "total_contract_value",
    "sla_terms",
    "primary_owner",
    "relationship_type",
    "dependency_tier",
    "spend_total_ttm_usd",
    "renewal_date",
})


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_quality_score(trust_level: str, freshness_status: str, conflict_flag: bool) -> float:
    """Compute quality score from trust, recency, and conflict.

    quality_score = trust_weight × recency_weight × conflict_penalty
    Clamped to [0.0, 1.0].
    """
    trust = TRUST_WEIGHTS.get(trust_level, 0.0)
    recency = RECENCY_WEIGHTS.get(freshness_status, 0.0)
    penalty = CONFLICT_PENALTY.get(conflict_flag, 1.0)
    return max(0.0, min(1.0, trust * recency * penalty))


def _freshness_threshold_for(extraction_type: str, source_file: str) -> int:
    """Determine freshness threshold days from extraction_type and source_file."""
    src = source_file.upper() if source_file else ""
    ext = extraction_type.upper() if extraction_type else ""

    if ext == "SIGNAL" or "CHECK" in src:
        return FRESHNESS_THRESHOLDS_DAYS["CHECK_IN"]
    if "INVOICE" in src:
        return FRESHNESS_THRESHOLDS_DAYS["INVOICE"]
    if "QBR" in src:
        return FRESHNESS_THRESHOLDS_DAYS["QBR"]
    if "SLA" in src:
        return FRESHNESS_THRESHOLDS_DAYS["SLA_EXHIBIT"]
    if "COMPLIANCE" in src or "CERT" in src:
        return FRESHNESS_THRESHOLDS_DAYS["COMPLIANCE_CERT"]
    if "MSA" in src:
        return FRESHNESS_THRESHOLDS_DAYS["MSA"]
    if "SOW" in src:
        return FRESHNESS_THRESHOLDS_DAYS["SOW"]
    if "CONTRACT" in src or ext == "AUTO_EXTRACTED":
        return FRESHNESS_THRESHOLDS_DAYS["CONTRACT"]
    if "SPEND" in src or ext == "COMPUTED":
        return FRESHNESS_THRESHOLDS_DAYS["SPEND"]
    return FRESHNESS_THRESHOLDS_DAYS["DEFAULT"]


def _check_freshness(fact: ValidatedEvidenceFact, historical_state: HistoricalEvidenceState | None) -> str:
    """Return FreshnessStatus value for a fact.

    When historical_state is None: all facts are CURRENT (no prior to compare against).
    When historical_state provided: check if the prior validated_at for this field is stale.
    """
    if historical_state is None:
        return "CURRENT"

    snapshot = historical_state.fact_snapshot
    if fact.field_name not in snapshot:
        return "CURRENT"

    prior = snapshot[fact.field_name]
    prior_at = prior.get("validated_at")

    threshold = _freshness_threshold_for(fact.extraction_type, fact.source_file)

    if is_stale(prior_at, threshold):
        return "STALE"
    return "CURRENT"


def _derive_confidence(trust_level: str, freshness_status: str) -> str:
    """Derive confidence label from trust_level and freshness."""
    if freshness_status in ("STALE", "MISSING"):
        return "LOW"
    if trust_level == "OFFICIAL":
        return "HIGH"
    if trust_level == "SYSTEM_EXPORT":
        return "MEDIUM"
    return "LOW"


def _facts_from_doc_intelligence(
    doc_intelligence: DocumentIntelligenceResult,
) -> list[ValidatedEvidenceFact]:
    """Extract ValidatedEvidenceFacts from RS-02 DocumentIntelligenceResult."""
    now = _now_iso()
    facts: list[ValidatedEvidenceFact] = []

    for contract in doc_intelligence.extracted_contracts:
        # Map ContractTerms fields → EXPECTED_FIELDS
        field_map: dict[str, object] = {
            "contract_term_start":  contract.effective_date,
            "contract_term_end":    contract.expiry_date,
            "auto_renew":           contract.auto_renews,
            "notice_period_days":   contract.notice_period_days,
            "total_contract_value": contract.total_value,
            "sla_terms":            contract.sla_summary,
        }

        for field_name, value in field_map.items():
            if value is None:
                continue
            facts.append(ValidatedEvidenceFact(
                field_name=field_name,
                value=value,
                display_value=str(value),
                extraction_type="AUTO_EXTRACTED",
                source_file=contract.document_id,
                source_section=None,
                confidence="HIGH",
                trust_level="OFFICIAL",
                freshness_status="CURRENT",   # updated after freshness check
                conflict_flag=False,
                conflict_values=[],
                quality_score=TRUST_WEIGHTS["OFFICIAL"],  # 1.0; updated after freshness check
                validated_at=now,
            ))

    return facts


def _facts_from_structured_bundle(
    structured_bundle: StructuredDataBundle,
) -> list[ValidatedEvidenceFact]:
    """Compute spend facts from RS-01 StructuredDataBundle."""
    now = _now_iso()
    records = structured_bundle.raw_spend_records

    if not records:
        return []

    # Trust level: first OFFICIAL or SYSTEM_EXPORT record wins, else USER_SUBMITTED
    trust_level = "USER_SUBMITTED"
    for r in records:
        if r.trust_level in ("OFFICIAL", "SYSTEM_EXPORT"):
            trust_level = r.trust_level
            break

    # Aggregate spend
    amounts = [r.amount_usd for r in records if r.amount_usd is not None]
    spend_ttm = sum(amounts) if amounts else 0.0
    invoice_count = len(records)
    po_count = sum(1 for r in records if r.po_number is not None)

    source_id = structured_bundle.vendor_id or "structured_bundle"
    facts: list[ValidatedEvidenceFact] = []

    facts.append(ValidatedEvidenceFact(
        field_name="spend_total_ttm_usd",
        value=spend_ttm,
        display_value=f"USD {spend_ttm:,.2f}",
        extraction_type="COMPUTED",
        source_file=source_id,
        source_section=None,
        confidence=_derive_confidence(trust_level, "CURRENT"),
        trust_level=trust_level,
        freshness_status="CURRENT",
        conflict_flag=False,
        conflict_values=[],
        quality_score=_compute_quality_score(trust_level, "CURRENT", False),
        validated_at=now,
    ))

    # invoice_count and po_count — not in EXPECTED_FIELDS, included as context facts
    facts.append(ValidatedEvidenceFact(
        field_name="invoice_count",
        value=invoice_count,
        display_value=str(invoice_count),
        extraction_type="COMPUTED",
        source_file=source_id,
        source_section=None,
        confidence=_derive_confidence(trust_level, "CURRENT"),
        trust_level=trust_level,
        freshness_status="CURRENT",
        conflict_flag=False,
        conflict_values=[],
        quality_score=_compute_quality_score(trust_level, "CURRENT", False),
        validated_at=now,
    ))

    facts.append(ValidatedEvidenceFact(
        field_name="po_count",
        value=po_count,
        display_value=str(po_count),
        extraction_type="COMPUTED",
        source_file=source_id,
        source_section=None,
        confidence=_derive_confidence(trust_level, "CURRENT"),
        trust_level=trust_level,
        freshness_status="CURRENT",
        conflict_flag=False,
        conflict_values=[],
        quality_score=_compute_quality_score(trust_level, "CURRENT", False),
        validated_at=now,
    ))

    return facts


def _facts_from_signals(signal_bundle: dict) -> list[ValidatedEvidenceFact]:
    """Extract ValidatedEvidenceFacts from check-in / email signals."""
    now = _now_iso()
    facts: list[ValidatedEvidenceFact] = []

    for signal in signal_bundle.get("signals", []):
        field_name = signal.get("field") or signal.get("field_name") or "signal_data"
        value = signal.get("value")
        if value is None:
            continue
        source = signal.get("source", "signal_bundle")
        facts.append(ValidatedEvidenceFact(
            field_name=field_name,
            value=value,
            display_value=str(value),
            extraction_type="SIGNAL",
            source_file=source,
            source_section=None,
            confidence="LOW",
            trust_level="USER_SUBMITTED",
            freshness_status="CURRENT",
            conflict_flag=False,
            conflict_values=[],
            quality_score=_compute_quality_score("USER_SUBMITTED", "CURRENT", False),
            validated_at=now,
        ))

    return facts


def _facts_from_vendor_file(vendor_file: dict) -> list[ValidatedEvidenceFact]:
    """Read key identity and relationship fields from the parsed workspace vendor_file."""
    now = _now_iso()
    field_map: dict[str, object] = {
        "relationship_type": vendor_file.get("relationship_type"),
        "dependency_tier":   vendor_file.get("dependency_tier"),
        "primary_owner":     vendor_file.get("primary_owner"),
        "renewal_date":      vendor_file.get("renewal_date"),
        "auto_renew":        vendor_file.get("auto_renew"),
    }

    facts: list[ValidatedEvidenceFact] = []
    for field_name, value in field_map.items():
        if value is None:
            continue
        facts.append(ValidatedEvidenceFact(
            field_name=field_name,
            value=value,
            display_value=str(value),
            extraction_type="COMPUTED",
            source_file="vendor_file",
            source_section=None,
            confidence="MEDIUM",
            trust_level="SYSTEM_EXPORT",
            freshness_status="CURRENT",
            conflict_flag=False,
            conflict_values=[],
            quality_score=_compute_quality_score("SYSTEM_EXPORT", "CURRENT", False),
            validated_at=now,
        ))

    return facts


def _detect_conflicts(facts: list[ValidatedEvidenceFact]) -> list[ValidatedEvidenceFact]:
    """Detect field-level conflicts and update affected facts in-place.

    Conflict: same field_name, 2+ different source_files, different values.
    Updates conflict_flag, conflict_values, and quality_score on all affected facts.
    """
    from collections import defaultdict

    # Group non-MISSING fact indices by field_name
    by_field: defaultdict[str, list[int]] = defaultdict(list)
    for i, fact in enumerate(facts):
        if fact.freshness_status != "MISSING":
            by_field[fact.field_name].append(i)

    for field_name, indices in by_field.items():
        if len(indices) < 2:
            continue

        field_facts = [facts[i] for i in indices]
        source_files = {f.source_file for f in field_facts}

        # Need facts from different source files
        if len(source_files) < 2:
            continue

        # Need different values
        str_values = [str(f.value) for f in field_facts]
        if len(set(str_values)) < 2:
            continue

        # Conflict detected — update all facts for this field
        all_values = [f.value for f in field_facts]
        for i in indices:
            fact = facts[i]
            fact.conflict_flag = True
            fact.conflict_values = all_values
            fact.quality_score = _compute_quality_score(fact.trust_level, fact.freshness_status, True)

    return facts


def _add_missing_fields(facts: list[ValidatedEvidenceFact]) -> list[ValidatedEvidenceFact]:
    """Add MISSING placeholder facts for any EXPECTED_FIELDS not yet covered."""
    now = _now_iso()
    covered = {f.field_name for f in facts if f.freshness_status != "MISSING"}

    for field_name in sorted(EXPECTED_FIELDS):   # sorted for deterministic ordering
        if field_name not in covered:
            facts.append(ValidatedEvidenceFact(
                field_name=field_name,
                value=None,
                display_value="",
                extraction_type="COMPUTED",
                source_file="",
                source_section=None,
                confidence="LOW",
                trust_level="USER_SUBMITTED",
                freshness_status="MISSING",
                conflict_flag=False,
                conflict_values=[],
                quality_score=0.0,
                validated_at=now,
            ))

    return facts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_evidence(
    vendor_id: str,
    programme_id: str,
    doc_intelligence: DocumentIntelligenceResult | None,
    structured_bundle: StructuredDataBundle | None,
    signal_bundle: dict | None,
    vendor_file: dict,
    historical_state: HistoricalEvidenceState | None,
) -> ValidatedEvidenceAssembly:
    """Validate all incoming evidence and produce a quality-scored assembly.

    All four optional inputs may be None — returns assembly with MISSING facts
    for absent sources. No LLM calls. No file writes.
    """
    now = _now_iso()

    # --- Collect raw facts from each source ---
    facts: list[ValidatedEvidenceFact] = []

    if doc_intelligence is not None:
        facts.extend(_facts_from_doc_intelligence(doc_intelligence))

    if structured_bundle is not None:
        facts.extend(_facts_from_structured_bundle(structured_bundle))

    if signal_bundle is not None:
        facts.extend(_facts_from_signals(signal_bundle))

    facts.extend(_facts_from_vendor_file(vendor_file))

    # --- Apply freshness check and recompute quality scores ---
    for fact in facts:
        freshness = _check_freshness(fact, historical_state)
        fact.freshness_status = freshness
        if freshness == "STALE":
            fact.confidence = "LOW"
        fact.quality_score = _compute_quality_score(fact.trust_level, freshness, fact.conflict_flag)

    # --- Detect conflicts (updates conflict_flag and quality_score in-place) ---
    facts = _detect_conflicts(facts)

    # --- Add MISSING placeholders for uncovered EXPECTED_FIELDS ---
    facts = _add_missing_fields(facts)

    # --- Compute summary metrics ---
    conflict_count = sum(1 for f in facts if f.conflict_flag)
    stale_count = sum(1 for f in facts if f.freshness_status == "STALE")
    missing_count = sum(1 for f in facts if f.freshness_status == "MISSING")

    # Completeness: unique EXPECTED_FIELDS that have at least one CURRENT fact
    present_and_current: set[str] = {
        f.field_name
        for f in facts
        if f.field_name in EXPECTED_FIELDS and f.freshness_status == "CURRENT"
    }
    completeness_pct = len(present_and_current) / len(EXPECTED_FIELDS)

    logger.debug(
        "[evidence_validator] vendor=%r facts=%d conflicts=%d stale=%d missing=%d pct=%.2f",
        vendor_id, len(facts), conflict_count, stale_count, missing_count, completeness_pct,
    )

    return ValidatedEvidenceAssembly(
        vendor_id=vendor_id,
        programme_id=programme_id,
        facts=facts,
        completeness_pct=completeness_pct,
        conflict_count=conflict_count,
        stale_count=stale_count,
        missing_count=missing_count,
        validated_at=now,
    )
