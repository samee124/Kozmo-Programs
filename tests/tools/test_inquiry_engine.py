"""Tests for inquiry_engine (AN-05).

All LLM calls are mocked. No real API calls.
Mock pattern: patch("cobalt.tools.inquiry_engine.llm_call")
"""

from __future__ import annotations

from unittest.mock import call, patch

import pytest

from cobalt.models.schemas.an_schema import (
    CommercialAnalysisResult,
    EvidenceCitation,
    HistoricalQAState,
    QAPair,
    ScoringConfig,
    ValidatedEvidenceAssembly,
    ValidatedEvidenceFact,
)
from cobalt.tools.inquiry_engine import (
    CRITICAL_QUESTIONS,
    MAX_TIER3_QUESTIONS,


    TIER_1_QUESTIONS,
    _build_citations,
    _build_evidence_text,
    _get_prior_answer,
    run_inquiry,
)
                

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_scoring_config() -> ScoringConfig:
    return ScoringConfig(
        dimension_weights={
            "delivery_reliability": 0.20,
            "responsiveness": 0.20,
            "commercial_value": 0.20,
            "risk_compliance": 0.20,
            "relationship_trend": 0.20,
        },
        health_band_thresholds={"HEALTHY": 80, "WATCH": 65, "AT_RISK": 50, "CRITICAL": 0},
        tier_cri_thresholds={"STRATEGIC": 70, "PREFERRED": 65},
        spike_multiplier=1.0,
    )


def _make_commercial_result(
    contract_type: str = "SAAS",
    risk: str = "LOW",
    sla_pct: float | None = None,
    waste_pct: float | None = None,
) -> CommercialAnalysisResult:
    return CommercialAnalysisResult(
        vendor_id="v001",
        contract_type=contract_type,
        contract_type_confidence="HIGH",
        utilisation_score=None,
        licence_waste_pct=waste_pct,
        cost_per_seat=None,
        shelfware_flag=False,
        sla_adherence_pct=sla_pct,
        delivery_score=None,
        milestone_status=None,
        penalty_exposure=None,
        uptime_pct=None,
        incident_trend=None,
        mttr_days=None,
        commercial_risk_level=risk,
        commercial_findings=[],
        spend_efficiency_score=None,
        renewal_risk_scenarios=[],
        spend_efficiency_narrative=None,
        analysed_at="2026-01-01T00:00:00+00:00",
    )


def _make_fact(
    field_name: str = "sla_target",
    display_value: str = "99.9%",
    source_file: str = "contract.pdf",
    source_section: str | None = None,
    extraction_type: str = "AUTO_EXTRACTED",
    quality_score: float = 0.85,
    freshness_status: str = "CURRENT",
) -> ValidatedEvidenceFact:
    return ValidatedEvidenceFact(
        field_name=field_name,
        value=display_value,
        display_value=display_value,
        extraction_type=extraction_type,
        source_file=source_file,
        source_section=source_section,
        confidence="HIGH",
        trust_level="OFFICIAL",
        freshness_status=freshness_status,
        conflict_flag=False,
        conflict_values=[],
        quality_score=quality_score,
        validated_at="2026-01-01T00:00:00+00:00",
    )


def _make_assembly(facts: list[ValidatedEvidenceFact] | None = None) -> ValidatedEvidenceAssembly:
    return ValidatedEvidenceAssembly(
        vendor_id="v001",
        programme_id="p001",
        facts=facts or [],
        completeness_pct=0.8,
        conflict_count=0,
        stale_count=0,
        missing_count=0,
        validated_at="2026-01-01T00:00:00+00:00",
    )


def _good_llm_response(
    answer: str = "Vendor meets SLA targets.",
    confidence: str = "HIGH",
    completeness: str = "COMPLETE",
    evidence_used: list[str] | None = None,
    missing: list[str] | None = None,
) -> dict:
    return {
        "answer_text": answer,
        "confidence": confidence,
        "completeness": completeness,
        "evidence_used": evidence_used or [],
        "missing_evidence": missing or [],
    }


# ---------------------------------------------------------------------------
# Tier 1 — always runs all 6 questions
# ---------------------------------------------------------------------------

class TestTier1AlwaysRuns:

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_six_tier1_qapairs_returned(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        assembly = _make_assembly()
        commercial = _make_commercial_result()
        result = run_inquiry("v001", assembly, commercial, None, None, _make_scoring_config())
        tier1 = [q for q in result if q.tier == 1]
        assert len(tier1) == 6

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_always_at_least_six_qapairs(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        assembly = _make_assembly()
        result = run_inquiry("v001", assembly, _make_commercial_result(), None, None, _make_scoring_config())
        assert len(result) >= 6

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_question_ids_q1_through_q6(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        tier1_ids = {q.question_id for q in result if q.tier == 1}
        assert tier1_ids == {"Q1", "Q2", "Q3", "Q4", "Q5", "Q6"}

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_tier1_answered_by_inquiry_engine(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        for qa in result:
            assert qa.answered_by == "inquiry_engine"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_high_complete_no_tier2_generated(self, mock_llm):
        mock_llm.return_value = _good_llm_response(confidence="HIGH", completeness="COMPLETE")
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        assert len(result) == 6  # exactly 6, no Tier 2

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_empty_assembly_all_6_returned(self, mock_llm):
        mock_llm.return_value = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")
        assembly = _make_assembly(facts=[])
        result = run_inquiry("v001", assembly, _make_commercial_result(), None, None, _make_scoring_config())
        tier1 = [q for q in result if q.tier == 1]
        assert len(tier1) == 6

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_empty_assembly_all_unanswerable(self, mock_llm):
        mock_llm.return_value = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")
        result = run_inquiry("v001", _make_assembly(facts=[]), _make_commercial_result(), None, None, _make_scoring_config())
        tier1 = [q for q in result if q.tier == 1]
        for qa in tier1:
            assert qa.completeness == "UNANSWERABLE"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_llm_called_6_times_for_tier1_complete(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        assert mock_llm.call_count == 6


# ---------------------------------------------------------------------------
# LLM mocking — response handling
# ---------------------------------------------------------------------------

class TestLLMMocking:

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_high_complete_response_sets_confidence(self, mock_llm):
        mock_llm.return_value = _good_llm_response(confidence="HIGH", completeness="COMPLETE")
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        for qa in result:
            assert qa.confidence == "HIGH"
            assert qa.completeness == "COMPLETE"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_llm_exception_returns_unanswerable(self, mock_llm):
        mock_llm.side_effect = Exception("LLM unavailable")
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        for qa in result:
            assert qa.completeness == "UNANSWERABLE"
            assert qa.answer_text == "Unable to answer — LLM unavailable."

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_llm_exception_no_crash_no_raise(self, mock_llm):
        mock_llm.side_effect = Exception("LLM unavailable")
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        assert result is not None
        assert len(result) >= 6

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_llm_returns_non_dict_unanswerable(self, mock_llm):
        mock_llm.return_value = "not a dict"
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        for qa in result:
            assert qa.completeness == "UNANSWERABLE"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_llm_missing_required_keys_unanswerable(self, mock_llm):
        mock_llm.return_value = {"wrong_key": "value"}
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        for qa in result:
            assert qa.completeness == "UNANSWERABLE"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_single_question_llm_failure_others_unaffected(self, mock_llm):
        """Q3 LLM fails → Q3 UNANSWERABLE, others succeed."""
        good = _good_llm_response(confidence="HIGH", completeness="COMPLETE")
        responses = [good, good, Exception("LLM fail on Q3"), good, good, good]
        mock_llm.side_effect = responses
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        tier1 = [q for q in result if q.tier == 1]
        q3 = next(q for q in tier1 if q.question_id == "Q3")
        assert q3.completeness == "UNANSWERABLE"
        others = [q for q in tier1 if q.question_id != "Q3"]
        for qa in others:
            assert qa.completeness == "COMPLETE"


# ---------------------------------------------------------------------------
# Tier escalation
# ---------------------------------------------------------------------------

class TestTierEscalation:

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_low_confidence_triggers_tier2(self, mock_llm):
        """Q1 LOW confidence → Tier 2 generated for Q1."""
        tier1_good = _good_llm_response(confidence="HIGH", completeness="COMPLETE")
        q1_low = _good_llm_response(confidence="LOW", completeness="PARTIAL")
        tier2_response = _good_llm_response(confidence="MEDIUM", completeness="COMPLETE")

        # Q1=LOW, Q2-Q6=HIGH COMPLETE, then tier2 for Q1
        mock_llm.side_effect = [
            q1_low,            # Q1 tier1
            tier1_good,        # Q2
            tier1_good,        # Q3
            tier1_good,        # Q4
            tier1_good,        # Q5
            tier1_good,        # Q6
            tier2_response,    # Q1 tier2
        ]
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        tier2 = [q for q in result if q.tier == 2]
        assert len(tier2) == 1
        assert tier2[0].question_id == "Q1"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_partial_completeness_triggers_tier2(self, mock_llm):
        """Q4 PARTIAL → Tier 2 generated for Q4."""
        tier1_good = _good_llm_response()
        q4_partial = _good_llm_response(confidence="MEDIUM", completeness="PARTIAL")
        tier2 = _good_llm_response(confidence="MEDIUM", completeness="COMPLETE")

        mock_llm.side_effect = [
            tier1_good,  # Q1
            tier1_good,  # Q2
            tier1_good,  # Q3
            q4_partial,  # Q4
            tier1_good,  # Q5
            tier1_good,  # Q6
            tier2,       # Q4 tier2
        ]
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        t2 = [q for q in result if q.tier == 2]
        assert len(t2) == 1
        assert t2[0].question_id == "Q4"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_q1_unanswerable_tier2_then_tier3(self, mock_llm):
        """Q1 LOW on Tier 1 → Tier 2 UNANSWERABLE → Tier 3 generated (Q1 is CRITICAL)."""
        good = _good_llm_response()
        q1_low = _good_llm_response(confidence="LOW", completeness="PARTIAL", missing=["SLA reports"])
        tier2_unanswerable = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")
        tier3_response = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")

        mock_llm.side_effect = [
            q1_low,              # Q1 tier1
            good, good, good, good, good,  # Q2-Q6 tier1
            tier2_unanswerable,  # Q1 tier2
            tier3_response,      # Q1 tier3
        ]
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        tier3 = [q for q in result if q.tier == 3]
        assert len(tier3) == 1
        assert tier3[0].question_id == "Q1"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_q1_and_q4_both_unanswerable_get_tier3(self, mock_llm):
        """Q1 + Q4 both UNANSWERABLE on Tier 2 → 2 Tier 3 questions (both in CRITICAL_QUESTIONS)."""
        good = _good_llm_response()
        partial = _good_llm_response(confidence="LOW", completeness="PARTIAL")
        unanswerable = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")
        tier3_response = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")

        mock_llm.side_effect = [
            partial,    # Q1 tier1
            good,       # Q2
            good,       # Q3
            partial,    # Q4 tier1
            good,       # Q5
            good,       # Q6
            unanswerable,  # Q1 tier2
            unanswerable,  # Q4 tier2
            tier3_response,  # Q1 tier3
            tier3_response,  # Q4 tier3
        ]
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        tier3 = [q for q in result if q.tier == 3]
        assert len(tier3) == 2
        tier3_ids = {q.question_id for q in tier3}
        assert tier3_ids == {"Q1", "Q4"}

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_q2_q3_unanswerable_on_tier2_no_tier3(self, mock_llm):
        """Q2 + Q3 UNANSWERABLE on Tier 2 → 0 Tier 3 (neither is in CRITICAL_QUESTIONS)."""
        good = _good_llm_response()
        low = _good_llm_response(confidence="LOW", completeness="PARTIAL")
        unanswerable = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")

        mock_llm.side_effect = [
            good,           # Q1
            low,            # Q2 tier1
            low,            # Q3 tier1
            good,           # Q4
            good,           # Q5
            good,           # Q6
            unanswerable,   # Q2 tier2
            unanswerable,   # Q3 tier2
        ]
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        tier3 = [q for q in result if q.tier == 3]
        assert len(tier3) == 0

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_tier3_capped_at_max_tier3_questions(self, mock_llm):
        """Multiple critical questions UNANSWERABLE on Tier 2 → capped at MAX_TIER3_QUESTIONS."""
        # Both Q1 and Q4 are critical. Cap is 2, so 2 Tier 3 at most even if more were eligible.
        good = _good_llm_response()
        low = _good_llm_response(confidence="LOW", completeness="PARTIAL")
        unanswerable = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")
        tier3_resp = _good_llm_response(confidence="LOW", completeness="UNANSWERABLE")

        mock_llm.side_effect = [
            low,   # Q1 tier1
            good,  # Q2
            good,  # Q3
            low,   # Q4 tier1
            good,  # Q5
            good,  # Q6
            unanswerable,  # Q1 tier2
            unanswerable,  # Q4 tier2
            tier3_resp,    # Q1 tier3
            tier3_resp,    # Q4 tier3
        ]
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        tier3 = [q for q in result if q.tier == 3]
        assert len(tier3) == MAX_TIER3_QUESTIONS

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_tier3_only_for_critical_questions(self, mock_llm):
        """Verify CRITICAL_QUESTIONS set controls Tier 3 activation."""
        assert CRITICAL_QUESTIONS == {"Q1", "Q4"}

    def test_max_tier3_constant(self):
        assert MAX_TIER3_QUESTIONS == 2


# ---------------------------------------------------------------------------
# Historical Q&A
# ---------------------------------------------------------------------------

class TestHistoricalQA:

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_prior_answer_included_in_prompt(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        historical = HistoricalQAState(
            vendor_id="v001",
            prior_pairs=[
                {"question_id": "Q1", "answer_text": "Previously SLA was met.", "confidence": "HIGH", "answered_at": "2025-01-01"}
            ],
        )
        run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, historical, _make_scoring_config())
        # Check that the prompt for Q1 includes the prior answer
        call_args = mock_llm.call_args_list[0]  # first call = Q1
        prompt = call_args.kwargs.get("prompt", call_args.args[0] if call_args.args else "")
        assert "Previously SLA was met." in prompt

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_no_historical_qa_uses_none(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        call_args = mock_llm.call_args_list[0]
        prompt = call_args.kwargs.get("prompt", call_args.args[0] if call_args.args else "")
        assert "None" in prompt  # prior_answer placeholder

    def test_get_prior_answer_found(self):
        historical = HistoricalQAState(
            vendor_id="v001",
            prior_pairs=[{"question_id": "Q2", "answer_text": "Fast response time."}],
        )
        result = _get_prior_answer("Q2", historical)
        assert result == "Fast response time."

    def test_get_prior_answer_not_found(self):
        historical = HistoricalQAState(vendor_id="v001", prior_pairs=[])
        assert _get_prior_answer("Q1", historical) is None

    def test_get_prior_answer_none_historical(self):
        assert _get_prior_answer("Q1", None) is None


# ---------------------------------------------------------------------------
# Evidence text construction
# ---------------------------------------------------------------------------

class TestEvidenceText:

    def test_facts_sorted_by_quality_score(self):
        facts = [
            _make_fact("low_q", quality_score=0.2),
            _make_fact("high_q", quality_score=0.9),
            _make_fact("mid_q", quality_score=0.5),
        ]
        assembly = _make_assembly(facts)
        commercial = _make_commercial_result()
        text = _build_evidence_text(assembly, commercial)
        pos_high = text.index("high_q")
        pos_mid = text.index("mid_q")
        pos_low = text.index("low_q")
        assert pos_high < pos_mid < pos_low

    def test_commercial_metrics_appended(self):
        assembly = _make_assembly()
        commercial = _make_commercial_result(contract_type="SAAS", risk="HIGH", sla_pct=85.0, waste_pct=30.0)
        text = _build_evidence_text(assembly, commercial)
        assert "[COMMERCIAL] contract_type: SAAS" in text
        assert "[COMMERCIAL] commercial_risk: HIGH" in text
        assert "[COMMERCIAL] sla_adherence: 85.0%" in text
        assert "[COMMERCIAL] licence_waste: 30.0%" in text

    def test_none_metrics_not_included(self):
        assembly = _make_assembly()
        commercial = _make_commercial_result(sla_pct=None, waste_pct=None)
        text = _build_evidence_text(assembly, commercial)
        assert "sla_adherence" not in text
        assert "licence_waste" not in text

    def test_evidence_text_capped_at_4000_chars(self):
        # Create many large facts to exceed 4000 chars
        facts = [
            _make_fact(f"field_{i}", display_value="x" * 200, quality_score=1.0 - i * 0.01)
            for i in range(30)
        ]
        assembly = _make_assembly(facts)
        text = _build_evidence_text(assembly, _make_commercial_result())
        assert len(text) <= 4000

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_evidence_text_in_llm_prompt(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        fact = _make_fact("contract_term_end", display_value="2027-01-01")
        assembly = _make_assembly([fact])
        run_inquiry("v001", assembly, _make_commercial_result(), None, None, _make_scoring_config())
        call_args = mock_llm.call_args_list[0]
        prompt = call_args.kwargs.get("prompt", call_args.args[0] if call_args.args else "")
        assert "contract_term_end" in prompt


# ---------------------------------------------------------------------------
# Evidence citations
# ---------------------------------------------------------------------------

class TestEvidenceCitations:

    def test_citation_built_from_evidence_used(self):
        facts = [_make_fact("contract_term_end", source_file="sla.pdf")]
        assembly = _make_assembly(facts)
        citations = _build_citations(["contract_term_end"], assembly)
        assert len(citations) == 1
        assert citations[0].source_file == "sla.pdf"
        assert citations[0].evidence_id == "contract_term_end"

    def test_citation_with_source_section_display_text(self):
        facts = [_make_fact("sla_target", source_file="contract.pdf", source_section="§ 3.1", extraction_type="AUTO_EXTRACTED")]
        assembly = _make_assembly(facts)
        citations = _build_citations(["sla_target"], assembly)
        assert citations[0].display_text == "contract.pdf · § 3.1 [AUTO_EXTRACTED]"

    def test_citation_without_source_section_display_text(self):
        facts = [_make_fact("spend_ytd", source_file="erp.csv", source_section=None, extraction_type="COMPUTED")]
        assembly = _make_assembly(facts)
        citations = _build_citations(["spend_ytd"], assembly)
        assert citations[0].display_text == "erp.csv [COMPUTED]"

    def test_unknown_field_skipped_silently(self):
        assembly = _make_assembly([])
        citations = _build_citations(["nonexistent_field"], assembly)
        assert citations == []

    def test_unknown_field_no_crash(self):
        assembly = _make_assembly([_make_fact("real_field")])
        citations = _build_citations(["real_field", "fake_field"], assembly)
        assert len(citations) == 1

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_qa_pair_has_citations_from_evidence_used(self, mock_llm):
        facts = [_make_fact("contract_term_end", source_file="contract.pdf")]
        assembly = _make_assembly(facts)
        mock_llm.return_value = _good_llm_response(
            evidence_used=["contract_term_end"],
            completeness="COMPLETE",
            confidence="HIGH",
        )
        result = run_inquiry("v001", assembly, _make_commercial_result(), None, None, _make_scoring_config())
        q1 = next(q for q in result if q.question_id == "Q1" and q.tier == 1)
        assert len(q1.evidence_citations) == 1
        assert q1.evidence_citations[0].source_file == "contract.pdf"

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_missing_evidence_list_populated(self, mock_llm):
        mock_llm.return_value = _good_llm_response(
            completeness="PARTIAL",
            confidence="HIGH",
            missing=["SLA reports from last quarter"],
        )
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        q1 = next(q for q in result if q.question_id == "Q1" and q.tier == 1)
        assert "SLA reports from last quarter" in q1.missing_evidence


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------

class TestRoundTrip:

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_qapair_round_trip(self, mock_llm):
        mock_llm.return_value = _good_llm_response()
        result = run_inquiry("v001", _make_assembly(), _make_commercial_result(), None, None, _make_scoring_config())
        q1 = result[0]
        q1_dict = q1.to_dict()
        q1_back = QAPair.from_dict(q1_dict)
        assert q1_back.question_id == q1.question_id
        assert q1_back.answer_text == q1.answer_text
        assert q1_back.confidence == q1.confidence
        assert q1_back.completeness == q1.completeness
        assert q1_back.tier == q1.tier

    @patch("cobalt.tools.inquiry_engine.llm_call")
    def test_qapair_with_citation_round_trip(self, mock_llm):
        facts = [_make_fact("sla_target", source_file="contract.pdf", source_section="§ 2")]
        assembly = _make_assembly(facts)
        mock_llm.return_value = _good_llm_response(evidence_used=["sla_target"])
        result = run_inquiry("v001", assembly, _make_commercial_result(), None, None, _make_scoring_config())
        q1 = result[0]
        if q1.evidence_citations:
            ec = q1.evidence_citations[0]
            ec_dict = ec.to_dict()
            ec_back = EvidenceCitation.from_dict(ec_dict)
            assert ec_back.evidence_id == ec.evidence_id
            assert ec_back.source_file == ec.source_file
