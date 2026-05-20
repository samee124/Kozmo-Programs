"""Tests for narrative_engine (AN-07).

All LLM calls are mocked. No real API calls.
Mock pattern: patch("cobalt.tools.narrative_engine.llm_call")
"""

from __future__ import annotations

from unittest.mock import call, patch

import pytest

from cobalt.models.schemas.an_schema import (
    ANGap,
    CommercialAnalysisResult,
    DimensionScore,
    EvidenceCitation,
    Finding,
    FindingsBundle,
    FindingNarrative,
    NBA,
    NarrativeBundle,
    QAPair,
    QASummary,
    ScoreBundle,
    ValidatedEvidenceAssembly,
    ValidatedEvidenceFact,
)
from cobalt.tools.narrative_engine import (
    REDACTION_PATTERNS,
    _check_redaction,
    _format_citations,
    generate_narratives,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_score_bundle(
    cri: int = 72,
    health_band: str = "WATCH",
    cri_delta: int | None = None,
) -> ScoreBundle:
    dims = [
        DimensionScore("delivery_reliability", 75, None, None, None),
        DimensionScore("responsiveness", 70, None, None, None),
        DimensionScore("commercial_value", 68, None, None, None),
        DimensionScore("risk_compliance", 72, None, None, None),
        DimensionScore("relationship_trend", 75, None, None, None),
    ]
    return ScoreBundle(
        vendor_id="v001",
        cri_score=cri,
        prior_cri=None,
        cri_delta=cri_delta,
        health_band=health_band,
        dimension_scores=dims,
        operational_metrics={},
        portfolio_rank=None,
        category_rank=None,
        scored_at="2026-01-01T00:00:00+00:00",
    )


def _make_commercial(
    contract_type: str = "SAAS",
    risk: str = "LOW",
) -> CommercialAnalysisResult:
    return CommercialAnalysisResult(
        vendor_id="v001",
        contract_type=contract_type,
        contract_type_confidence="HIGH",
        utilisation_score=0.85,
        licence_waste_pct=10.0,
        cost_per_seat=None,
        shelfware_flag=False,
        sla_adherence_pct=None,
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


def _make_finding(
    finding_id: str = "finding-001",
    title: str = "Test finding",
    severity: str = "MEDIUM",
    source: str = "SCORE",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        title=title,
        severity=severity,
        why="Because of something",
        evidence_ids=[],
        source=source,
        status="OPEN",
        created_at="2026-01-01T00:00:00+00:00",
    )


def _make_findings_bundle(findings: list[Finding] | None = None) -> FindingsBundle:
    f_list = findings or []
    return FindingsBundle(
        vendor_id="v001",
        findings=f_list,
        gaps=[],
        nba=None,
        top_findings=f_list[:3],
        triage_tasks=[],
        generated_at="2026-01-01T00:00:00+00:00",
    )


def _make_qa(
    question_id: str = "Q1",
    answer: str = "All SLAs met.",
    confidence: str = "HIGH",
    completeness: str = "COMPLETE",
    citations: list[EvidenceCitation] | None = None,
) -> QAPair:
    return QAPair(
        question_id=question_id,
        question=f"Question {question_id}?",
        answer_text=answer,
        confidence=confidence,
        completeness=completeness,
        answered_by="inquiry_engine",
        evidence_citations=citations or [],
        missing_evidence=[],
        tier=1,
        answered_at="2026-01-01T00:00:00+00:00",
    )


def _make_citation(
    evidence_id: str = "sla_target",
    source_file: str = "contract.pdf",
    source_section: str | None = None,
    extraction_type: str = "AUTO_EXTRACTED",
) -> EvidenceCitation:
    if source_section:
        display_text = f"{source_file} · {source_section} [{extraction_type}]"
    else:
        display_text = f"{source_file} [{extraction_type}]"
    return EvidenceCitation(
        evidence_id=evidence_id,
        source_file=source_file,
        source_section=source_section,
        extraction_type=extraction_type,
        quality_score=0.85,
        display_text=display_text,
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


def _make_fact(field_name: str, source_file: str = "evidence.pdf") -> ValidatedEvidenceFact:
    return ValidatedEvidenceFact(
        field_name=field_name,
        value="value",
        display_value="value",
        extraction_type="AUTO_EXTRACTED",
        source_file=source_file,
        source_section=None,
        confidence="HIGH",
        trust_level="OFFICIAL",
        freshness_status="CURRENT",
        conflict_flag=False,
        conflict_values=[],
        quality_score=0.8,
        validated_at="2026-01-01T00:00:00+00:00",
    )


_VENDOR_FILE = {"name": "Acme Corp", "expiry_date": None}

_GOOD_CALL1 = {
    "vendor_summary": "Acme Corp is performing adequately with some areas for improvement.",
    "finding_narratives": {},
}
_GOOD_CALL2 = {
    "commercial_summary": "Commercial performance is broadly on track.",
    "qa_summaries": {"Q1": "SLA performance is satisfactory."},
}


# ---------------------------------------------------------------------------
# LLM Call 1 — finding narratives + vendor summary
# ---------------------------------------------------------------------------

class TestLLMCall1:

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_valid_response_populates_vendor_summary(self, mock_llm):
        mock_llm.return_value = _GOOD_CALL1
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert result.vendor_summary == _GOOD_CALL1["vendor_summary"]

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_valid_response_populates_finding_narratives(self, mock_llm):
        finding = _make_finding("f001", "Test finding")
        mock_llm.return_value = {
            "vendor_summary": "Acme Corp summary.",
            "finding_narratives": {"f001": "This finding needs attention."},
        }
        result = generate_narratives(
            "v001", _make_findings_bundle([finding]), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        f_narrative = next((fn for fn in result.finding_narratives if fn.finding_id == "f001"), None)
        assert f_narrative is not None
        assert f_narrative.narrative_text == "This finding needs attention."

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_llm_call1_failure_fallback_vendor_summary(self, mock_llm):
        mock_llm.side_effect = Exception("LLM unavailable")
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert "Acme Corp" in result.vendor_summary
        assert result is not None

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_llm_call1_failure_finding_narratives_empty(self, mock_llm):
        mock_llm.side_effect = Exception("LLM unavailable")
        finding = _make_finding("f001")
        result = generate_narratives(
            "v001", _make_findings_bundle([finding]), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        # Finding narrative exists but has empty text
        f_narratives = [fn for fn in result.finding_narratives if fn.finding_id == "f001"]
        assert f_narratives[0].narrative_text == ""

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_llm_call1_failure_no_raise(self, mock_llm):
        mock_llm.side_effect = Exception("LLM unavailable")
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert isinstance(result, NarrativeBundle)

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_empty_findings_bundle_no_crash(self, mock_llm):
        mock_llm.return_value = {"vendor_summary": "All good.", "finding_narratives": {}}
        result = generate_narratives(
            "v001", _make_findings_bundle([]), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert result.vendor_summary == "All good."
        assert result.finding_narratives == []

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_llm_call1_invalid_json_fallback(self, mock_llm):
        from cobalt.core.exceptions import LLMCallFailure
        mock_llm.side_effect = LLMCallFailure("Invalid JSON")
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert result is not None
        assert result.vendor_summary != ""

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_llm_call1_non_dict_fallback(self, mock_llm):
        mock_llm.return_value = "not a dict"
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert result is not None
        assert result.vendor_summary != ""


# ---------------------------------------------------------------------------
# LLM Call 2 — commercial + Q&A summaries (conditional)
# ---------------------------------------------------------------------------

class TestLLMCall2:

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_call2_runs_for_saas_with_qa_pairs(self, mock_llm):
        mock_llm.side_effect = [_GOOD_CALL1, _GOOD_CALL2]
        qa = [_make_qa("Q1")]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("SAAS"), qa, _make_assembly(), _VENDOR_FILE,
        )
        assert mock_llm.call_count == 2
        assert result.commercial_summary == _GOOD_CALL2["commercial_summary"]

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_call2_skipped_for_unknown_contract_type(self, mock_llm):
        mock_llm.return_value = _GOOD_CALL1
        qa = [_make_qa("Q1")]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("UNKNOWN"), qa, _make_assembly(), _VENDOR_FILE,
        )
        assert mock_llm.call_count == 1  # only Call 1
        assert result.commercial_summary is None

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_call2_skipped_when_no_qa_pairs(self, mock_llm):
        mock_llm.return_value = _GOOD_CALL1
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("SAAS"), [], _make_assembly(), _VENDOR_FILE,
        )
        assert mock_llm.call_count == 1  # only Call 1
        assert result.commercial_summary is None

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_call2_failure_commercial_summary_none(self, mock_llm):
        mock_llm.side_effect = [_GOOD_CALL1, Exception("LLM Call 2 fail")]
        qa = [_make_qa("Q1")]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("SAAS"), qa, _make_assembly(), _VENDOR_FILE,
        )
        assert result.commercial_summary is None

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_call2_failure_qa_summaries_empty(self, mock_llm):
        mock_llm.side_effect = [_GOOD_CALL1, Exception("LLM Call 2 fail")]
        qa = [_make_qa("Q1")]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("SAAS"), qa, _make_assembly(), _VENDOR_FILE,
        )
        assert result.qa_summaries == []

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_call2_failure_no_raise(self, mock_llm):
        mock_llm.side_effect = [_GOOD_CALL1, Exception("LLM Call 2 fail")]
        qa = [_make_qa("Q1")]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("SAAS"), qa, _make_assembly(), _VENDOR_FILE,
        )
        assert isinstance(result, NarrativeBundle)

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_call2_qa_summaries_populated(self, mock_llm):
        mock_llm.side_effect = [
            _GOOD_CALL1,
            {"commercial_summary": "Summary here.", "qa_summaries": {"Q1": "Q1 summary."}},
        ]
        qa = [_make_qa("Q1")]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("SAAS"), qa, _make_assembly(), _VENDOR_FILE,
        )
        assert len(result.qa_summaries) == 1
        assert result.qa_summaries[0].question_id == "Q1"
        assert result.qa_summaries[0].prose_summary == "Q1 summary."

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_both_calls_fail_still_returns_bundle(self, mock_llm):
        mock_llm.side_effect = Exception("Both fail")
        qa = [_make_qa("Q1")]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("SAAS"), qa, _make_assembly(), _VENDOR_FILE,
        )
        assert isinstance(result, NarrativeBundle)
        assert result.commercial_summary is None
        assert result.qa_summaries == []


# ---------------------------------------------------------------------------
# Redaction check
# ---------------------------------------------------------------------------

class TestRedactionCheck:

    def test_critical_word_triggers_redaction(self):
        assert _check_redaction("This vendor is CRITICAL for operations.") is True

    def test_sla_breach_pattern_triggers_redaction(self):
        assert _check_redaction("Vendor has SLA_BREACH_PATTERN issues.") is True

    def test_at_risk_triggers_redaction(self):
        assert _check_redaction("Vendor is AT_RISK of failing.") is True

    def test_cri_score_triggers_redaction(self):
        assert _check_redaction("The CRI 65 is below target.") is True

    def test_score_fraction_triggers_redaction(self):
        assert _check_redaction("Score is 45/100 which is poor.") is True

    def test_clean_narrative_no_redaction(self):
        assert _check_redaction("Vendor is performing well in all areas.") is False

    def test_high_finding_phrase_triggers_redaction(self):
        assert _check_redaction("This is a HIGH finding requiring action.") is True

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_flagged_narrative_in_redaction_flags(self, mock_llm):
        finding = _make_finding("f001", "CRITICAL SLA issue")
        mock_llm.return_value = {
            "vendor_summary": "Vendor is doing well.",
            "finding_narratives": {"f001": "This vendor has a CRITICAL performance gap."},
        }
        result = generate_narratives(
            "v001", _make_findings_bundle([finding]), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert "f001" in result.redaction_flags
        f_narrative = next(fn for fn in result.finding_narratives if fn.finding_id == "f001")
        assert f_narrative.redaction_flag is True

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_clean_narrative_not_in_redaction_flags(self, mock_llm):
        finding = _make_finding("f002", "Responsiveness gap")
        mock_llm.return_value = {
            "vendor_summary": "Vendor is doing well.",
            "finding_narratives": {"f002": "Vendor response times could be improved."},
        }
        result = generate_narratives(
            "v001", _make_findings_bundle([finding]), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert "f002" not in result.redaction_flags
        f_narrative = next(fn for fn in result.finding_narratives if fn.finding_id == "f002")
        assert f_narrative.redaction_flag is False

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_licence_waste_label_triggers_redaction(self, mock_llm):
        finding = _make_finding("f003")
        mock_llm.return_value = {
            "vendor_summary": "Summary.",
            "finding_narratives": {"f003": "LICENCE_WASTE has been detected."},
        }
        result = generate_narratives(
            "v001", _make_findings_bundle([finding]), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert "f003" in result.redaction_flags


# ---------------------------------------------------------------------------
# Evidence citations
# ---------------------------------------------------------------------------

class TestEvidenceCitations:

    def test_citation_with_source_section_display_text(self):
        citation = _make_citation(
            source_file="contract.pdf",
            source_section="§ 3.1",
            extraction_type="AUTO_EXTRACTED",
        )
        assert citation.display_text == "contract.pdf · § 3.1 [AUTO_EXTRACTED]"

    def test_citation_without_source_section_display_text(self):
        citation = _make_citation(
            source_file="erp.csv",
            source_section=None,
            extraction_type="COMPUTED",
        )
        assert citation.display_text == "erp.csv [COMPUTED]"

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_format_citations_collects_display_texts(self, mock_llm):
        mock_llm.return_value = _GOOD_CALL1
        citation = _make_citation("sla_target", "contract.pdf", "§ 3.1", "AUTO_EXTRACTED")
        qa = [_make_qa("Q1", citations=[citation])]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial(), qa, _make_assembly(), _VENDOR_FILE,
        )
        assert "contract.pdf · § 3.1 [AUTO_EXTRACTED]" in result.evidence_citations

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_empty_qa_pairs_empty_citations(self, mock_llm):
        mock_llm.return_value = _GOOD_CALL1
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        assert result.evidence_citations == []

    def test_format_citations_helper(self):
        c1 = _make_citation("f1", "a.pdf", "§ 1", "AUTO_EXTRACTED")
        c2 = _make_citation("f2", "b.csv", None, "COMPUTED")
        qa1 = _make_qa("Q1", citations=[c1])
        qa2 = _make_qa("Q2", citations=[c2])
        texts = _format_citations([qa1, qa2])
        assert "a.pdf · § 1 [AUTO_EXTRACTED]" in texts
        assert "b.csv [COMPUTED]" in texts


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------

class TestRoundTrip:

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_narrative_bundle_round_trip(self, mock_llm):
        mock_llm.return_value = _GOOD_CALL1
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        d = result.to_dict()
        restored = NarrativeBundle.from_dict(d)
        assert restored.vendor_id == result.vendor_id
        assert restored.vendor_summary == result.vendor_summary
        assert len(restored.finding_narratives) == len(result.finding_narratives)
        assert restored.redaction_flags == result.redaction_flags

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_finding_narrative_round_trip(self, mock_llm):
        finding = _make_finding("f001")
        mock_llm.return_value = {
            "vendor_summary": "Summary.",
            "finding_narratives": {"f001": "Narrative text here."},
        }
        result = generate_narratives(
            "v001", _make_findings_bundle([finding]), _make_score_bundle(),
            _make_commercial(), [], _make_assembly(), _VENDOR_FILE,
        )
        if result.finding_narratives:
            fn = result.finding_narratives[0]
            d = fn.to_dict()
            restored = FindingNarrative.from_dict(d)
            assert restored.finding_id == fn.finding_id
            assert restored.redaction_flag == fn.redaction_flag

    @patch("cobalt.tools.narrative_engine.llm_call")
    def test_qa_summary_round_trip(self, mock_llm):
        mock_llm.side_effect = [
            _GOOD_CALL1,
            {"commercial_summary": "Summary.", "qa_summaries": {"Q1": "Q1 prose."}},
        ]
        qa = [_make_qa("Q1")]
        result = generate_narratives(
            "v001", _make_findings_bundle(), _make_score_bundle(),
            _make_commercial("SAAS"), qa, _make_assembly(), _VENDOR_FILE,
        )
        if result.qa_summaries:
            qs = result.qa_summaries[0]
            d = qs.to_dict()
            restored = QASummary.from_dict(d)
            assert restored.question_id == qs.question_id
            assert restored.prose_summary == qs.prose_summary
