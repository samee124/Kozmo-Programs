"""Tool 5 (Process 4) — inquiry_engine.

Answers structured questions about a vendor from validated evidence.
Tiered depth — Tier 1 always, Tier 2 on weak answers, Tier 3 on material issues.
One LLM call per question. Returns list[QAPair] in memory.

Writes to workspace: No — returns list[QAPair] in memory.
LLM: Yes — one call per question. 6–12 calls per vendor depending on tier activation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from cobalt.core.llm_call import llm_call
from cobalt.models.schemas.an_schema import (
    CommercialAnalysisResult,
    EvidenceCitation,
    HistoricalQAState,
    QAPair,
    QuestionSetItem,
    ScoringConfig,
    ValidatedEvidenceAssembly,
    ValidatedEvidenceFact,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

TIER_1_QUESTIONS: list[QuestionSetItem] = [
    QuestionSetItem(
        question_id="Q1", tier=1, dimension="delivery_reliability",
        question="Is this vendor meeting its contracted delivery commitments and SLA targets?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q2", tier=1, dimension="responsiveness",
        question="Is this vendor responding to issues, escalations, and requests within agreed timeframes?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q3", tier=1, dimension="commercial_value",
        question="Is the spend on this vendor delivering expected business value relative to contract cost?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q4", tier=1, dimension="risk_compliance",
        question="Are there active compliance failures, security risks, or regulatory concerns with this vendor?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q5", tier=1, dimension="relationship_trend",
        question="Is the overall quality of the relationship improving, stable, or declining?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q6", tier=1, dimension="renewal_readiness",
        question="What is the renewal posture and are we prepared for the upcoming contract decision?",
        contract_types=[],
    ),
]

CRITICAL_QUESTIONS = {"Q1", "Q4"}
MAX_TIER3_QUESTIONS = 2
MAX_EVIDENCE_TEXT_CHARS = 4000

_SYSTEM_PROMPT = (
    "You are a procurement intelligence analyst answering questions about vendor "
    "performance. Answer only from the evidence provided. If evidence is insufficient, "
    "say so explicitly. Return JSON only. No preamble."
)

_REQUIRED_KEYS = {"answer_text", "confidence", "completeness"}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_evidence_text(
    validated_assembly: ValidatedEvidenceAssembly,
    commercial_result: CommercialAnalysisResult,
) -> str:
    """Build evidence_text sorted by quality_score descending, capped at 4000 chars."""
    sorted_facts = sorted(
        validated_assembly.facts,
        key=lambda f: f.quality_score,
        reverse=True,
    )

    fact_lines: list[str] = []
    for fact in sorted_facts:
        fact_lines.append(
            f"{fact.field_name}: {fact.display_value} "
            f"[source: {fact.source_file}, confidence: {fact.confidence}, "
            f"quality: {fact.quality_score:.2f}]"
        )

    commercial_lines: list[str] = [
        f"[COMMERCIAL] contract_type: {commercial_result.contract_type}",
        f"[COMMERCIAL] commercial_risk: {commercial_result.commercial_risk_level}",
    ]
    if commercial_result.sla_adherence_pct is not None:
        commercial_lines.append(
            f"[COMMERCIAL] sla_adherence: {commercial_result.sla_adherence_pct}%"
        )
    if commercial_result.licence_waste_pct is not None:
        commercial_lines.append(
            f"[COMMERCIAL] licence_waste: {commercial_result.licence_waste_pct}%"
        )

    commercial_block = "\n".join(commercial_lines)
    fact_block = "\n".join(fact_lines)
    full_text = fact_block + "\n" + commercial_block if fact_block else commercial_block

    if len(full_text) <= MAX_EVIDENCE_TEXT_CHARS:
        return full_text

    # Truncate fact lines from the bottom (lowest quality), keep commercial block
    budget = MAX_EVIDENCE_TEXT_CHARS - len(commercial_block) - 1
    truncated_facts = fact_block[:budget] if budget > 0 else ""
    return truncated_facts + "\n" + commercial_block


def _get_prior_answer(
    question_id: str,
    historical_qa: HistoricalQAState | None,
) -> str | None:
    if historical_qa is None:
        return None
    for pair in historical_qa.prior_pairs:
        if pair.get("question_id") == question_id:
            return pair.get("answer_text")
    return None


def _build_citations(
    evidence_used: list,
    validated_assembly: ValidatedEvidenceAssembly,
) -> list[EvidenceCitation]:
    fact_index: dict[str, ValidatedEvidenceFact] = {
        f.field_name: f for f in validated_assembly.facts
    }
    citations: list[EvidenceCitation] = []
    for field_name in (evidence_used or []):
        fact = fact_index.get(str(field_name))
        if fact is None:
            continue
        if fact.source_section:
            display_text = (
                f"{fact.source_file} · {fact.source_section} [{fact.extraction_type}]"
            )
        else:
            display_text = f"{fact.source_file} [{fact.extraction_type}]"
        citations.append(EvidenceCitation(
            evidence_id=field_name,
            source_file=fact.source_file,
            source_section=fact.source_section,
            extraction_type=fact.extraction_type,
            quality_score=fact.quality_score,
            display_text=display_text,
        ))
    return citations


def _answer_question(
    question: str,
    question_id: str,
    evidence_text: str,
    prior_answer: str | None,
    vendor_id: str,
    contract_type: str,
    tier: int,
    validated_assembly: ValidatedEvidenceAssembly,
) -> QAPair:
    now = _now_iso()
    user = (
        f"Question: {question}\n"
        f"Vendor: {vendor_id}\n"
        f"Contract type: {contract_type}\n"
        f"Prior answer (if available): {prior_answer or 'None'}\n\n"
        f"Evidence:\n{evidence_text}\n\n"
        'Return JSON:\n{\n'
        '  "answer_text": "2-3 sentences",\n'
        '  "confidence": "HIGH|MEDIUM|LOW",\n'
        '  "completeness": "COMPLETE|PARTIAL|UNANSWERABLE",\n'
        '  "evidence_used": ["field_name_1", "field_name_2"],\n'
        '  "missing_evidence": ["description of what would improve this answer"]\n'
        '}'
    )

    try:
        result = llm_call(prompt=user, system=_SYSTEM_PROMPT, expect_json=True)
        if not isinstance(result, dict):
            raise ValueError("LLM returned non-dict response")
        if not _REQUIRED_KEYS.issubset(result.keys()):
            missing = _REQUIRED_KEYS - result.keys()
            raise ValueError(f"LLM response missing required keys: {missing}")

        answer_text = str(result.get("answer_text", ""))
        confidence = str(result.get("confidence", "LOW"))
        completeness = str(result.get("completeness", "UNANSWERABLE"))
        evidence_used = result.get("evidence_used") or []
        missing_evidence = result.get("missing_evidence") or []

        citations = _build_citations(evidence_used, validated_assembly)

    except Exception:
        logger.warning(
            "[inquiry_engine] LLM failed for question_id=%r tier=%d", question_id, tier,
        )
        return QAPair(
            question_id=question_id,
            question=question,
            answer_text="Unable to answer — LLM unavailable.",
            confidence="LOW",
            completeness="UNANSWERABLE",
            answered_by="inquiry_engine",
            evidence_citations=[],
            missing_evidence=[],
            tier=tier,
            answered_at=now,
        )

    return QAPair(
        question_id=question_id,
        question=question,
        answer_text=answer_text,
        confidence=confidence,
        completeness=completeness,
        answered_by="inquiry_engine",
        evidence_citations=citations,
        missing_evidence=missing_evidence if isinstance(missing_evidence, list) else [],
        tier=tier,
        answered_at=now,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_inquiry(
    vendor_id: str,
    validated_assembly: ValidatedEvidenceAssembly,
    commercial_result: CommercialAnalysisResult,
    rs_profile: object,
    historical_qa: HistoricalQAState | None,
    scoring_config: ScoringConfig,
) -> list[QAPair]:
    """Answer structured questions about a vendor from validated evidence.

    Always runs all 6 Tier 1 questions. Tier 2 activates for weak answers.
    Tier 3 activates for UNANSWERABLE material questions, capped at MAX_TIER3_QUESTIONS.
    """
    contract_type = commercial_result.contract_type
    evidence_text = _build_evidence_text(validated_assembly, commercial_result)
    qa_pairs: list[QAPair] = []

    # -----------------------------------------------------------------------
    # Tier 1 — always runs all 6 questions
    # -----------------------------------------------------------------------
    for item in TIER_1_QUESTIONS:
        prior_answer = _get_prior_answer(item.question_id, historical_qa)
        qa = _answer_question(
            question=item.question,
            question_id=item.question_id,
            evidence_text=evidence_text,
            prior_answer=prior_answer,
            vendor_id=vendor_id,
            contract_type=contract_type,
            tier=1,
            validated_assembly=validated_assembly,
        )
        qa_pairs.append(qa)

    # -----------------------------------------------------------------------
    # Tier 2 — weak Tier 1 answers (LOW confidence or PARTIAL completeness)
    # -----------------------------------------------------------------------
    # Track (original_question_text, question_id, tier2_pair) for Tier 3 lookup
    tier2_entries: list[tuple[str, str, QAPair]] = []

    for qa in list(qa_pairs):  # snapshot — prevents mutation-during-iteration
        if qa.confidence == "LOW" or qa.completeness == "PARTIAL":
            missing = (
                qa.missing_evidence[0] if qa.missing_evidence else "evidence is limited"
            )
            tier2_q = (
                f"Given that {missing}, specifically regarding: {qa.question} "
                f"— what can be determined from the available signals and context?"
            )
            tier2_qa = _answer_question(
                question=tier2_q,
                question_id=qa.question_id,
                evidence_text=evidence_text,
                prior_answer=None,
                vendor_id=vendor_id,
                contract_type=contract_type,
                tier=2,
                validated_assembly=validated_assembly,
            )
            tier2_entries.append((qa.question, qa.question_id, tier2_qa))
            qa_pairs.append(tier2_qa)

    # -----------------------------------------------------------------------
    # Tier 3 — critical UNANSWERABLE Tier 2 results (capped)
    # -----------------------------------------------------------------------
    tier3_count = 0
    for original_question, question_id, tier2_qa in tier2_entries:
        if tier3_count >= MAX_TIER3_QUESTIONS:
            break
        if (
            tier2_qa.completeness == "UNANSWERABLE"
            and question_id in CRITICAL_QUESTIONS
        ):
            tier3_q = (
                f"For the critical question '{original_question}': "
                f"given all available signals, what is the best available assessment "
                f"and what specific evidence would be needed to answer definitively?"
            )
            tier3_qa = _answer_question(
                question=tier3_q,
                question_id=question_id,
                evidence_text=evidence_text,
                prior_answer=None,
                vendor_id=vendor_id,
                contract_type=contract_type,
                tier=3,
                validated_assembly=validated_assembly,
            )
            qa_pairs.append(tier3_qa)
            tier3_count += 1

    logger.debug(
        "[inquiry_engine] vendor=%r total_qa=%d tier2=%d tier3=%d",
        vendor_id, len(qa_pairs), len(tier2_entries), tier3_count,
    )

    return qa_pairs
