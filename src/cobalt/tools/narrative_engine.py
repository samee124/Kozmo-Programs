"""Tool 7 (Process 4) — narrative_engine.

Generates human-readable summaries and narratives from structured outputs of
all upstream P4 tools. Two batched LLM calls. Flags narratives containing
internal labels before external distribution.

Writes to workspace: No — returns NarrativeBundle in memory.
LLM: Yes — exactly 2 calls maximum. All failures produce degraded-but-non-null output.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone

from cobalt.core.llm_call import llm_call
from cobalt.models.schemas.an_schema import (
    CommercialAnalysisResult,
    FindingNarrative,
    FindingsBundle,
    NarrativeBundle,
    QAPair,
    QASummary,
    ScoreBundle,
    ValidatedEvidenceAssembly,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

REDACTION_PATTERNS = [
    r"\bCRITICAL\b",
    r"\bHIGH finding\b",
    r"\bAT_RISK\b",
    r"\bSLA_BREACH_PATTERN\b",
    r"\bLICENCE_WASTE\b",
    r"\bCONTRACT_DEVIATION\b",
    r"\bMILESTONE_RISK\b",
    r"\bINCIDENT_FREQUENCY_RISING\b",
    r"\bCRI\s*\d+\b",
    r"\b\d+/100\b",
]

_COMPILED_PATTERNS = [re.compile(p) for p in REDACTION_PATTERNS]

_SYSTEM_FINDINGS = (
    "You are a professional procurement analyst writing clear, factual summaries. "
    "Be concise. 2-3 sentences per finding. Do not include internal scores, "
    "severity labels (HIGH/MEDIUM/CRITICAL), flag names (SLA_BREACH_PATTERN etc.), "
    "or CRI numbers in output. Return JSON only. No preamble."
)

_SYSTEM_COMMERCIAL = (
    "You are writing procurement briefing content. Be factual and concise. "
    "One sentence per Q&A summary. Return JSON only."
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_renewal_days(vendor_file: dict) -> int | None:
    expiry = vendor_file.get("expiry_date") or vendor_file.get("renewal_date")
    if not expiry:
        return None
    try:
        d = date.fromisoformat(str(expiry)[:10])
        delta = (d - date.today()).days
        return max(0, delta)
    except (ValueError, AttributeError):
        return None


def _build_citation_map(validated_assembly: ValidatedEvidenceAssembly) -> dict[str, str]:
    return {fact.field_name: fact.source_file for fact in validated_assembly.facts}


def _check_redaction(narrative_text: str) -> bool:
    for pattern in _COMPILED_PATTERNS:
        if pattern.search(narrative_text):
            return True
    return False


def _format_citations(qa_pairs: list[QAPair]) -> list[str]:
    citations: list[str] = []
    for qa in qa_pairs:
        for ec in qa.evidence_citations:
            if ec.display_text:
                citations.append(ec.display_text)
    return citations


def _call_findings_llm(
    findings_bundle: FindingsBundle,
    score_bundle: ScoreBundle,
    vendor_file: dict,
    citation_map: dict[str, str],
) -> tuple[str, dict]:
    """Call LLM for finding narratives + vendor summary. Returns (vendor_summary, finding_narratives)."""
    vendor_name = vendor_file.get("name", score_bundle.vendor_id)
    renewal_days = _get_renewal_days(vendor_file)
    cri_delta = score_bundle.cri_delta
    if cri_delta is not None:
        cri_trend = "improving" if cri_delta > 0 else ("declining" if cri_delta < 0 else "stable")
    else:
        cri_trend = "first analysis"

    findings_data = [
        {
            "id": f.finding_id,
            "title": f.title,
            "why": f.why,
            "evidence_sources": [
                citation_map.get(eid, eid) for eid in f.evidence_ids[:3]
            ],
        }
        for f in findings_bundle.findings
    ]

    top_title = (
        findings_bundle.top_findings[0].title
        if findings_bundle.top_findings
        else "None identified"
    )

    user = (
        f"Vendor: {vendor_name}\n"
        f"Health: {score_bundle.health_band}\n"
        f"CRI trend: {cri_trend}\n"
        f"Renewal: {renewal_days if renewal_days is not None else 'unknown'} days\n"
        f"Primary finding: {top_title}\n\n"
        "Write:\n"
        "1. VENDOR_SUMMARY: Exactly 2 sentences summarising current vendor status. "
        "Do not mention scores or severity labels.\n"
        "2. For each finding below, write FINDING_{{finding_id}}: 2-3 sentences explaining "
        "why it exists, what evidence supports it, and what risk exists if unaddressed. "
        "Do not use the word 'CRITICAL', 'HIGH', 'MEDIUM', or 'LOW'.\n\n"
        f"Findings:\n{json.dumps(findings_data, indent=2)}\n\n"
        'Return JSON:\n'
        '{\n'
        '  "vendor_summary": "...",\n'
        '  "finding_narratives": {"finding_id_1": "...", "finding_id_2": "..."}\n'
        '}'
    )

    try:
        result = llm_call(prompt=user, system=_SYSTEM_FINDINGS, expect_json=True)
        if not isinstance(result, dict):
            raise ValueError("LLM returned non-dict")
        vendor_summary = str(result.get("vendor_summary", ""))
        finding_narratives = result.get("finding_narratives") or {}
        if not isinstance(finding_narratives, dict):
            finding_narratives = {}
        return vendor_summary, finding_narratives

    except Exception:
        logger.warning("[narrative_engine] findings LLM call failed for vendor=%r", score_bundle.vendor_id)
        fallback_summary = (
            f"{vendor_name} — Analysis completed. "
            f"{len(findings_bundle.findings)} findings identified."
        )
        return fallback_summary, {}


def _build_metrics_text(commercial_result: CommercialAnalysisResult) -> str:
    ct = commercial_result.contract_type
    if ct == "SAAS":
        util = (
            f"{commercial_result.utilisation_score:.0%}"
            if commercial_result.utilisation_score is not None
            else "N/A"
        )
        waste = (
            f"{commercial_result.licence_waste_pct:.0f}"
            if commercial_result.licence_waste_pct is not None
            else "N/A"
        )
        return f"Utilisation: {util}, Licence waste: {waste}%"
    if ct == "SERVICES":
        sla = (
            f"{commercial_result.sla_adherence_pct:.0f}"
            if commercial_result.sla_adherence_pct is not None
            else "N/A"
        )
        delivery = (
            f"{commercial_result.delivery_score:.0f}"
            if commercial_result.delivery_score is not None
            else "N/A"
        )
        return f"SLA adherence: {sla}%, Delivery: {delivery}%"
    if ct == "MANAGED_SERVICES":
        uptime = (
            f"{commercial_result.uptime_pct:.0f}"
            if commercial_result.uptime_pct is not None
            else "N/A"
        )
        trend = commercial_result.incident_trend or "N/A"
        return f"Uptime: {uptime}%, Incident trend: {trend}"
    return "Mixed contract — see individual metrics"


def _call_commercial_llm(
    commercial_result: CommercialAnalysisResult,
    qa_pairs: list[QAPair],
) -> tuple[str | None, dict]:
    """Call LLM for commercial + Q&A summaries. Returns (commercial_summary, qa_summaries)."""
    metrics_text = _build_metrics_text(commercial_result)

    qa_data = [
        {
            "question_id": p.question_id,
            "question": p.question,
            "answer": p.answer_text,
            "confidence": p.confidence,
        }
        for p in qa_pairs
    ]

    user = (
        f"Contract type: {commercial_result.contract_type}\n"
        f"Key metrics: {metrics_text}\n"
        f"Commercial risk: {commercial_result.commercial_risk_level}\n"
        f"Active flags: {commercial_result.commercial_findings}\n\n"
        f"Q&A answers:\n{json.dumps(qa_data, indent=2)}\n\n"
        "Write:\n"
        "1. COMMERCIAL_SUMMARY: 2-3 sentences on commercial performance and risk. "
        "Do not include flag names or technical labels.\n"
        "2. For each Q&A: QA_{{question_id}}: one sentence summary for a briefing.\n\n"
        'Return JSON:\n'
        '{\n'
        '  "commercial_summary": "...",\n'
        '  "qa_summaries": {"Q1": "...", "Q2": "..."}\n'
        '}'
    )

    try:
        result = llm_call(prompt=user, system=_SYSTEM_COMMERCIAL, expect_json=True)
        if not isinstance(result, dict):
            raise ValueError("LLM returned non-dict")
        commercial_summary = result.get("commercial_summary")
        qa_summaries = result.get("qa_summaries") or {}
        if not isinstance(qa_summaries, dict):
            qa_summaries = {}
        return commercial_summary, qa_summaries

    except Exception:
        logger.warning(
            "[narrative_engine] commercial LLM call failed for contract_type=%r",
            commercial_result.contract_type,
        )
        return None, {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_narratives(
    vendor_id: str,
    findings_bundle: FindingsBundle,
    score_bundle: ScoreBundle,
    commercial_result: CommercialAnalysisResult,
    qa_pairs: list[QAPair],
    validated_assembly: ValidatedEvidenceAssembly,
    vendor_file: dict,
) -> NarrativeBundle:
    """Generate human-readable narratives from structured P4 outputs.

    Always runs LLM Call 1 (findings + vendor summary).
    LLM Call 2 (commercial + Q&A) only when contract_type != UNKNOWN and qa_pairs non-empty.
    All LLM failures return degraded-but-non-null output. Never raises.
    """
    now = _now_iso()
    citation_map = _build_citation_map(validated_assembly)

    # -----------------------------------------------------------------------
    # LLM Call 1 — finding narratives + vendor summary (always runs)
    # -----------------------------------------------------------------------
    vendor_summary, finding_narratives_dict = _call_findings_llm(
        findings_bundle, score_bundle, vendor_file, citation_map,
    )

    # Build FindingNarrative objects with redaction check
    finding_narratives: list[FindingNarrative] = []
    redaction_flags: list[str] = []

    for finding in findings_bundle.findings:
        narrative_text = finding_narratives_dict.get(finding.finding_id, "")
        flagged = _check_redaction(narrative_text) if narrative_text else False
        if flagged:
            redaction_flags.append(finding.finding_id)
        finding_narratives.append(FindingNarrative(
            finding_id=finding.finding_id,
            narrative_text=narrative_text,
            tone="factual",
            evidence_summary="",
            redaction_flag=flagged,
        ))

    # -----------------------------------------------------------------------
    # LLM Call 2 — commercial + Q&A summaries (conditional)
    # -----------------------------------------------------------------------
    commercial_summary: str | None = None
    qa_summaries_list: list[QASummary] = []

    if commercial_result.contract_type != "UNKNOWN" and len(qa_pairs) > 0:
        commercial_summary, qa_summaries_dict = _call_commercial_llm(
            commercial_result, qa_pairs,
        )

        # Build QASummary objects
        qa_index = {p.question_id: p for p in qa_pairs}
        for question_id, prose in (qa_summaries_dict or {}).items():
            qa = qa_index.get(question_id)
            if qa is None:
                continue
            qa_summaries_list.append(QASummary(
                question_id=question_id,
                question=qa.question,
                prose_summary=str(prose),
            ))

    # -----------------------------------------------------------------------
    # Evidence citations from Q&A pairs
    # -----------------------------------------------------------------------
    evidence_citations = _format_citations(qa_pairs)

    logger.debug(
        "[narrative_engine] vendor=%r findings=%d redacted=%d qa_summaries=%d",
        vendor_id,
        len(finding_narratives),
        len(redaction_flags),
        len(qa_summaries_list),
    )

    return NarrativeBundle(
        vendor_id=vendor_id,
        vendor_summary=vendor_summary,
        finding_narratives=finding_narratives,
        commercial_summary=commercial_summary,
        qa_summaries=qa_summaries_list,
        evidence_citations=evidence_citations,
        redaction_flags=redaction_flags,
        generated_at=now,
    )
