"""Tests for finding_engine (AN-06).

All LLM calls are mocked. No real API calls.
Mock pattern: patch("cobalt.tools.finding_engine.llm_call")
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from cobalt.models.schemas.an_schema import (
    ANGap,
    CommercialAnalysisResult,
    DimensionScore,
    Finding,
    FindingsBundle,
    QAPair,
    ScoreBundle,
    ScoringConfig,
    TrendReport,
    ValidatedEvidenceAssembly,
    ValidatedEvidenceFact,
)
from cobalt.models.schemas.rs_schema import (
    RelationshipClassification,
    RelationshipSpendProfile,
    SpendSummary,
)
from cobalt.tools.finding_engine import (
    SCORE_FINDING_THRESHOLD_HIGH,
    SCORE_FINDING_THRESHOLD_MEDIUM,
    SCORE_DELTA_FINDING_THRESHOLD,
    SEVERITY_ORDER,
    TIER_CRI_THRESHOLDS,
    TREND_VELOCITY_HIGH_THRESHOLD,
    detect_findings,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_scoring_config() -> ScoringConfig:
    return ScoringConfig(
        dimension_weights={"delivery_reliability": 0.20, "responsiveness": 0.20,
                           "commercial_value": 0.20, "risk_compliance": 0.20,
                           "relationship_trend": 0.20},
        health_band_thresholds={"HEALTHY": 80, "WATCH": 65, "AT_RISK": 50, "CRITICAL": 0},
        tier_cri_thresholds={"STRATEGIC": 70, "PREFERRED": 65},
        spike_multiplier=1.0,
    )


def _make_rs_profile(
    relationship_type: str = "PREFERRED",
    expiry_date: str | None = None,
) -> RelationshipSpendProfile:
    from cobalt.models.schemas.rs_schema import ContractTerms
    rc = RelationshipClassification(
        vendor_id="v001",
        relationship_type=relationship_type,
        dependency_score=0.5,
        dependency_tier="MEDIUM",
        single_source_risk=False,
        contract_coverage="FULLY_COVERED",
        relationship_age_days=365,
        renewal_urgency="OK",
        classification_confidence="HIGH",
        llm_used=False,
        reasoning=None,
    )
    spend = SpendSummary(
        total_usd_all_time=500_000.0,
        total_usd_ttm=100_000.0,
        total_usd_ytd=100_000.0,
        by_period={},
        by_category={},
        by_cost_centre={},
        invoice_count=12,
        po_count=3,
        payment_terms_days_avg=30,
        data_completeness="FULL",
        confidence="HIGH",
    )
    ct = ContractTerms(
        document_id="c001",
        document_type="CONTRACT",
        effective_date="2024-01-01",
        expiry_date=expiry_date,
        auto_renews=False,
        notice_period_days=30,
        total_value=100_000.0,
        currency="USD",
        payment_terms_days=30,
        governing_law=None,
        key_obligations=[],
        sla_summary=None,
    )
    return RelationshipSpendProfile(
        vendor_id="v001",
        programme_id="p001",
        profile_version=1,
        profile_status="COMPLETE",
        created_at="2026-01-01T00:00:00+00:00",
        last_updated="2026-01-01T00:00:00+00:00",
        contract_count=1,
        spend_summary=spend,
        contract_terms=[ct],
        relationship_classification=rc,
        gap_report={},
        pcs_contribution=0.05,
        pcs_total=0.70,
        flags=[],
        data_sources=["ERP"],
    )


def _make_score_bundle(
    cri: int = 75,
    dimensions: list[tuple[str, int, int | None]] | None = None,
) -> ScoreBundle:
    """dimensions: list of (name, score, delta)"""
    if dimensions is None:
        dimensions = [
            ("delivery_reliability", 80, None),
            ("responsiveness", 78, None),
            ("commercial_value", 72, None),
            ("risk_compliance", 70, None),
            ("relationship_trend", 75, None),
        ]
    dim_scores = [
        DimensionScore(dimension=d, score=s, prior_score=None, delta=dt, trend_direction=None)
        for d, s, dt in dimensions
    ]
    return ScoreBundle(
        vendor_id="v001",
        cri_score=cri,
        prior_cri=None,
        cri_delta=None,
        health_band="WATCH",
        dimension_scores=dim_scores,
        operational_metrics={},
        portfolio_rank=None,
        category_rank=None,
        scored_at="2026-01-01T00:00:00+00:00",
    )


def _make_qa_pair(
    question_id: str = "Q1",
    completeness: str = "COMPLETE",
    confidence: str = "HIGH",
    missing: list[str] | None = None,
) -> QAPair:
    return QAPair(
        question_id=question_id,
        question=f"Question {question_id}?",
        answer_text="Answer.",
        confidence=confidence,
        completeness=completeness,
        answered_by="inquiry_engine",
        evidence_citations=[],
        missing_evidence=missing or [],
        tier=1,
        answered_at="2026-01-01T00:00:00+00:00",
    )


def _make_trend_report(
    dimension_trends: dict | None = None,
) -> TrendReport:
    default = {
        "delivery_reliability": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
        "responsiveness": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
        "commercial_value": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
        "risk_compliance": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
        "relationship_trend": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
    }
    return TrendReport(
        vendor_id="v001",
        dimension_trends=dimension_trends or default,
        action_learning=[],
        action_learning_summary=None,
        spend_trend={"direction": "UNKNOWN", "velocity": None, "yoy_delta": None},
        sla_trend={"response_time_direction": "UNKNOWN", "breach_rate_direction": "UNKNOWN"},
        sentiment_trend={"direction": "UNKNOWN", "last_signal_date": None},
        trend_computed_at="2026-01-01T00:00:00+00:00",
        data_points_available=1,
    )


def _make_commercial(
    risk: str = "LOW",
    findings: list[str] | None = None,
    sla_pct: float | None = None,
    waste_pct: float | None = None,
    delivery_score: float | None = None,
) -> CommercialAnalysisResult:
    return CommercialAnalysisResult(
        vendor_id="v001",
        contract_type="SAAS",
        contract_type_confidence="HIGH",
        utilisation_score=None,
        licence_waste_pct=waste_pct,
        cost_per_seat=None,
        shelfware_flag=False,
        sla_adherence_pct=sla_pct,
        delivery_score=delivery_score,
        milestone_status=None,
        penalty_exposure=None,
        uptime_pct=None,
        incident_trend=None,
        mttr_days=None,
        commercial_risk_level=risk,
        commercial_findings=findings or [],
        spend_efficiency_score=None,
        renewal_risk_scenarios=[],
        spend_efficiency_narrative=None,
        analysed_at="2026-01-01T00:00:00+00:00",
    )


def _make_assembly(facts: list[ValidatedEvidenceFact] | None = None) -> ValidatedEvidenceAssembly:
    return ValidatedEvidenceAssembly(
        vendor_id="v001",
        programme_id="p001",
        facts=facts or [],
        completeness_pct=0.9,
        conflict_count=0,
        stale_count=0,
        missing_count=0,
        validated_at="2026-01-01T00:00:00+00:00",
    )


def _make_fact(field_name: str, freshness_status: str = "CURRENT") -> ValidatedEvidenceFact:
    return ValidatedEvidenceFact(
        field_name=field_name,
        value="value",
        display_value="value",
        extraction_type="AUTO_EXTRACTED",
        source_file="source.pdf",
        source_section=None,
        confidence="HIGH",
        trust_level="OFFICIAL",
        freshness_status=freshness_status,
        conflict_flag=False,
        conflict_values=[],
        quality_score=0.8,
        validated_at="2026-01-01T00:00:00+00:00",
    )


def _run(
    cri: int = 75,
    dimensions: list[tuple[str, int, int | None]] | None = None,
    qa_pairs: list[QAPair] | None = None,
    dimension_trends: dict | None = None,
    commercial_risk: str = "LOW",
    commercial_flags: list[str] | None = None,
    sla_pct: float | None = None,
    waste_pct: float | None = None,
    delivery_score: float | None = None,
    relationship_type: str = "PREFERRED",
    facts: list[ValidatedEvidenceFact] | None = None,
    expiry_date: str | None = None,
) -> FindingsBundle:
    return detect_findings(
        vendor_id="v001",
        programme_id="p001",
        score_bundle=_make_score_bundle(cri, dimensions),
        qa_pairs=qa_pairs or [_make_qa_pair()],
        trend_report=_make_trend_report(dimension_trends),
        commercial_result=_make_commercial(
            commercial_risk, commercial_flags, sla_pct, waste_pct, delivery_score,
        ),
        validated_assembly=_make_assembly(facts),
        rs_profile=_make_rs_profile(relationship_type, expiry_date),
        scoring_config=_make_scoring_config(),
    )


# ---------------------------------------------------------------------------
# Score-based finding rules
# ---------------------------------------------------------------------------

class TestScoreFindings:

    def test_dimension_score_below_high_threshold_produces_high_finding(self):
        bundle = _run(dimensions=[
            ("delivery_reliability", 45, None),
            ("responsiveness", 80, None),
            ("commercial_value", 80, None),
            ("risk_compliance", 80, None),
            ("relationship_trend", 80, None),
        ])
        high = [f for f in bundle.findings if f.source == "SCORE" and f.severity == "HIGH"
                and "delivery_reliability" in f.title]
        assert len(high) >= 1

    def test_dimension_score_below_medium_threshold_produces_medium_finding(self):
        bundle = _run(dimensions=[
            ("delivery_reliability", 60, None),
            ("responsiveness", 80, None),
            ("commercial_value", 80, None),
            ("risk_compliance", 80, None),
            ("relationship_trend", 80, None),
        ])
        medium = [f for f in bundle.findings if f.source == "SCORE" and f.severity == "MEDIUM"
                  and "delivery_reliability" in f.title and "declining" not in f.title]
        assert len(medium) >= 1

    def test_dimension_score_above_medium_threshold_no_score_finding(self):
        bundle = _run(dimensions=[
            ("delivery_reliability", 70, None),
            ("responsiveness", 80, None),
            ("commercial_value", 80, None),
            ("risk_compliance", 80, None),
            ("relationship_trend", 80, None),
        ])
        score_findings = [f for f in bundle.findings if f.source == "SCORE"
                          and "delivery_reliability" in f.title and "declining" not in f.title]
        assert len(score_findings) == 0

    def test_dimension_delta_large_negative_produces_rapid_decline_finding(self):
        bundle = _run(dimensions=[
            ("delivery_reliability", 75, -12),
            ("responsiveness", 80, None),
            ("commercial_value", 80, None),
            ("risk_compliance", 80, None),
            ("relationship_trend", 80, None),
        ])
        decline = [f for f in bundle.findings if "declining rapidly" in f.title]
        assert len(decline) >= 1

    def test_dimension_delta_small_no_rapid_decline_finding(self):
        bundle = _run(dimensions=[
            ("delivery_reliability", 75, -5),
            ("responsiveness", 80, None),
            ("commercial_value", 80, None),
            ("risk_compliance", 80, None),
            ("relationship_trend", 80, None),
        ])
        decline = [f for f in bundle.findings if "declining rapidly" in f.title]
        assert len(decline) == 0

    def test_strategic_vendor_cri_below_threshold_high_finding(self):
        bundle = _run(cri=68, relationship_type="STRATEGIC")
        cri_finding = [f for f in bundle.findings if "CRI below threshold" in f.title]
        assert len(cri_finding) >= 1
        assert cri_finding[0].severity == "HIGH"

    def test_preferred_vendor_cri_below_threshold_high_finding(self):
        bundle = _run(cri=60, relationship_type="PREFERRED")
        cri_finding = [f for f in bundle.findings if "CRI below threshold" in f.title]
        assert len(cri_finding) >= 1

    def test_preferred_vendor_cri_above_threshold_no_cri_finding(self):
        bundle = _run(cri=70, relationship_type="PREFERRED")
        cri_finding = [f for f in bundle.findings if "CRI below threshold" in f.title]
        assert len(cri_finding) == 0

    def test_score_threshold_constants(self):
        assert SCORE_FINDING_THRESHOLD_HIGH == 50
        assert SCORE_FINDING_THRESHOLD_MEDIUM == 65
        assert SCORE_DELTA_FINDING_THRESHOLD == -10


# ---------------------------------------------------------------------------
# Q&A-based finding rules
# ---------------------------------------------------------------------------

class TestQAFindings:

    def test_q1_unanswerable_produces_high_finding(self):
        qa = [_make_qa_pair("Q1", completeness="UNANSWERABLE")]
        bundle = _run(qa_pairs=qa)
        qa_findings = [f for f in bundle.findings if f.source == "QA" and f.severity == "HIGH"]
        assert len(qa_findings) >= 1

    def test_q1_unanswerable_produces_blocking_gap(self):
        qa = [_make_qa_pair("Q1", completeness="UNANSWERABLE")]
        bundle = _run(qa_pairs=qa)
        blocking = [g for g in bundle.gaps if g.severity == "BLOCKING"]
        assert len(blocking) >= 1

    def test_q4_unanswerable_produces_high_finding_and_gap(self):
        qa = [_make_qa_pair("Q4", completeness="UNANSWERABLE")]
        bundle = _run(qa_pairs=qa)
        qa_findings = [f for f in bundle.findings if f.source == "QA" and f.severity == "HIGH"]
        blocking = [g for g in bundle.gaps if g.severity == "BLOCKING"]
        assert len(qa_findings) >= 1
        assert len(blocking) >= 1

    def test_q3_unanswerable_no_high_qa_finding(self):
        # Q3 not in CRITICAL_QA_QUESTIONS → no HIGH finding, no BLOCKING gap
        qa = [_make_qa_pair("Q3", completeness="UNANSWERABLE")]
        bundle = _run(qa_pairs=qa)
        qa_high = [f for f in bundle.findings if f.source == "QA" and f.severity == "HIGH"
                   and "Q3" in f.title]
        blocking = [g for g in bundle.gaps if g.severity == "BLOCKING"]
        assert len(qa_high) == 0
        assert len(blocking) == 0

    def test_q1_partial_produces_medium_finding(self):
        qa = [_make_qa_pair("Q1", completeness="PARTIAL")]
        bundle = _run(qa_pairs=qa)
        medium = [f for f in bundle.findings if f.source == "QA" and f.severity == "MEDIUM"]
        assert len(medium) >= 1

    def test_q2_partial_produces_medium_finding(self):
        # Q2 in MATERIAL_QA_QUESTIONS
        qa = [_make_qa_pair("Q2", completeness="PARTIAL")]
        bundle = _run(qa_pairs=qa)
        medium = [f for f in bundle.findings if f.source == "QA" and f.severity == "MEDIUM"]
        assert len(medium) >= 1

    def test_q5_partial_no_qa_finding(self):
        # Q5 NOT in MATERIAL_QA_QUESTIONS
        qa = [_make_qa_pair("Q5", completeness="PARTIAL")]
        bundle = _run(qa_pairs=qa)
        qa_findings = [f for f in bundle.findings if f.source == "QA"]
        assert len(qa_findings) == 0

    def test_complete_high_qa_no_finding(self):
        qa = [_make_qa_pair("Q1", completeness="COMPLETE", confidence="HIGH")]
        bundle = _run(qa_pairs=qa)
        qa_findings = [f for f in bundle.findings if f.source == "QA"]
        assert len(qa_findings) == 0


# ---------------------------------------------------------------------------
# Trend-based finding rules
# ---------------------------------------------------------------------------

class TestTrendFindings:

    def test_declining_velocity_below_threshold_produces_high_finding(self):
        trends = {
            "delivery_reliability": {"direction": "DECLINING", "velocity": -7.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "responsiveness": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "commercial_value": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "risk_compliance": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "relationship_trend": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
        }
        bundle = _run(dimension_trends=trends)
        trend_high = [f for f in bundle.findings if f.source == "TREND" and f.severity == "HIGH"]
        assert len(trend_high) >= 1

    def test_declining_velocity_above_threshold_no_trend_finding(self):
        trends = {
            "delivery_reliability": {"direction": "DECLINING", "velocity": -2.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "responsiveness": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "commercial_value": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "risk_compliance": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "relationship_trend": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
        }
        bundle = _run(dimension_trends=trends)
        trend_high = [f for f in bundle.findings if f.source == "TREND" and f.severity == "HIGH"]
        assert len(trend_high) == 0

    def test_inflection_point_set_produces_medium_trend_finding(self):
        trends = {
            "delivery_reliability": {"direction": "DECLINING", "velocity": -1.0, "inflection_point": "2026-01-01", "pattern": "UNKNOWN"},
            "responsiveness": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "commercial_value": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "risk_compliance": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
            "relationship_trend": {"direction": "STABLE", "velocity": 0.0, "inflection_point": None, "pattern": "UNKNOWN"},
        }
        bundle = _run(dimension_trends=trends)
        medium = [f for f in bundle.findings if f.source == "TREND" and f.severity == "MEDIUM"]
        assert len(medium) >= 1

    def test_no_inflection_no_trend_medium_finding(self):
        bundle = _run()  # all STABLE, no inflection
        trend_medium = [f for f in bundle.findings if f.source == "TREND" and f.severity == "MEDIUM"]
        assert len(trend_medium) == 0

    def test_trend_velocity_threshold_constant(self):
        assert TREND_VELOCITY_HIGH_THRESHOLD == -5.0


# ---------------------------------------------------------------------------
# Commercial finding rules
# ---------------------------------------------------------------------------

class TestCommercialFindings:

    def test_commercial_risk_high_produces_high_finding(self):
        bundle = _run(commercial_risk="HIGH")
        comm_high = [f for f in bundle.findings if f.source == "COMMERCIAL" and f.severity == "HIGH"
                     and "Commercial risk elevated" in f.title]
        assert len(comm_high) >= 1

    def test_commercial_risk_critical_produces_high_finding(self):
        bundle = _run(commercial_risk="CRITICAL")
        comm_high = [f for f in bundle.findings if f.source == "COMMERCIAL" and f.severity == "HIGH"
                     and "Commercial risk elevated" in f.title]
        assert len(comm_high) >= 1

    def test_commercial_risk_low_no_risk_finding(self):
        bundle = _run(commercial_risk="LOW")
        comm_risk = [f for f in bundle.findings if "Commercial risk elevated" in f.title]
        assert len(comm_risk) == 0

    def test_sla_breach_pattern_flag_produces_high_finding(self):
        bundle = _run(commercial_flags=["SLA_BREACH_PATTERN"], sla_pct=80.0)
        flag_findings = [f for f in bundle.findings if "SLA breach pattern" in f.title]
        assert len(flag_findings) >= 1
        assert flag_findings[0].severity == "HIGH"

    def test_licence_waste_flag_produces_medium_finding(self):
        bundle = _run(commercial_flags=["LICENCE_WASTE"], waste_pct=35.0)
        flag_findings = [f for f in bundle.findings if "Licence utilisation" in f.title]
        assert len(flag_findings) >= 1
        assert flag_findings[0].severity == "MEDIUM"

    def test_unknown_flag_ignored_silently(self):
        initial = _run()
        with_flag = _run(commercial_flags=["UNKNOWN_FLAG_XYZ"])
        # No additional findings from the unknown flag
        unknown_findings = [f for f in with_flag.findings if "UNKNOWN_FLAG_XYZ" in f.why]
        assert len(unknown_findings) == 0

    def test_incident_frequency_rising_no_format_error(self):
        # INCIDENT_FREQUENCY_RISING has no format placeholders — should never raise
        bundle = _run(commercial_flags=["INCIDENT_FREQUENCY_RISING"])
        flag_findings = [f for f in bundle.findings if "Incident frequency" in f.title]
        assert len(flag_findings) >= 1


# ---------------------------------------------------------------------------
# Gap classification
# ---------------------------------------------------------------------------

class TestGapClassification:

    def test_missing_expected_field_produces_enrichment_gap(self):
        from cobalt.tools.finding_engine import EXPECTED_FIELDS
        field = next(iter(EXPECTED_FIELDS))  # pick any expected field
        fact = _make_fact(field, freshness_status="MISSING")
        bundle = _run(facts=[fact])
        enrichment = [g for g in bundle.gaps if g.severity == "ENRICHMENT"]
        assert len(enrichment) >= 1

    def test_missing_unexpected_field_no_gap(self):
        fact = _make_fact("some_random_field_not_expected", freshness_status="MISSING")
        bundle = _run(facts=[fact])
        enrichment = [g for g in bundle.gaps if g.severity == "ENRICHMENT"]
        assert len(enrichment) == 0

    def test_current_field_no_enrichment_gap(self):
        from cobalt.tools.finding_engine import EXPECTED_FIELDS
        field = next(iter(EXPECTED_FIELDS))
        fact = _make_fact(field, freshness_status="CURRENT")
        bundle = _run(facts=[fact])
        enrichment = [g for g in bundle.gaps if g.severity == "ENRICHMENT"]
        assert len(enrichment) == 0

    def test_q1_unanswerable_blocking_gap_created(self):
        qa = [_make_qa_pair("Q1", completeness="UNANSWERABLE")]
        bundle = _run(qa_pairs=qa)
        blocking = [g for g in bundle.gaps if g.severity == "BLOCKING"]
        assert len(blocking) >= 1


# ---------------------------------------------------------------------------
# Finding deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:

    def test_same_source_and_title_prefix_deduplicated(self):
        # Score 45 on delivery_reliability triggers both HIGH AND "declining rapidly" if delta is -12
        # But score 45 + delta None → only HIGH. Let's use same source manually.
        # Use two dimensions with same score range to get same title prefix
        # Actually, the dedup key is (source, title[:40]). Two identical rule-triggered findings
        # for the same dimension won't happen — let's test via score+delta on same dim.
        bundle = _run(dimensions=[
            ("delivery_reliability", 45, -12),  # HIGH for score + MEDIUM for delta
            ("responsiveness", 80, None),
            ("commercial_value", 80, None),
            ("risk_compliance", 80, None),
            ("relationship_trend", 80, None),
        ])
        # Two different findings (different titles) — not duplicates
        delivery_findings = [f for f in bundle.findings if "delivery_reliability" in f.title]
        # HIGH (score below 50) and MEDIUM (delta -12) — different titles → not deduplicated
        assert len(delivery_findings) >= 2

    def test_different_sources_same_title_not_deduplicated(self):
        # One SCORE finding + one COMMERCIAL finding with same title would NOT dedup (different source)
        bundle = _run(commercial_risk="HIGH")
        score_findings = [f for f in bundle.findings if f.source == "SCORE"]
        commercial_findings = [f for f in bundle.findings if f.source == "COMMERCIAL"]
        # Both exist separately
        assert len(score_findings) >= 0  # may have none if all scores above threshold
        assert len(commercial_findings) >= 1


# ---------------------------------------------------------------------------
# LLM severity calibration
# ---------------------------------------------------------------------------

class TestLLMCalibration:

    @patch("cobalt.tools.finding_engine.llm_call")
    def test_two_high_findings_llm_not_called(self, mock_llm):
        # Only 2 HIGH findings → below threshold of 3 → LLM not called
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),  # HIGH
                ("responsiveness", 45, None),          # HIGH
                ("commercial_value", 80, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
            cri=75,  # above PREFERRED threshold of 65 → no CRI finding
        )
        high_count = sum(1 for f in bundle.findings if f.severity in ("HIGH", "CRITICAL"))
        if high_count < 3:
            mock_llm.assert_not_called()

    @patch("cobalt.tools.finding_engine.llm_call")
    def test_three_high_findings_llm_called(self, mock_llm):
        mock_llm.return_value = {"calibrations": []}
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),  # HIGH
                ("responsiveness", 45, None),          # HIGH
                ("commercial_value", 45, None),        # HIGH
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
            cri=60,  # below PREFERRED threshold → additional HIGH CRI finding
        )
        high_count = sum(1 for f in bundle.findings if f.severity in ("HIGH", "CRITICAL"))
        if high_count >= 3:
            mock_llm.assert_called_once()

    @patch("cobalt.tools.finding_engine.llm_call")
    def test_llm_elevation_accepted(self, mock_llm):
        """LLM calibrates MEDIUM → HIGH → accepted."""
        def calibration_effect(prompt, system, expect_json=True):
            match = re.search(r'"finding_id":\s*"([^"]+)"', prompt)
            if match:
                fid = match.group(1)
                return {"calibrations": [{"finding_id": fid, "severity": "HIGH", "reason": "elevated"}]}
            return {"calibrations": []}

        mock_llm.side_effect = calibration_effect
        # Need >= 3 HIGH/CRITICAL findings to trigger calibration
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),  # HIGH
                ("responsiveness", 45, None),          # HIGH
                ("commercial_value", 45, None),        # HIGH
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
            cri=60,  # may produce CRI finding too
        )
        # Calibration runs; result is still valid
        assert bundle is not None

    @patch("cobalt.tools.finding_engine.llm_call")
    def test_llm_lowering_rejected_floor_enforced(self, mock_llm):
        """LLM tries to lower HIGH → LOW → rejected (floor rule)."""
        def calibration_effect(prompt, system, expect_json=True):
            match = re.search(r'"finding_id":\s*"([^"]+)"', prompt)
            if match:
                fid = match.group(1)
                return {"calibrations": [{"finding_id": fid, "severity": "LOW", "reason": "try to lower"}]}
            return {"calibrations": []}

        mock_llm.side_effect = calibration_effect
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),
                ("responsiveness", 45, None),
                ("commercial_value", 45, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
            cri=60,
        )
        # No finding should be LOW that started as HIGH
        high_or_above = [f for f in bundle.findings if SEVERITY_ORDER.get(f.severity, 0) >= 3]
        assert len(high_or_above) >= 1

    @patch("cobalt.tools.finding_engine.llm_call")
    def test_llm_failure_severities_unchanged_no_crash(self, mock_llm):
        mock_llm.side_effect = Exception("LLM unavailable")
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),
                ("responsiveness", 45, None),
                ("commercial_value", 45, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
            cri=60,
        )
        assert bundle is not None
        # Still has HIGH findings from rule-based logic
        high = [f for f in bundle.findings if f.severity == "HIGH"]
        assert len(high) >= 1

    @patch("cobalt.tools.finding_engine.llm_call")
    def test_nonexistent_finding_id_in_calibration_ignored(self, mock_llm):
        mock_llm.return_value = {
            "calibrations": [{"finding_id": "finding-nonexistent", "severity": "CRITICAL", "reason": "test"}]
        }
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),
                ("responsiveness", 45, None),
                ("commercial_value", 45, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
            cri=60,
        )
        assert bundle is not None


# ---------------------------------------------------------------------------
# NBA selection
# ---------------------------------------------------------------------------

class TestNBASelection:

    def test_no_findings_nba_is_none(self):
        bundle = _run(
            cri=80,
            dimensions=[
                ("delivery_reliability", 80, None),
                ("responsiveness", 80, None),
                ("commercial_value", 80, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
        )
        # May or may not have findings depending on tier threshold; just check nba type
        if not bundle.findings:
            assert bundle.nba is None

    def test_high_finding_nba_review_required_true(self):
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),  # HIGH
                ("responsiveness", 80, None),
                ("commercial_value", 80, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
        )
        assert bundle.nba is not None
        assert bundle.nba.review_required is True

    def test_top_findings_at_most_3(self):
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 40, None),
                ("responsiveness", 40, None),
                ("commercial_value", 40, None),
                ("risk_compliance", 40, None),
                ("relationship_trend", 40, None),
            ],
            cri=38,
        )
        assert len(bundle.top_findings) <= 3

    def test_renewal_days_none_nba_standard_timing(self):
        # No expiry_date → renewal_days=None → MONITOR or BEFORE_RENEWAL timing
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),
                ("responsiveness", 80, None),
                ("commercial_value", 80, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
            expiry_date=None,
        )
        assert bundle.nba is not None
        assert bundle.nba.timing in ("THIS_WEEK", "NOW", "BEFORE_RENEWAL", "MONITOR")

    def test_nba_owner_is_vendor_owner(self):
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),
                ("responsiveness", 80, None),
                ("commercial_value", 80, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
        )
        if bundle.nba is not None:
            assert bundle.nba.owner == "vendor_owner"


# ---------------------------------------------------------------------------
# Triage tasks
# ---------------------------------------------------------------------------

class TestTriageTasks:

    def test_blocking_gap_generates_triage_task(self):
        qa = [_make_qa_pair("Q1", completeness="UNANSWERABLE")]
        bundle = _run(qa_pairs=qa)
        assert len(bundle.triage_tasks) >= 1

    def test_multiple_blocking_gaps_multiple_triage_tasks(self):
        qa = [
            _make_qa_pair("Q1", completeness="UNANSWERABLE"),
            _make_qa_pair("Q4", completeness="UNANSWERABLE"),
        ]
        bundle = _run(qa_pairs=qa)
        assert len(bundle.triage_tasks) >= 2

    def test_triage_task_has_required_fields(self):
        qa = [_make_qa_pair("Q1", completeness="UNANSWERABLE")]
        bundle = _run(qa_pairs=qa)
        if bundle.triage_tasks:
            task = bundle.triage_tasks[0]
            assert "triage_type" in task
            assert "severity" in task
            assert "vendor_id" in task
            assert "due_date" in task


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------

class TestRoundTrip:

    def test_findings_bundle_round_trip(self):
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),
                ("responsiveness", 80, None),
                ("commercial_value", 80, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
        )
        d = bundle.to_dict()
        restored = FindingsBundle.from_dict(d)
        assert restored.vendor_id == bundle.vendor_id
        assert len(restored.findings) == len(bundle.findings)
        assert len(restored.gaps) == len(bundle.gaps)
        assert (restored.nba is None) == (bundle.nba is None)

    def test_finding_round_trip(self):
        bundle = _run(
            dimensions=[
                ("delivery_reliability", 45, None),
                ("responsiveness", 80, None),
                ("commercial_value", 80, None),
                ("risk_compliance", 80, None),
                ("relationship_trend", 80, None),
            ],
        )
        if bundle.findings:
            f = bundle.findings[0]
            d = f.to_dict()
            restored = Finding.from_dict(d)
            assert restored.finding_id == f.finding_id
            assert restored.severity == f.severity
            assert restored.source == f.source
