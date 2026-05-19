"""Tool 4 (Process 3) — relationship_classifier.

Classify the nature and strategic importance of the vendor relationship.
Score dependency level. Uses LLM only when signals are ambiguous (0.35–0.65).

Returns RelationshipClassification in memory. Never raises.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

from cobalt.core.llm_call import llm_call
from cobalt.models.schemas.rs_schema import (
    ContractCoverage,
    ContractTerms,
    DependencyTier,
    RelationshipClassification,
    RelationshipType,
    RenewalUrgency,
    SpendSummary,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
SPEND_NORMALISATION_CEILING_USD = 500_000
AMBIGUOUS_SCORE_LOW  = 0.35
AMBIGUOUS_SCORE_HIGH = 0.65

_STRATEGIC_CATEGORIES = {"IT_INFRASTRUCTURE", "CORE_OPERATIONS"}

_LLM_PROMPT_TEMPLATE = """\
You are classifying a vendor relationship for procurement risk management.
Based on the following signals, classify the relationship as exactly one of:
STRATEGIC, PREFERRED, TRANSACTIONAL, INCIDENTAL.

Signals:
- Spend TTM (USD): {spend_ttm}
- Data completeness: {data_completeness}
- Contract coverage: {contract_coverage}
- Contract duration (months): {duration_months}
- Auto-renews: {auto_renews}
- Single source risk: {single_source_risk}
- Vendor category: {category}
- Rule-based dependency score: {score:.2f}

Definitions:
- STRATEGIC: Core to operations; hard to replace; significant spend or critical function
- PREFERRED: Important; regularly used; some alternatives exist
- TRANSACTIONAL: Routine; easily replaceable; low criticality
- INCIDENTAL: Occasional; marginal spend; no dependency

Respond as JSON only: {{"relationship_type": "...", "reasoning": "one sentence explanation"}}\
"""


def _today() -> date:
    return date.today()


def _parse_date(iso_str: str | None) -> date | None:
    if not iso_str:
        return None
    try:
        return date.fromisoformat(iso_str[:10])
    except (ValueError, TypeError):
        return None


def _contract_duration_months(contract_terms: list[ContractTerms]) -> float:
    """Return the duration in months of the longest active contract."""
    max_months = 0.0
    today = _today()
    for ct in contract_terms:
        eff = _parse_date(ct.effective_date)
        exp = _parse_date(ct.expiry_date)
        if eff and exp and exp >= today:
            months = (exp.year - eff.year) * 12 + (exp.month - eff.month)
            if months > max_months:
                max_months = months
    return max_months


def _detect_contract_coverage(contract_terms: list[ContractTerms]) -> str:
    if not contract_terms:
        return ContractCoverage.UNCOVERED.value

    today = _today()
    for ct in contract_terms:
        if ct.extraction_confidence == "LOW":
            continue
        exp = _parse_date(ct.expiry_date)
        if exp and exp > today:
            return ContractCoverage.FULLY_COVERED.value

    # Has contracts but none fully active/future with good confidence
    return ContractCoverage.PARTIALLY_COVERED.value


def _detect_renewal_urgency(contract_terms: list[ContractTerms]) -> str:
    today = _today()
    has_expiry = False
    urgent = False
    watch = False

    for ct in contract_terms:
        exp = _parse_date(ct.expiry_date)
        if exp is None:
            continue
        if exp <= today:
            # Expired — ignore for urgency
            continue
        has_expiry = True
        days_until = (exp - today).days
        if days_until <= 90:
            urgent = True
        elif days_until <= 180:
            watch = True

    if not has_expiry:
        return RenewalUrgency.UNKNOWN.value
    if urgent:
        return RenewalUrgency.URGENT.value
    if watch:
        return RenewalUrgency.WATCH.value
    return RenewalUrgency.OK.value


def _check_single_source(
    entity_profile: dict,
    known_facts: dict,
    spend_summary: SpendSummary,
) -> bool:
    """Return True if vendor appears to be single-source (no alternatives found + real spend)."""
    if spend_summary.total_usd_all_time is None or spend_summary.total_usd_all_time == 0:
        return False

    for key in ("alternatives", "competitors", "alternative_vendors"):
        if key in known_facts or key in entity_profile:
            return False

    return True


def _score_dependency(
    spend_summary: SpendSummary,
    contract_terms: list[ContractTerms],
    entity_profile: dict,
    known_facts: dict,
) -> float:
    """Compute weighted dependency score 0.0–1.0."""
    # Spend concentration
    ttm = spend_summary.total_usd_ttm
    if ttm is None or ttm == 0:
        spend_signal = 0.0
    else:
        spend_signal = min(ttm / SPEND_NORMALISATION_CEILING_USD, 1.0)

    # Contract coverage
    coverage = _detect_contract_coverage(contract_terms)
    coverage_map = {
        ContractCoverage.FULLY_COVERED.value:     1.0,
        ContractCoverage.PARTIALLY_COVERED.value: 0.5,
        ContractCoverage.UNCOVERED.value:          0.0,
    }
    coverage_signal = coverage_map.get(coverage, 0.0)

    # Single source risk
    single_source = _check_single_source(entity_profile, known_facts, spend_summary)
    single_signal = 1.0 if single_source else 0.0

    # Contract duration
    duration_months = _contract_duration_months(contract_terms)
    if duration_months > 36:
        duration_signal = 1.0
    elif duration_months >= 12:
        duration_signal = 0.5
    else:
        duration_signal = 0.0

    # Auto-renewal
    auto_renew_signal = 0.0
    for ct in contract_terms:
        if ct.auto_renews:
            auto_renew_signal = 1.0
            break

    # Strategic category
    category = known_facts.get("category") or entity_profile.get("category_hint") or ""
    cat_upper = category.upper() if category else ""
    if cat_upper in _STRATEGIC_CATEGORIES:
        category_signal = 1.0
    elif cat_upper:
        category_signal = 0.5
    else:
        category_signal = 0.3

    score = (
        spend_signal    * 0.25 +
        coverage_signal * 0.20 +
        single_signal   * 0.15 +
        duration_signal * 0.15 +
        auto_renew_signal * 0.10 +
        category_signal * 0.15
    )
    return min(max(score, 0.0), 1.0)


def _classify_type_from_score(score: float, spend_ttm: float | None) -> str:
    if score >= 0.70:
        return RelationshipType.STRATEGIC.value
    if score >= 0.50:
        return RelationshipType.PREFERRED.value
    if score >= 0.30:
        return RelationshipType.TRANSACTIONAL.value
    return RelationshipType.INCIDENTAL.value


def _classify_tier_from_score(score: float, single_source: bool) -> str | None:
    # CRITICAL: ≥ 0.85 AND single_source_risk
    if score >= 0.85 and single_source:
        return DependencyTier.CRITICAL.value
    # HIGH: ≥ 0.70 OR ≥ 0.60 with single_source
    if score >= 0.70 or (score >= 0.60 and single_source):
        return DependencyTier.HIGH.value
    # MEDIUM
    if score >= 0.40:
        return DependencyTier.MEDIUM.value
    return DependencyTier.LOW.value


def _llm_classify(vendor_id: str, signals: dict) -> tuple[str, str] | None:
    """Call LLM for ambiguous band classification. Returns (relationship_type, reasoning) or None."""
    prompt = _LLM_PROMPT_TEMPLATE.format(**signals)
    try:
        response = llm_call(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o",
            temperature=0,
            max_tokens=200,
        )
        raw = response.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[^\n]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())
        parsed = json.loads(raw)
        rel_type = parsed.get("relationship_type", "").upper()
        valid_types = {t.value for t in RelationshipType}
        if rel_type not in valid_types:
            return None
        reasoning = parsed.get("reasoning", "")
        return rel_type, reasoning
    except Exception as exc:
        logger.debug("LLM classification failed for %s: %s", vendor_id, exc)
        return None


def _compute_classification_confidence(
    score: float,
    spend_summary: SpendSummary,
    contract_terms: list[ContractTerms],
    llm_used: bool,
) -> str:
    completeness = spend_summary.data_completeness
    has_contracts = bool(contract_terms)

    if not spend_summary.total_usd_all_time and not has_contracts:
        return "LOW"

    if llm_used:
        return "MEDIUM"

    if completeness in ("FULL", "PARTIAL") and has_contracts:
        return "HIGH"

    if completeness == "SPARSE":
        return "MEDIUM"

    return "MEDIUM"


def classify_relationship(
    vendor_id: str,
    spend_summary: SpendSummary,
    contract_terms: list[ContractTerms],
    entity_profile: dict,
    known_facts: dict,
) -> RelationshipClassification:
    """Classify the vendor relationship. Never raises."""
    today = _today()

    # Check for no-data case
    has_spend = spend_summary.total_usd_all_time is not None and spend_summary.total_usd_all_time > 0
    has_contracts = bool(contract_terms)

    if not has_spend and not has_contracts:
        return RelationshipClassification(
            vendor_id=vendor_id,
            relationship_type=RelationshipType.UNKNOWN.value,
            dependency_score=0.0,
            dependency_tier=None,
            single_source_risk=False,
            contract_coverage=ContractCoverage.UNCOVERED.value,
            relationship_age_days=None,
            renewal_urgency=RenewalUrgency.UNKNOWN.value,
            classification_confidence="LOW",
            llm_used=False,
            reasoning=None,
        )

    score = _score_dependency(spend_summary, contract_terms, entity_profile, known_facts)
    single_source = _check_single_source(entity_profile, known_facts, spend_summary)
    contract_coverage = _detect_contract_coverage(contract_terms)
    renewal_urgency = _detect_renewal_urgency(contract_terms)

    # Relationship age
    age_days: int | None = None
    for key in ("first_transacted_date", "created_at"):
        val = entity_profile.get(key) or known_facts.get(key)
        if val:
            d = _parse_date(str(val))
            if d:
                age_days = (today - d).days
                break

    # LLM for ambiguous band
    llm_used = False
    reasoning: str | None = None
    relationship_type = _classify_type_from_score(score, spend_summary.total_usd_ttm)

    if (
        AMBIGUOUS_SCORE_LOW <= score <= AMBIGUOUS_SCORE_HIGH
        and spend_summary.data_completeness != "NONE"
    ):
        duration_months = _contract_duration_months(contract_terms)
        auto_renews = any(ct.auto_renews for ct in contract_terms if ct.auto_renews is not None)
        category = known_facts.get("category") or entity_profile.get("category_hint") or "unknown"

        signals = {
            "spend_ttm": spend_summary.total_usd_ttm,
            "data_completeness": spend_summary.data_completeness,
            "contract_coverage": contract_coverage,
            "duration_months": round(duration_months),
            "auto_renews": auto_renews,
            "single_source_risk": single_source,
            "category": category,
            "score": score,
        }

        llm_result = _llm_classify(vendor_id, signals)
        if llm_result:
            relationship_type, reasoning = llm_result
            llm_used = True
        else:
            # Fallback: treat as score 0.50 → PREFERRED
            relationship_type = RelationshipType.PREFERRED.value

    tier: str | None = None
    if relationship_type != RelationshipType.UNKNOWN.value:
        tier = _classify_tier_from_score(score, single_source)

    confidence = _compute_classification_confidence(score, spend_summary, contract_terms, llm_used)

    return RelationshipClassification(
        vendor_id=vendor_id,
        relationship_type=relationship_type,
        dependency_score=round(score, 4),
        dependency_tier=tier,
        single_source_risk=single_source,
        contract_coverage=contract_coverage,
        relationship_age_days=age_days,
        renewal_urgency=renewal_urgency,
        classification_confidence=confidence,
        llm_used=llm_used,
        reasoning=reasoning,
    )
