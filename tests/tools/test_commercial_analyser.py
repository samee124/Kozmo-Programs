"""Tests for commercial_analyser — Tool 3 P4."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cobalt.models.schemas.an_schema import (
    CommercialAnalysisResult,
    HistoricalCommercialState,
    ScoringConfig,
    ValidatedEvidenceAssembly,
    ValidatedEvidenceFact,
)
from cobalt.models.schemas.rs_schema import (
    ContractTerms,
    GapReport,
    RelationshipClassification,
    RelationshipSpendProfile,
    SpendSummary,
    StructuredDataBundle,
)
from cobalt.tools.commercial_analyser import (
    _analyse_managed,
    _analyse_saas,
    _analyse_services,
    _compute_risk_level,
    _compute_spend_efficiency,
    _detect_contract_type_keywords,
    analyse_commercial,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

VENDOR_ID = "v-comm-001"
PROG_ID = "prog-comm-001"


def _scoring_config() -> ScoringConfig:
    return ScoringConfig(
        dimension_weights={},
        health_band_thresholds={},
        tier_cri_thresholds={},
        spike_multiplier=1.0,
    )


def _spend_summary(total_usd: float = 100_000.0) -> SpendSummary:
    return SpendSummary(
        total_usd_all_time=total_usd,
        total_usd_ttm=total_usd,
        total_usd_ytd=total_usd,
        by_period={},
        by_category={},
        by_cost_centre={},
        invoice_count=12,
        po_count=12,
        payment_terms_days_avg=30,
        data_completeness="FULL",
        confidence="HIGH",
    )


def _rel_class() -> RelationshipClassification:
    return RelationshipClassification(
        vendor_id=VENDOR_ID,
        relationship_type="STRATEGIC",
        dependency_score=0.8,
        dependency_tier="HIGH",
        single_source_risk=False,
        contract_coverage="FULLY_COVERED",
        relationship_age_days=365,
        renewal_urgency="OK",
        classification_confidence="HIGH",
        llm_used=False,
        reasoning=None,
    )


def _gap_report() -> GapReport:
    return GapReport(
        missing_fields=[],
        low_confidence_fields=[],
        stale_fields=[],
        gap_severity="NONE",
        recommended_actions=[],
    )


def _make_contract(
    doc_id: str = "c001",
    document_type: str = "CONTRACT",
    key_obligations: list[str] | None = None,
    sla_summary: str | None = None,
    total_value: float | None = 100_000.0,
    expiry_date: str | None = None,  # None by default — suppresses renewal LLM call
    auto_renews: bool | None = True,
    notice_period_days: int | None = 30,
) -> ContractTerms:
    return ContractTerms(
        document_id=doc_id,
        document_type=document_type,
        effective_date="2024-01-01",
        expiry_date=expiry_date,
        auto_renews=auto_renews,
        notice_period_days=notice_period_days,
        total_value=total_value,
        currency="USD",
        payment_terms_days=30,
        governing_law="England",
        key_obligations=key_obligations or [],
        sla_summary=sla_summary,
    )


def _make_profile(
    contracts: list[ContractTerms] | None = None,
    spend: float = 100_000.0,
    flags: list[str] | None = None,
) -> RelationshipSpendProfile:
    return RelationshipSpendProfile(
        vendor_id=VENDOR_ID,
        programme_id=PROG_ID,
        profile_version=1,
        profile_status="COMPLETE",
        created_at="2024-01-01T00:00:00+00:00",
        last_updated="2024-06-01T00:00:00+00:00",
        contract_count=len(contracts or []),
        spend_summary=_spend_summary(spend),
        contract_terms=contracts or [],
        relationship_classification=_rel_class(),
        gap_report=_gap_report().to_dict(),
        pcs_contribution=0.05,
        pcs_total=0.05,
        flags=flags or [],
        data_sources=["erp"],
    )


def _empty_assembly() -> ValidatedEvidenceAssembly:
    return ValidatedEvidenceAssembly(
        vendor_id=VENDOR_ID,
        programme_id=PROG_ID,
        facts=[],
        completeness_pct=0.0,
        conflict_count=0,
        stale_count=0,
        missing_count=11,
        validated_at="2024-06-01T00:00:00+00:00",
    )


def _make_bundle(**meta_kwargs) -> StructuredDataBundle:
    return StructuredDataBundle(
        vendor_id=VENDOR_ID,
        programme_id=PROG_ID,
        collected_at="2024-06-01T00:00:00+00:00",
        arrival_modes_used=["CONNECTOR"],
        raw_spend_records=[],
        connector_metadata=meta_kwargs,
        upload_metadata={},
        checkin_metadata={},
        collection_warnings=[],
    )


# ---------------------------------------------------------------------------
# _detect_contract_type_keywords
# ---------------------------------------------------------------------------

class TestDetectContractTypeKeywords:
    def test_saas_keywords_only(self):
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "named user", "saas", "licence",
                              "per user", "software as a service"],
        )
        profile = _make_profile([ct])
        ctype, conf = _detect_contract_type_keywords(profile)
        assert ctype == "SAAS"
        assert conf >= 0.70

    def test_services_keywords_only(self):
        ct = _make_contract(
            key_obligations=["statement of work", "milestone", "deliverable",
                              "professional services", "time and materials", "sow"],
        )
        profile = _make_profile([ct])
        ctype, conf = _detect_contract_type_keywords(profile)
        assert ctype == "SERVICES"
        assert conf >= 0.70

    def test_managed_keywords_only(self):
        ct = _make_contract(
            sla_summary="uptime 99.9%, incident response within 4h, managed service, "
                        "sla response, service desk 24x7",
        )
        profile = _make_profile([ct])
        ctype, conf = _detect_contract_type_keywords(profile)
        assert ctype == "MANAGED_SERVICES"
        assert conf >= 0.70

    def test_mixed_saas_and_services(self):
        # Many SAAS signals + some SERVICES: still MIXED but confidence >= 0.70
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user", "licence",
                              "per user", "software as a service",  # 7 SAAS
                              "sow"],                                 # 1 SERVICES
        )
        profile = _make_profile([ct])
        ctype, conf = _detect_contract_type_keywords(profile)
        assert ctype == "MIXED"
        assert conf == pytest.approx(7 / 8)

    def test_no_keywords_returns_unknown_confidence_zero(self):
        ct = _make_contract(key_obligations=["deliver quarterly report"])
        profile = _make_profile([ct])
        ctype, conf = _detect_contract_type_keywords(profile)
        assert ctype == "UNKNOWN"
        assert conf == pytest.approx(0.0)

    def test_empty_contract_terms_returns_unknown(self):
        profile = _make_profile([])
        ctype, conf = _detect_contract_type_keywords(profile)
        assert ctype == "UNKNOWN"
        assert conf == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# analyse_commercial — contract type detection (deterministic)
# ---------------------------------------------------------------------------

class TestAnalyseCommercialContractType:
    def test_saas_contract_type_high_confidence_no_llm(self):
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user",
                              "licence", "per user", "software as a service"],
        )
        profile = _make_profile([ct])
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.contract_type == "SAAS"
        assert result.contract_type_confidence == "HIGH"
        mock_llm.assert_not_called()

    def test_services_contract_type_no_llm(self):
        ct = _make_contract(
            key_obligations=["statement of work", "milestone", "deliverable",
                              "professional services", "time and materials", "sow"],
        )
        profile = _make_profile([ct])
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.contract_type == "SERVICES"
        mock_llm.assert_not_called()

    def test_mixed_contract_type_high_confidence(self):
        # 7 SAAS + 1 SERVICES = confidence 7/8 >= 0.70 → MIXED, no LLM
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user",
                              "licence", "per user", "software as a service", "sow"],
        )
        profile = _make_profile([ct])
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.contract_type == "MIXED"
        mock_llm.assert_not_called()

    def test_no_keyword_signals_returns_unknown_no_llm(self):
        ct = _make_contract(key_obligations=["quarterly review", "governance"])
        profile = _make_profile([ct])
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.contract_type == "UNKNOWN"
        # No LLM call for UNKNOWN (no signals)
        # Only scenario/narrative LLM calls could run but UNKNOWN skips scenarios
        # Only narrative runs if variance > 15%; contract value == spend → variance 0%
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# analyse_commercial — LLM contract classification
# ---------------------------------------------------------------------------

class TestAnalyseCommercialLLMClassification:
    def _ambiguous_profile(self) -> RelationshipSpendProfile:
        """2 SAAS + 1 SERVICES signals → confidence 2/3 = 0.67 < 0.70 → LLM called."""
        ct = _make_contract(
            key_obligations=["per seat", "licence", "sow"],
        )
        return _make_profile([ct])

    def test_ambiguous_llm_returns_services(self):
        profile = self._ambiguous_profile()
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            # First call: contract classification
            # Second+ calls: renewal scenarios (which returns list) and narrative
            mock_llm.side_effect = [
                {"contract_type": "SERVICES", "confidence": "HIGH", "reasoning": "SOW present"},
                [{"scenario": "best_case", "description": "ok", "probability": 0.7},
                 {"scenario": "expected_case", "description": "normal", "probability": 0.2},
                 {"scenario": "worst_case", "description": "bad", "probability": 0.1}],
            ]
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.contract_type == "SERVICES"
        assert result.contract_type_confidence == "HIGH"

    def test_ambiguous_llm_fails_returns_unknown(self):
        profile = self._ambiguous_profile()
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.side_effect = Exception("LLM unavailable")
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.contract_type == "UNKNOWN"
        assert result.contract_type_confidence == "LOW"

    def test_ambiguous_llm_no_raise_on_failure(self):
        profile = self._ambiguous_profile()
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.side_effect = Exception("LLM unavailable")
            # Must not raise
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert isinstance(result, CommercialAnalysisResult)


# ---------------------------------------------------------------------------
# _analyse_saas
# ---------------------------------------------------------------------------

class TestAnalyseSaas:
    def test_utilisation_60_pct_licence_waste_flag(self):
        bundle = _make_bundle(
            active_users=60,
            total_licences=100,
            annual_contract_value=120_000.0,
        )
        result = _analyse_saas(bundle, _empty_assembly())
        assert "LICENCE_WASTE" in result["findings"]
        assert result["licence_waste_pct"] == pytest.approx(40.0)
        assert result["utilisation_score"] == pytest.approx(0.60)

    def test_utilisation_90_pct_no_licence_waste(self):
        bundle = _make_bundle(
            active_users=90,
            total_licences=100,
        )
        result = _analyse_saas(bundle, _empty_assembly())
        assert "LICENCE_WASTE" not in result["findings"]

    def test_utilisation_40_pct_shelfware_detected(self):
        bundle = _make_bundle(
            active_users=40,
            total_licences=100,
        )
        result = _analyse_saas(bundle, _empty_assembly())
        assert result["shelfware_flag"] is True
        assert "SHELFWARE_DETECTED" in result["findings"]
        assert "LICENCE_WASTE" in result["findings"]

    def test_utilisation_75_pct_no_shelfware(self):
        bundle = _make_bundle(
            active_users=75,
            total_licences=100,
        )
        result = _analyse_saas(bundle, _empty_assembly())
        assert result["shelfware_flag"] is False
        assert "SHELFWARE_DETECTED" not in result["findings"]

    def test_no_bundle_returns_missing_flag_and_none_metrics(self):
        result = _analyse_saas(None, _empty_assembly())
        assert "LICENCE_DATA_MISSING" in result["findings"]
        assert result["utilisation_score"] is None
        assert result["licence_waste_pct"] is None
        assert result["cost_per_seat"] is None

    def test_cost_per_seat_computed(self):
        bundle = _make_bundle(
            active_users=100,
            total_licences=120,
            annual_contract_value=60_000.0,
        )
        result = _analyse_saas(bundle, _empty_assembly())
        assert result["cost_per_seat"] == pytest.approx(600.0)


# ---------------------------------------------------------------------------
# _analyse_services
# ---------------------------------------------------------------------------

class TestAnalyseServices:
    def test_sla_adherence_85_breach_pattern_flag(self):
        bundle = _make_bundle(
            compliant_tickets=85,
            total_priority_tickets=100,
        )
        result = _analyse_services(bundle, _empty_assembly())
        assert result["sla_adherence_pct"] == pytest.approx(85.0)
        assert "SLA_BREACH_PATTERN" in result["findings"]

    def test_sla_adherence_92_no_breach_flag(self):
        bundle = _make_bundle(
            compliant_tickets=92,
            total_priority_tickets=100,
        )
        result = _analyse_services(bundle, _empty_assembly())
        assert result["sla_adherence_pct"] == pytest.approx(92.0)
        assert "SLA_BREACH_PATTERN" not in result["findings"]

    def test_delivery_score_75_milestone_risk(self):
        bundle = _make_bundle(
            compliant_tickets=90,
            total_priority_tickets=100,
            milestones_hit=3,
            total_milestones=4,
        )
        result = _analyse_services(bundle, _empty_assembly())
        assert result["delivery_score"] == pytest.approx(75.0)
        assert "MILESTONE_RISK" in result["findings"]

    def test_delivery_score_100_no_milestone_risk(self):
        bundle = _make_bundle(
            compliant_tickets=90,
            total_priority_tickets=100,
            milestones_hit=4,
            total_milestones=4,
        )
        result = _analyse_services(bundle, _empty_assembly())
        assert result["delivery_score"] == pytest.approx(100.0)
        assert "MILESTONE_RISK" not in result["findings"]

    def test_no_bundle_returns_ticket_data_missing(self):
        result = _analyse_services(None, _empty_assembly())
        assert "TICKET_DATA_MISSING" in result["findings"]
        assert result["sla_adherence_pct"] is None

    def test_penalty_exposure_sum(self):
        bundle = _make_bundle(
            compliant_tickets=80,
            total_priority_tickets=100,
            sla_credit_caps=[5_000.0, 3_000.0],
        )
        result = _analyse_services(bundle, _empty_assembly())
        assert result["penalty_exposure"] == pytest.approx(8_000.0)


# ---------------------------------------------------------------------------
# _compute_risk_level
# ---------------------------------------------------------------------------

class TestComputeRiskLevel:
    def test_critical_licence_waste_and_high_pct(self):
        findings = ["LICENCE_WASTE"]
        metrics = {"licence_waste_pct": 35.0, "penalty_exposure": None,
                   "utilisation_score": 0.65, "delivery_score": None}
        assert _compute_risk_level(findings, metrics) == "CRITICAL"

    def test_critical_sla_breach_with_penalty(self):
        findings = ["SLA_BREACH_PATTERN"]
        metrics = {"licence_waste_pct": None, "penalty_exposure": 10_000.0,
                   "utilisation_score": None, "delivery_score": None}
        assert _compute_risk_level(findings, metrics) == "CRITICAL"

    def test_high_licence_waste_under_30(self):
        findings = ["LICENCE_WASTE"]
        metrics = {"licence_waste_pct": 25.0, "penalty_exposure": None,
                   "utilisation_score": 0.75, "delivery_score": None}
        assert _compute_risk_level(findings, metrics) == "HIGH"

    def test_high_incident_frequency_rising(self):
        findings = ["INCIDENT_FREQUENCY_RISING"]
        metrics = {"licence_waste_pct": None, "penalty_exposure": None,
                   "utilisation_score": None, "delivery_score": None}
        assert _compute_risk_level(findings, metrics) == "HIGH"

    def test_medium_licence_data_missing(self):
        findings = ["LICENCE_DATA_MISSING"]
        metrics = {"licence_waste_pct": None, "penalty_exposure": None,
                   "utilisation_score": None, "delivery_score": None}
        assert _compute_risk_level(findings, metrics) == "MEDIUM"

    def test_medium_ticket_data_missing(self):
        findings = ["TICKET_DATA_MISSING"]
        metrics = {"licence_waste_pct": None, "penalty_exposure": None,
                   "utilisation_score": None, "delivery_score": None}
        assert _compute_risk_level(findings, metrics) == "MEDIUM"

    def test_medium_low_utilisation_score(self):
        findings = []
        metrics = {"licence_waste_pct": None, "penalty_exposure": None,
                   "utilisation_score": 0.80, "delivery_score": None}
        assert _compute_risk_level(findings, metrics) == "MEDIUM"

    def test_low_when_no_flags(self):
        findings = []
        metrics = {"licence_waste_pct": None, "penalty_exposure": None,
                   "utilisation_score": None, "delivery_score": None}
        assert _compute_risk_level(findings, metrics) == "LOW"


# ---------------------------------------------------------------------------
# analyse_commercial — SaaS path via analyse_commercial
# ---------------------------------------------------------------------------

class TestAnalyseCommercialSaasPath:
    def _saas_profile(self) -> RelationshipSpendProfile:
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user",
                              "licence", "per user", "software as a service"],
        )
        return _make_profile([ct])

    def test_saas_with_no_bundle_licence_data_missing(self):
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []  # renewal scenarios call returns empty
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._saas_profile(),
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert "LICENCE_DATA_MISSING" in result.commercial_findings
        assert result.utilisation_score is None
        assert result.licence_waste_pct is None
        assert result.cost_per_seat is None

    def test_saas_utilisation_60_pct_in_full_run(self):
        bundle = _make_bundle(active_users=60, total_licences=100)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._saas_profile(),
                structured_bundle=bundle,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert "LICENCE_WASTE" in result.commercial_findings
        assert result.licence_waste_pct == pytest.approx(40.0)

    def test_saas_utilisation_90_pct_no_waste_flag(self):
        bundle = _make_bundle(active_users=90, total_licences=100)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._saas_profile(),
                structured_bundle=bundle,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert "LICENCE_WASTE" not in result.commercial_findings

    def test_saas_shelfware_detected(self):
        bundle = _make_bundle(active_users=40, total_licences=100)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._saas_profile(),
                structured_bundle=bundle,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.shelfware_flag is True
        assert "SHELFWARE_DETECTED" in result.commercial_findings


# ---------------------------------------------------------------------------
# analyse_commercial — SERVICES path
# ---------------------------------------------------------------------------

class TestAnalyseCommercialServicesPath:
    def _services_profile(self) -> RelationshipSpendProfile:
        ct = _make_contract(
            key_obligations=["statement of work", "milestone", "deliverable",
                              "professional services", "time and materials", "sow"],
        )
        return _make_profile([ct])

    def test_services_no_bundle_ticket_data_missing(self):
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._services_profile(),
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert "TICKET_DATA_MISSING" in result.commercial_findings

    def test_services_sla_breach_85_pct(self):
        bundle = _make_bundle(compliant_tickets=85, total_priority_tickets=100)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._services_profile(),
                structured_bundle=bundle,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert "SLA_BREACH_PATTERN" in result.commercial_findings
        assert result.sla_adherence_pct == pytest.approx(85.0)

    def test_services_sla_92_pct_no_breach(self):
        bundle = _make_bundle(compliant_tickets=92, total_priority_tickets=100)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._services_profile(),
                structured_bundle=bundle,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert "SLA_BREACH_PATTERN" not in result.commercial_findings


# ---------------------------------------------------------------------------
# analyse_commercial — UNKNOWN path
# ---------------------------------------------------------------------------

class TestAnalyseCommercialUnknownPath:
    def test_unknown_contract_risk_level_low(self):
        ct = _make_contract(key_obligations=["generic vendor service"])
        profile = _make_profile([ct])
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.contract_type == "UNKNOWN"
        assert result.commercial_risk_level == "LOW"
        mock_llm.assert_not_called()

    def test_unknown_no_crash(self):
        profile = _make_profile([])
        result = analyse_commercial(
            vendor_id=VENDOR_ID,
            validated_assembly=_empty_assembly(),
            rs_profile=profile,
            structured_bundle=None,
            historical_state=None,
            scoring_config=_scoring_config(),
        )
        assert isinstance(result, CommercialAnalysisResult)


# ---------------------------------------------------------------------------
# Renewal scenarios (LLM Call B)
# ---------------------------------------------------------------------------

class TestRenewalScenarios:
    def _saas_profile_with_expiry(self) -> RelationshipSpendProfile:
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user",
                              "licence", "per user", "software as a service"],
            expiry_date="2026-06-01",
        )
        return _make_profile([ct])

    def test_valid_scenarios_populated(self):
        scenarios = [
            {"scenario": "best_case", "description": "Renew as-is", "probability": 0.6},
            {"scenario": "expected_case", "description": "Minor price increase", "probability": 0.3},
            {"scenario": "worst_case", "description": "30% uplift", "probability": 0.1},
        ]
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = scenarios
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._saas_profile_with_expiry(),
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert len(result.renewal_risk_scenarios) == 3
        assert result.renewal_risk_scenarios[0]["scenario"] == "best_case"

    def test_llm_scenarios_fail_returns_empty_no_crash(self):
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.side_effect = Exception("LLM unavailable")
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=self._saas_profile_with_expiry(),
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.renewal_risk_scenarios == []

    def test_no_expiry_date_no_scenarios_llm_call(self):
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user",
                              "licence", "per user", "software as a service"],
            expiry_date=None,  # no expiry
        )
        profile = _make_profile([ct])
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.renewal_risk_scenarios == []
        # LLM should not be called for scenarios (no expiry date)
        calls = [str(c) for c in mock_llm.call_args_list]
        # The scenarios call never happened (mock not called for scenarios)
        assert mock_llm.call_count == 0

    def test_no_contract_terms_no_scenarios(self):
        profile = _make_profile([])
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.renewal_risk_scenarios == []
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Spend efficiency narrative (LLM Call C)
# ---------------------------------------------------------------------------

class TestSpendEfficiencyNarrative:
    def _saas_profile_with_variance(self, contract_val: float, actual_spend: float) -> RelationshipSpendProfile:
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user",
                              "licence", "per user", "software as a service"],
            total_value=contract_val,
            expiry_date=None,  # suppress scenarios LLM call
        )
        return _make_profile([ct], spend=actual_spend)

    def test_variance_5_pct_no_narrative_llm(self):
        # contract=100000, actual=105000 → variance=5% → no narrative call
        profile = self._saas_profile_with_variance(100_000.0, 105_000.0)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        # narrative LLM call should NOT be made (variance only 5%)
        mock_llm.assert_not_called()
        assert result.spend_efficiency_narrative is None

    def test_variance_35_pct_narrative_llm_called(self):
        # contract=100000, actual=135000 → variance=35% → narrative call
        profile = self._saas_profile_with_variance(100_000.0, 135_000.0)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = {"narrative": "Significant spend overage detected."}
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        mock_llm.assert_called_once()
        assert result.spend_efficiency_narrative == "Significant spend overage detected."

    def test_narrative_llm_fails_returns_none_no_crash(self):
        profile = self._saas_profile_with_variance(100_000.0, 135_000.0)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.side_effect = Exception("LLM unavailable")
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.spend_efficiency_narrative is None


# ---------------------------------------------------------------------------
# _compute_spend_efficiency
# ---------------------------------------------------------------------------

class TestComputeSpendEfficiency:
    def test_variance_pct_computed(self):
        ct = _make_contract(total_value=100_000.0)
        profile = _make_profile([ct], spend=120_000.0)
        contract_total, actual, variance = _compute_spend_efficiency(profile)
        assert contract_total == pytest.approx(100_000.0)
        assert actual == pytest.approx(120_000.0)
        assert variance == pytest.approx(20.0)

    def test_no_contract_total_returns_none_variance(self):
        ct = _make_contract(total_value=None)
        profile = _make_profile([ct], spend=50_000.0)
        contract_total, actual, variance = _compute_spend_efficiency(profile)
        assert variance is None

    def test_spend_efficiency_score_decreases_with_variance(self):
        ct = _make_contract(total_value=100_000.0)
        profile = _make_profile([ct], spend=110_000.0)  # 10% variance
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.spend_efficiency_score == pytest.approx(90.0)

    def test_spend_efficiency_score_clamped_at_zero(self):
        ct = _make_contract(total_value=100_000.0)
        profile = _make_profile([ct], spend=300_000.0)  # 200% variance
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = {"narrative": "Big overage."}
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert result.spend_efficiency_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# CONTRACT_DEVIATION flag forwarding
# ---------------------------------------------------------------------------

class TestContractDeviationFlag:
    def test_contract_deviation_forwarded_from_profile_flags(self):
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user",
                              "licence", "per user", "software as a service"],
            expiry_date=None,
        )
        profile = _make_profile([ct], flags=["CONTRACT_DEVIATION"])
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=None,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        assert "CONTRACT_DEVIATION" in result.commercial_findings


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------

class TestCommercialAnalysisResultRoundTrip:
    def test_round_trip(self):
        ct = _make_contract(
            key_obligations=["per seat", "subscription", "saas", "named user",
                              "licence", "per user", "software as a service"],
            expiry_date=None,
        )
        profile = _make_profile([ct])
        bundle = _make_bundle(active_users=70, total_licences=100)
        with patch("cobalt.tools.commercial_analyser.llm_call") as mock_llm:
            mock_llm.return_value = []
            result = analyse_commercial(
                vendor_id=VENDOR_ID,
                validated_assembly=_empty_assembly(),
                rs_profile=profile,
                structured_bundle=bundle,
                historical_state=None,
                scoring_config=_scoring_config(),
            )
        d = result.to_dict()
        restored = CommercialAnalysisResult.from_dict(d)
        assert restored.vendor_id == result.vendor_id
        assert restored.contract_type == result.contract_type
        assert restored.commercial_risk_level == result.commercial_risk_level
        assert restored.commercial_findings == result.commercial_findings
        assert restored.licence_waste_pct == pytest.approx(result.licence_waste_pct)
