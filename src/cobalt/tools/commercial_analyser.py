"""Tool 3 (Process 4) — commercial_analyser.

Contract-type-aware commercial intelligence. Detects contract type and routes
to the correct analysis path. Computes utilisation, SLA adherence, spend
efficiency, and renewal risk scenarios.

Writes to workspace: No — returns CommercialAnalysisResult in memory.
LLM: Conditional — at most 3 calls per invocation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from cobalt.core.llm_call import llm_call
from cobalt.models.schemas.an_schema import (
    CommercialAnalysisResult,
    HistoricalCommercialState,
    ScoringConfig,
    ValidatedEvidenceAssembly,
)
from cobalt.models.schemas.rs_schema import StructuredDataBundle

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SAAS_SIGNALS: list[str] = [
    "per seat", "per user", "licence", "subscription", "saas",
    "software as a service", "named user",
]

SERVICES_SIGNALS: list[str] = [
    "statement of work", "sow", "milestone", "deliverable",
    "professional services", "time and materials",
]

MANAGED_SIGNALS: list[str] = [
    "uptime", "incident response", "managed service", "sla response",
    "service desk", "24x7",
]

_KEYWORD_CONFIDENCE_THRESHOLD = 0.70


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detect_contract_type_keywords(rs_profile) -> tuple[str, float]:
    """Scan contract_terms for keyword signals and return (contract_type, keyword_confidence).

    keyword_confidence = max_category_count / total_signals_found  (0.0 if no signals).
    """
    saas_count = 0
    services_count = 0
    managed_count = 0

    for ct in rs_profile.contract_terms:
        # Build one text blob per contract
        parts = [ct.document_type or ""]
        parts.extend(ct.key_obligations or [])
        if ct.sla_summary:
            parts.append(ct.sla_summary)
        blob = " ".join(parts).lower()

        for sig in SAAS_SIGNALS:
            if sig in blob:
                saas_count += 1
        for sig in SERVICES_SIGNALS:
            if sig in blob:
                services_count += 1
        for sig in MANAGED_SIGNALS:
            if sig in blob:
                managed_count += 1

    total = saas_count + services_count + managed_count
    if total == 0:
        return "UNKNOWN", 0.0

    max_count = max(saas_count, services_count, managed_count)
    confidence = max_count / total

    categories_present = sum([saas_count > 0, services_count > 0, managed_count > 0])
    if categories_present > 1:
        contract_type = "MIXED"
    elif saas_count > 0:
        contract_type = "SAAS"
    elif services_count > 0:
        contract_type = "SERVICES"
    else:
        contract_type = "MANAGED_SERVICES"

    return contract_type, confidence


def _llm_classify_contract(rs_profile, vendor_id: str) -> tuple[str, str]:
    """Call LLM to classify ambiguous contract. Returns (contract_type, confidence)."""
    system = (
        "Classify vendor contract type as SAAS, SERVICES, MANAGED_SERVICES, MIXED, or UNKNOWN. "
        "Return JSON only."
    )
    user = (
        f"Contract description for {vendor_id}:\n"
        f"Document types: {[ct.document_type for ct in rs_profile.contract_terms]}\n"
        f"Key obligations: {[ct.key_obligations for ct in rs_profile.contract_terms]}\n"
        f"SLA terms: {[ct.sla_summary for ct in rs_profile.contract_terms]}\n\n"
        'Return: {"contract_type": "...", "confidence": "HIGH|MEDIUM|LOW", "reasoning": "..."}'
    )
    try:
        result = llm_call(prompt=user, system=system, expect_json=True)
        if isinstance(result, dict):
            ctype = result.get("contract_type", "UNKNOWN")
            conf = result.get("confidence", "LOW")
        else:
            ctype = "UNKNOWN"
            conf = "LOW"
        return ctype, conf
    except Exception:
        logger.warning("[commercial_analyser] LLM contract classification failed for %r", vendor_id)
        return "UNKNOWN", "LOW"


def _extract_fact_value(validated_assembly: ValidatedEvidenceAssembly, field_name: str):
    """Return value of highest-quality CURRENT fact for field_name, or None."""
    facts = [
        f for f in validated_assembly.facts
        if f.field_name == field_name and f.freshness_status != "MISSING"
    ]
    if not facts:
        return None
    return max(facts, key=lambda f: f.quality_score).value


def _analyse_saas(structured_bundle: StructuredDataBundle | None, validated_assembly: ValidatedEvidenceAssembly) -> dict:
    """Extract SaaS-specific metrics."""
    active_users = None
    total_licences = None
    annual_contract_value = None

    if structured_bundle is not None:
        meta = {**structured_bundle.connector_metadata, **structured_bundle.checkin_metadata}
        active_users = meta.get("active_users")
        total_licences = meta.get("total_licences") or meta.get("total_licenses")
        annual_contract_value = meta.get("annual_contract_value")

    # Fall back to validated_assembly facts
    if active_users is None:
        active_users = _extract_fact_value(validated_assembly, "active_users")
    if total_licences is None:
        total_licences = _extract_fact_value(validated_assembly, "total_licences")
    if annual_contract_value is None:
        annual_contract_value = (
            _extract_fact_value(validated_assembly, "annual_contract_value")
            or _extract_fact_value(validated_assembly, "total_contract_value")
        )

    findings: list[str] = []
    utilisation_score = None
    licence_waste_pct = None
    cost_per_seat = None
    shelfware_flag = False

    if active_users is None or total_licences is None:
        findings.append("LICENCE_DATA_MISSING")
    else:
        active_users = float(active_users)
        total_licences = float(total_licences)
        if total_licences > 0:
            utilisation_rate = active_users / total_licences
            utilisation_score = utilisation_rate
            licence_waste_pct = (1.0 - utilisation_rate) * 100.0

            if utilisation_rate < 0.70:
                findings.append("LICENCE_WASTE")
            if utilisation_rate < 0.50:
                shelfware_flag = True
                findings.append("SHELFWARE_DETECTED")

        if annual_contract_value is not None and active_users > 0:
            cost_per_seat = float(annual_contract_value) / active_users

    return {
        "utilisation_score": utilisation_score,
        "licence_waste_pct": licence_waste_pct,
        "cost_per_seat": cost_per_seat,
        "shelfware_flag": shelfware_flag,
        "findings": findings,
    }


def _analyse_services(structured_bundle: StructuredDataBundle | None, validated_assembly: ValidatedEvidenceAssembly) -> dict:
    """Extract Services-specific metrics."""
    compliant_tickets = None
    total_priority_tickets = None
    milestones_hit = None
    total_milestones = None
    sla_credit_caps: list[float] = []

    if structured_bundle is not None:
        meta = {**structured_bundle.connector_metadata, **structured_bundle.checkin_metadata}
        compliant_tickets = meta.get("compliant_tickets")
        total_priority_tickets = meta.get("total_priority_tickets")
        milestones_hit = meta.get("milestones_hit")
        total_milestones = meta.get("total_milestones")
        caps = meta.get("sla_credit_caps", [])
        if isinstance(caps, list):
            sla_credit_caps = [float(c) for c in caps if c is not None]

    # Fall back to validated_assembly facts
    if compliant_tickets is None:
        compliant_tickets = _extract_fact_value(validated_assembly, "compliant_tickets")
    if total_priority_tickets is None:
        total_priority_tickets = _extract_fact_value(validated_assembly, "total_priority_tickets")
    if milestones_hit is None:
        milestones_hit = _extract_fact_value(validated_assembly, "milestones_hit")
    if total_milestones is None:
        total_milestones = _extract_fact_value(validated_assembly, "total_milestones")

    findings: list[str] = []
    sla_adherence_pct = None
    delivery_score = None
    penalty_exposure = None
    milestone_status = None

    if compliant_tickets is None or total_priority_tickets is None:
        findings.append("TICKET_DATA_MISSING")
    else:
        total_priority_tickets = float(total_priority_tickets)
        if total_priority_tickets > 0:
            sla_adherence_pct = float(compliant_tickets) / total_priority_tickets * 100.0
            if sla_adherence_pct < 90:
                findings.append("SLA_BREACH_PATTERN")

        penalty_exposure = sum(sla_credit_caps) if sla_credit_caps else 0.0

    if milestones_hit is not None and total_milestones is not None:
        total_milestones_f = float(total_milestones)
        if total_milestones_f > 0:
            delivery_score = float(milestones_hit) / total_milestones_f * 100.0
            if delivery_score < 80:
                findings.append("MILESTONE_RISK")
            milestone_status = "ON_TRACK" if delivery_score >= 80 else "AT_RISK"

    return {
        "sla_adherence_pct": sla_adherence_pct,
        "delivery_score": delivery_score,
        "milestone_status": milestone_status,
        "penalty_exposure": penalty_exposure,
        "findings": findings,
    }


def _analyse_managed(structured_bundle: StructuredDataBundle | None, validated_assembly: ValidatedEvidenceAssembly) -> dict:
    """Extract Managed Services-specific metrics."""
    uptime_pct = None
    incident_trend = None
    findings: list[str] = []

    meta: dict = {}
    if structured_bundle is not None:
        meta = {**structured_bundle.connector_metadata, **structured_bundle.checkin_metadata}

    uptime_pct = meta.get("uptime_pct")
    if uptime_pct is None:
        uptime_pct = _extract_fact_value(validated_assembly, "uptime_pct")

    # Incident trend from monthly counts (list ordered oldest→newest)
    monthly_counts = meta.get("monthly_incident_counts", [])
    if monthly_counts and len(monthly_counts) >= 2:
        recent = monthly_counts[-1]
        prior = monthly_counts[-2]
        if recent > prior:
            incident_trend = "RISING"
            findings.append("INCIDENT_FREQUENCY_RISING")
        elif recent < prior:
            incident_trend = "FALLING"
        else:
            incident_trend = "STABLE"
    elif monthly_counts and len(monthly_counts) == 1:
        incident_trend = "STABLE"

    return {
        "uptime_pct": uptime_pct,
        "incident_trend": incident_trend,
        "findings": findings,
    }


def _compute_risk_level(commercial_findings: list[str], metrics: dict) -> str:
    """Determine commercial_risk_level deterministically from findings and metrics."""
    licence_waste_pct = metrics.get("licence_waste_pct")
    penalty_exposure = metrics.get("penalty_exposure")
    utilisation_score = metrics.get("utilisation_score")
    delivery_score = metrics.get("delivery_score")

    # CRITICAL
    if "LICENCE_WASTE" in commercial_findings and licence_waste_pct is not None and licence_waste_pct > 30:
        return "CRITICAL"
    if (
        "SLA_BREACH_PATTERN" in commercial_findings
        and penalty_exposure is not None
        and penalty_exposure > 0
    ):
        return "CRITICAL"

    # HIGH
    if any(f in commercial_findings for f in ("LICENCE_WASTE", "SLA_BREACH_PATTERN", "INCIDENT_FREQUENCY_RISING")):
        return "HIGH"

    # MEDIUM
    if any(f in commercial_findings for f in ("TICKET_DATA_MISSING", "LICENCE_DATA_MISSING")):
        return "MEDIUM"
    if utilisation_score is not None and utilisation_score < 0.85:
        return "MEDIUM"
    if delivery_score is not None and delivery_score < 90:
        return "MEDIUM"

    return "LOW"


def _compute_spend_efficiency(rs_profile) -> tuple[float | None, float | None, float | None]:
    """Return (contract_total, actual_spend, variance_pct).

    variance_pct = (actual_spend - contract_total) / contract_total * 100
    Returns (None, None, None) if contract_total is 0 or missing.
    """
    contract_total = sum(
        ct.total_value for ct in rs_profile.contract_terms if ct.total_value is not None
    )
    actual_spend = rs_profile.spend_summary.total_usd_all_time

    if not contract_total or actual_spend is None:
        return contract_total or None, actual_spend, None

    variance_pct = (actual_spend - contract_total) / contract_total * 100.0
    return contract_total, actual_spend, variance_pct


def _llm_renewal_scenarios(
    rs_profile,
    vendor_id: str,
    commercial_risk_level: str,
    commercial_findings: list[str],
    historical_state: HistoricalCommercialState | None,
) -> list[dict]:
    """Call LLM for renewal risk scenarios. Returns [] on failure."""
    # Find first contract with expiry_date
    contracts_with_expiry = [ct for ct in rs_profile.contract_terms if ct.expiry_date is not None]
    if not contracts_with_expiry:
        return []

    ct = contracts_with_expiry[0]
    prior_trend = historical_state.prior_risk_level if historical_state else None

    system = (
        "You are a procurement analyst. Generate renewal risk scenarios. "
        "Return JSON only. No preamble."
    )
    user = (
        f"Vendor: {vendor_id}\n"
        f"Contract value: {ct.total_value}\n"
        f"Expiry: {ct.expiry_date}\n"
        f"Auto-renews: {ct.auto_renews}\n"
        f"Notice period: {ct.notice_period_days} days\n"
        f"Commercial risk: {commercial_risk_level}\n"
        f"Active flags: {commercial_findings}\n"
        f"Prior trend: {prior_trend}\n\n"
        'Return a JSON array of exactly 3 scenarios:\n'
        '[{"scenario": "best_case", "description": "...", "probability": 0.X},'
        ' {"scenario": "expected_case", "description": "...", "probability": 0.X},'
        ' {"scenario": "worst_case", "description": "...", "probability": 0.X}]'
    )
    try:
        result = llm_call(prompt=user, system=system, expect_json=True)
        if isinstance(result, list):
            return result
        # Some LLMs wrap in a dict key
        if isinstance(result, dict):
            for key in ("scenarios", "renewal_scenarios"):
                if key in result and isinstance(result[key], list):
                    return result[key]
        return []
    except Exception:
        logger.warning("[commercial_analyser] LLM renewal scenarios failed for %r", vendor_id)
        return []


def _llm_spend_narrative(
    vendor_id: str,
    contract_total: float,
    actual_spend: float,
    variance_pct: float,
    risk_level: str,
) -> str | None:
    """Call LLM for spend efficiency narrative. Returns None on failure."""
    system = "Write one sentence explaining what this spend variance means for procurement."
    user = (
        f"Vendor {vendor_id}: contract value {contract_total:.2f}, "
        f"actual spend {actual_spend:.2f}, "
        f"variance {variance_pct:.1f}%, commercial risk {risk_level}."
        '\nReturn JSON: {"narrative": "..."}'
    )
    try:
        result = llm_call(prompt=user, system=system, expect_json=True)
        if isinstance(result, dict):
            return result.get("narrative")
        return None
    except Exception:
        logger.warning("[commercial_analyser] LLM spend narrative failed for %r", vendor_id)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_commercial(
    vendor_id: str,
    validated_assembly: ValidatedEvidenceAssembly,
    rs_profile,
    structured_bundle: StructuredDataBundle | None,
    historical_state: HistoricalCommercialState | None,
    scoring_config: ScoringConfig,
) -> CommercialAnalysisResult:
    """Analyse commercial position for a vendor.

    At most 3 LLM calls per invocation (contract classification, renewal scenarios,
    spend narrative). All LLM calls degrade gracefully on failure.
    """
    now = _now_iso()

    # ------------------------------------------------------------------
    # Step 1: Detect contract type
    # ------------------------------------------------------------------
    contract_type, keyword_confidence = _detect_contract_type_keywords(rs_profile)

    if keyword_confidence < _KEYWORD_CONFIDENCE_THRESHOLD and contract_type != "UNKNOWN":
        # Signals found but confidence low — call LLM to disambiguate.
        # (UNKNOWN means no signals at all; no LLM call for that case.)
        llm_type, llm_conf_str = _llm_classify_contract(rs_profile, vendor_id)
        contract_type = llm_type
        if llm_conf_str == "HIGH":
            contract_type_confidence = "HIGH"
        elif llm_conf_str == "MEDIUM":
            contract_type_confidence = "MEDIUM"
        else:
            contract_type_confidence = "LOW"
    else:
        # Either no signals (UNKNOWN, confidence=0) or confidence >= threshold
        contract_type_confidence = "HIGH" if keyword_confidence >= _KEYWORD_CONFIDENCE_THRESHOLD else "LOW"

    # ------------------------------------------------------------------
    # Step 2: Run contract-type-specific analysis paths
    # ------------------------------------------------------------------
    commercial_findings: list[str] = []

    # SaaS metrics
    utilisation_score = None
    licence_waste_pct = None
    cost_per_seat = None
    shelfware_flag = False

    # Services metrics
    sla_adherence_pct = None
    delivery_score = None
    milestone_status = None
    penalty_exposure = None

    # Managed services metrics
    uptime_pct = None
    incident_trend = None
    mttr_days = None

    if contract_type in ("SAAS", "MIXED"):
        saas = _analyse_saas(structured_bundle, validated_assembly)
        utilisation_score = saas["utilisation_score"]
        licence_waste_pct = saas["licence_waste_pct"]
        cost_per_seat = saas["cost_per_seat"]
        shelfware_flag = saas["shelfware_flag"]
        commercial_findings.extend(saas["findings"])

    if contract_type in ("SERVICES", "MIXED"):
        svc = _analyse_services(structured_bundle, validated_assembly)
        sla_adherence_pct = svc["sla_adherence_pct"]
        delivery_score = svc["delivery_score"]
        milestone_status = svc["milestone_status"]
        penalty_exposure = svc["penalty_exposure"]
        commercial_findings.extend(svc["findings"])

    if contract_type in ("MANAGED_SERVICES", "MIXED"):
        mgd = _analyse_managed(structured_bundle, validated_assembly)
        uptime_pct = mgd["uptime_pct"]
        incident_trend = mgd["incident_trend"]
        mttr_days = mgd.get("mttr_days")
        commercial_findings.extend(mgd["findings"])

    # Forward CONTRACT_DEVIATION from rs_profile flags
    if "CONTRACT_DEVIATION" in (rs_profile.flags or []):
        commercial_findings.append("CONTRACT_DEVIATION")

    # ------------------------------------------------------------------
    # Step 3: Commercial risk level
    # ------------------------------------------------------------------
    metrics = {
        "licence_waste_pct": licence_waste_pct,
        "penalty_exposure": penalty_exposure,
        "utilisation_score": utilisation_score,
        "delivery_score": delivery_score,
    }
    commercial_risk_level = _compute_risk_level(commercial_findings, metrics)

    # UNKNOWN contract type → LOW risk
    if contract_type == "UNKNOWN":
        commercial_risk_level = "LOW"

    # ------------------------------------------------------------------
    # Step 4: Spend efficiency
    # ------------------------------------------------------------------
    contract_total, actual_spend, variance_pct = _compute_spend_efficiency(rs_profile)

    spend_efficiency_score: float | None = None
    if variance_pct is not None:
        spend_efficiency_score = max(0.0, 100.0 - abs(variance_pct))

    # ------------------------------------------------------------------
    # Step 5: Renewal risk scenarios (LLM Call B)
    # ------------------------------------------------------------------
    renewal_risk_scenarios: list[dict] = []
    if contract_type != "UNKNOWN":
        renewal_risk_scenarios = _llm_renewal_scenarios(
            rs_profile, vendor_id, commercial_risk_level, commercial_findings, historical_state
        )

    # ------------------------------------------------------------------
    # Step 6: Spend efficiency narrative (LLM Call C)
    # ------------------------------------------------------------------
    spend_efficiency_narrative: str | None = None
    if (
        variance_pct is not None
        and abs(variance_pct) > 15
        and contract_total is not None
        and actual_spend is not None
    ):
        spend_efficiency_narrative = _llm_spend_narrative(
            vendor_id, contract_total, actual_spend, variance_pct, commercial_risk_level
        )

    logger.debug(
        "[commercial_analyser] vendor=%r type=%r risk=%r findings=%r",
        vendor_id, contract_type, commercial_risk_level, commercial_findings,
    )

    return CommercialAnalysisResult(
        vendor_id=vendor_id,
        contract_type=contract_type,
        contract_type_confidence=contract_type_confidence,
        utilisation_score=utilisation_score,
        licence_waste_pct=licence_waste_pct,
        cost_per_seat=cost_per_seat,
        shelfware_flag=shelfware_flag,
        sla_adherence_pct=sla_adherence_pct,
        delivery_score=delivery_score,
        milestone_status=milestone_status,
        penalty_exposure=penalty_exposure,
        uptime_pct=uptime_pct,
        incident_trend=incident_trend,
        mttr_days=mttr_days,
        commercial_risk_level=commercial_risk_level,
        commercial_findings=commercial_findings,
        spend_efficiency_score=spend_efficiency_score,
        renewal_risk_scenarios=renewal_risk_scenarios,
        spend_efficiency_narrative=spend_efficiency_narrative,
        analysed_at=now,
    )
