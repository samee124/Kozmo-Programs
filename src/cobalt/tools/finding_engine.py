"""Tool 6 (Process 4) — finding_engine.

Detects findings from scores, Q&A answers, trends, and commercial analysis.
Classifies evidence gaps. Generates Next Best Action. Deterministic rule engine
with optional LLM severity calibration when >= 3 HIGH findings are present.

Writes to workspace: No — returns FindingsBundle in memory.
LLM: Conditional — one call for severity calibration when >= 3 HIGH findings.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone

from cobalt.core import triage
from cobalt.core.llm_call import llm_call
from cobalt.models.schemas.an_schema import (
    ANGap,
    CommercialAnalysisResult,
    Finding,
    FindingsBundle,
    NBA,
    QAPair,
    ScoreBundle,
    ScoringConfig,
    TrendReport,
    ValidatedEvidenceAssembly,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SCORE_FINDING_THRESHOLD_MEDIUM = 65
SCORE_FINDING_THRESHOLD_HIGH = 50
SCORE_DELTA_FINDING_THRESHOLD = -10

TIER_CRI_THRESHOLDS = {
    "STRATEGIC":    70,
    "PREFERRED":    65,
    "TRANSACTIONAL": 55,
    "INCIDENTAL":   45,
}

TREND_VELOCITY_HIGH_THRESHOLD = -5.0

CRITICAL_QA_QUESTIONS = {"Q1", "Q4"}
MATERIAL_QA_QUESTIONS = {"Q1", "Q2", "Q4", "Q6"}

NBA_RENEWAL_URGENCY_DAYS = 120
NBA_COMPLIANCE_DAYS = 90

SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

COMMERCIAL_FLAG_FINDINGS: dict[str, tuple[str, str, str]] = {
    "LICENCE_WASTE": (
        "MEDIUM",
        "Licence utilisation below threshold",
        "Licence waste: {result.licence_waste_pct:.0f}% of licences unused",
    ),
    "SLA_BREACH_PATTERN": (
        "HIGH",
        "SLA breach pattern detected",
        "SLA adherence at {result.sla_adherence_pct:.0f}% against 90% target",
    ),
    "MILESTONE_RISK": (
        "MEDIUM",
        "Delivery milestone at risk",
        "Delivery score {result.delivery_score:.0f}% against 80% threshold",
    ),
    "INCIDENT_FREQUENCY_RISING": (
        "MEDIUM",
        "Incident frequency increasing",
        "Month-over-month incident count is rising",
    ),
}

# Fields expected to be present in a complete vendor evidence profile
EXPECTED_FIELDS = {
    "contract_term_end",
    "sla_target",
    "spend_ytd",
    "compliance_status",
    "primary_contact",
    "incident_count",
}

_LLM_CALIBRATION_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_finding(
    severity: str,
    source: str,
    title: str,
    why: str,
    evidence_ids: list[str] | None = None,
) -> Finding:
    return Finding(
        finding_id=f"finding-{uuid.uuid4().hex[:8]}",
        title=title,
        severity=severity,
        why=why,
        evidence_ids=evidence_ids or [],
        source=source,
        status="OPEN",
        created_at=_now_iso(),
    )


def _score_findings(score_bundle: ScoreBundle, rs_profile: object) -> list[Finding]:
    findings: list[Finding] = []

    for ds in score_bundle.dimension_scores:
        if ds.score < SCORE_FINDING_THRESHOLD_HIGH:
            findings.append(_make_finding(
                severity="HIGH",
                source="SCORE",
                title=f"{ds.dimension} performance significantly below threshold",
                why=(
                    f"{ds.dimension} score is {ds.score}/100, "
                    f"below the critical threshold of {SCORE_FINDING_THRESHOLD_HIGH}"
                ),
            ))
        elif ds.score < SCORE_FINDING_THRESHOLD_MEDIUM:
            findings.append(_make_finding(
                severity="MEDIUM",
                source="SCORE",
                title=f"{ds.dimension} below acceptable threshold",
                why=(
                    f"{ds.dimension} score is {ds.score}/100, "
                    f"below the acceptable threshold of {SCORE_FINDING_THRESHOLD_MEDIUM}"
                ),
            ))

        if ds.delta is not None and ds.delta <= SCORE_DELTA_FINDING_THRESHOLD:
            findings.append(_make_finding(
                severity="MEDIUM",
                source="SCORE",
                title=f"{ds.dimension} declining rapidly",
                why=f"{ds.dimension} fell {abs(ds.delta)} points since last review",
            ))

    # Tier CRI threshold
    relationship_type = rs_profile.relationship_classification.relationship_type
    tier_threshold = TIER_CRI_THRESHOLDS.get(relationship_type, 55)
    if score_bundle.cri_score < tier_threshold:
        findings.append(_make_finding(
            severity="HIGH",
            source="SCORE",
            title=f"CRI below threshold for {relationship_type} vendor",
            why=(
                f"CRI of {score_bundle.cri_score} is below the {tier_threshold} threshold "
                f"expected for a {relationship_type} vendor"
            ),
        ))

    return findings


def _qa_findings(qa_pairs: list[QAPair]) -> tuple[list[Finding], list[ANGap]]:
    findings: list[Finding] = []
    gaps: list[ANGap] = []

    for qa in qa_pairs:
        if qa.completeness == "PARTIAL" and qa.question_id in MATERIAL_QA_QUESTIONS:
            findings.append(_make_finding(
                severity="MEDIUM",
                source="QA",
                title=f"Insufficient evidence for {qa.question_id} assessment",
                why=f"Incomplete answer to: {qa.question[:80]}",
            ))

        if qa.completeness == "UNANSWERABLE" and qa.question_id in CRITICAL_QA_QUESTIONS:
            findings.append(_make_finding(
                severity="HIGH",
                source="QA",
                title=f"Critical question unanswerable: {qa.question_id}",
                why=f"Cannot assess: {qa.question[:80]}",
            ))
            missing_desc = ", ".join(qa.missing_evidence[:2]) if qa.missing_evidence else "supporting evidence"
            gaps.append(ANGap(
                severity="BLOCKING",
                description=f"Evidence required to answer: {qa.question}",
                suggested_action=(
                    f"Upload supporting documents or provide check-in response "
                    f"addressing: {missing_desc}"
                ),
            ))

    return findings, gaps


def _trend_findings(trend_report: TrendReport) -> list[Finding]:
    findings: list[Finding] = []

    for dimension, trend in trend_report.dimension_trends.items():
        if (
            trend.get("direction") == "DECLINING"
            and trend.get("velocity") is not None
            and trend["velocity"] <= TREND_VELOCITY_HIGH_THRESHOLD
        ):
            findings.append(_make_finding(
                severity="HIGH",
                source="TREND",
                title=f"{dimension} in accelerating decline",
                why=f"{dimension} declining at {abs(trend['velocity']):.1f} pts/month",
            ))

        if trend.get("inflection_point"):
            findings.append(_make_finding(
                severity="MEDIUM",
                source="TREND",
                title=f"{dimension} trend reversed to declining",
                why=f"Trend changed direction at {trend['inflection_point']}",
            ))

    return findings


def _commercial_findings(commercial_result: CommercialAnalysisResult) -> list[Finding]:
    findings: list[Finding] = []

    if commercial_result.commercial_risk_level in ("HIGH", "CRITICAL"):
        findings.append(_make_finding(
            severity="HIGH",
            source="COMMERCIAL",
            title="Commercial risk elevated",
            why=f"Commercial risk assessed as {commercial_result.commercial_risk_level}",
        ))

    for flag, (severity, title, why_tpl) in COMMERCIAL_FLAG_FINDINGS.items():
        if flag in commercial_result.commercial_findings:
            try:
                why = why_tpl.format(result=commercial_result)
            except (TypeError, ValueError, AttributeError):
                why = title
            findings.append(_make_finding(
                severity=severity,
                source="COMMERCIAL",
                title=title,
                why=why,
            ))

    return findings


def _evidence_gaps(validated_assembly: ValidatedEvidenceAssembly) -> list[ANGap]:
    gaps: list[ANGap] = []
    for fact in validated_assembly.facts:
        if fact.freshness_status == "MISSING" and fact.field_name in EXPECTED_FIELDS:
            gaps.append(ANGap(
                severity="ENRICHMENT",
                description=f"Missing evidence: {fact.field_name}",
                suggested_action=f"Provide {fact.field_name} data for accurate analysis",
            ))
    return gaps


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    by_key: dict[tuple[str, str], Finding] = {}
    for f in findings:
        key = (f.source, f.title[:40])
        if key in by_key:
            existing = by_key[key]
            if SEVERITY_ORDER.get(f.severity, 0) > SEVERITY_ORDER.get(existing.severity, 0):
                merged_ids = existing.evidence_ids + f.evidence_ids
                by_key[key] = Finding(
                    finding_id=existing.finding_id,
                    title=existing.title,
                    severity=f.severity,
                    why=f.why,
                    evidence_ids=merged_ids,
                    source=existing.source,
                    status=existing.status,
                    created_at=existing.created_at,
                )
            else:
                existing.evidence_ids.extend(f.evidence_ids)
        else:
            by_key[key] = f
    return list(by_key.values())


def _llm_calibrate(
    findings: list[Finding],
    score_bundle: ScoreBundle,
    rs_profile: object,
    vendor_id: str,
) -> list[Finding]:
    high_critical = [f for f in findings if f.severity in ("HIGH", "CRITICAL")]
    if len(high_critical) < _LLM_CALIBRATION_THRESHOLD:
        return findings

    renewal_days = _get_renewal_days(rs_profile)
    relationship_type = rs_profile.relationship_classification.relationship_type

    findings_list = [
        {
            "finding_id": f.finding_id,
            "title": f.title,
            "current_severity": f.severity,
            "why": f.why,
            "source": f.source,
        }
        for f in findings
    ]

    system = (
        "You are reviewing findings for a vendor risk assessment. You may ELEVATE "
        "severity but NEVER reduce below the rule-based floor. Return JSON only."
    )
    user = (
        f"Vendor: {vendor_id}, CRI: {score_bundle.cri_score}, "
        f"Tier: {relationship_type}\n"
        f"Renewal in: {renewal_days if renewal_days is not None else 'unknown'} days\n\n"
        f"Findings:\n{json.dumps(findings_list, indent=2)}\n\n"
        'Return: {"calibrations": [{"finding_id": "...", "severity": "...", "reason": "..."}]}'
    )

    try:
        result = llm_call(prompt=user, system=system, expect_json=True)
        if not isinstance(result, dict):
            return findings

        calibrations = result.get("calibrations") or []
        finding_index = {f.finding_id: f for f in findings}

        for cal in calibrations:
            fid = cal.get("finding_id")
            new_sev = cal.get("severity")
            if not fid or not new_sev:
                continue
            target = finding_index.get(fid)
            if target is None:
                continue
            if SEVERITY_ORDER.get(new_sev, 0) > SEVERITY_ORDER.get(target.severity, 0):
                target.severity = new_sev

    except Exception:
        logger.warning("[finding_engine] LLM calibration failed for vendor=%r", vendor_id)

    return findings


def _get_renewal_days(rs_profile: object) -> int | None:
    try:
        earliest: date | None = None
        for ct in rs_profile.contract_terms:
            if not ct.expiry_date:
                continue
            try:
                d = date.fromisoformat(ct.expiry_date[:10])
                if earliest is None or d < earliest:
                    earliest = d
            except (ValueError, AttributeError):
                continue
        if earliest is None:
            return None
        delta = (earliest - date.today()).days
        return max(0, delta)
    except Exception:
        return None


def _derive_action(finding: Finding) -> str:
    if finding.source == "QA":
        return "Obtain missing evidence and re-assess vendor status"
    if finding.severity in ("HIGH", "CRITICAL") and finding.source == "SCORE":
        return "Escalate to vendor management immediately"
    if finding.source == "TREND":
        return "Investigate trend root cause and agree recovery plan with vendor"
    if finding.source == "COMMERCIAL":
        return "Review commercial terms with procurement and vendor owner"
    return "Monitor and review at next scheduled vendor meeting"


def _derive_timing(finding: Finding | None, renewal_days: int | None) -> str:
    if finding is None:
        return "MONITOR"
    if finding.severity in ("HIGH", "CRITICAL"):
        if renewal_days is not None and renewal_days < NBA_COMPLIANCE_DAYS:
            return "NOW"
        return "THIS_WEEK"
    if renewal_days is not None and renewal_days < NBA_RENEWAL_URGENCY_DAYS:
        return "BEFORE_RENEWAL"
    return "MONITOR"


def _select_nba(
    sorted_findings: list[Finding],
    renewal_days: int | None,
) -> NBA | None:
    if not sorted_findings:
        return None
    top = sorted_findings[0]
    return NBA(
        action=_derive_action(top),
        why=top.why,
        owner="vendor_owner",
        timing=_derive_timing(top, renewal_days),
        review_required=top.severity in ("HIGH", "CRITICAL"),
        linked_finding_id=top.finding_id,
        created_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_findings(
    vendor_id: str,
    programme_id: str,
    score_bundle: ScoreBundle,
    qa_pairs: list[QAPair],
    trend_report: TrendReport,
    commercial_result: CommercialAnalysisResult,
    validated_assembly: ValidatedEvidenceAssembly,
    rs_profile: object,
    scoring_config: ScoringConfig,
) -> FindingsBundle:
    """Detect findings from scores, Q&A, trends, and commercial analysis.

    Deterministic rule engine with optional LLM severity calibration.
    Returns FindingsBundle in memory.
    """
    now = _now_iso()

    # 1. Collect raw findings from all sources
    all_findings: list[Finding] = []
    all_gaps: list[ANGap] = []

    all_findings.extend(_score_findings(score_bundle, rs_profile))
    qa_f, qa_g = _qa_findings(qa_pairs)
    all_findings.extend(qa_f)
    all_gaps.extend(qa_g)
    all_findings.extend(_trend_findings(trend_report))
    all_findings.extend(_commercial_findings(commercial_result))
    all_gaps.extend(_evidence_gaps(validated_assembly))

    # 2. Deduplicate
    findings = _deduplicate(all_findings)

    # 3. Optional LLM calibration
    findings = _llm_calibrate(findings, score_bundle, rs_profile, vendor_id)

    # 4. Triage tasks for BLOCKING gaps
    triage_tasks: list[dict] = []
    for gap in all_gaps:
        if gap.severity == "BLOCKING":
            gap_dict = {
                "severity": "BLOCKING",
                "description": gap.description,
                "suggested_action": gap.suggested_action,
            }
            tasks = triage.generate_triage_tasks(
                [gap_dict], [], vendor_id, programme_id,
            )
            triage_tasks.extend(tasks)

    # 5. Sort by severity and build NBA
    sorted_findings = sorted(
        findings,
        key=lambda f: SEVERITY_ORDER.get(f.severity, 0),
        reverse=True,
    )
    renewal_days = _get_renewal_days(rs_profile)
    nba = _select_nba(sorted_findings, renewal_days)
    top_findings = sorted_findings[:3]

    logger.debug(
        "[finding_engine] vendor=%r findings=%d gaps=%d nba=%s",
        vendor_id, len(findings), len(all_gaps), nba.action if nba else None,
    )

    return FindingsBundle(
        vendor_id=vendor_id,
        findings=findings,
        gaps=all_gaps,
        nba=nba,
        top_findings=top_findings,
        triage_tasks=triage_tasks,
        generated_at=now,
    )
