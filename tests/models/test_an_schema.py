"""Tests for cobalt.models.schemas.an_schema — all dataclasses round-trip via to_dict/from_dict."""

from cobalt.models.schemas.an_schema import (
    ActionLearning,
    ActionOutcomeHistory,
    ANGap,
    ANRunResult,
    ANRunStatus,
    CommercialAnalysisResult,
    CommercialRisk,
    ContractType,
    DimensionScore,
    EvidenceCitation,
    ExtractionType,
    Finding,
    FindingNarrative,
    FindingsBundle,
    FindingSeverity,
    FindingSource,
    FindingStatus,
    FreshnessStatus,
    GapSeverityAN,
    HistoricalCommercialState,
    HistoricalEvidenceState,
    HistoricalQAState,
    HistoricalScoreState,
    NBA,
    NarrativeBundle,
    QACompleteness,
    QAPair,
    QASummary,
    QuestionSetItem,
    ScoreBundle,
    ScoringConfig,
    TrendDirection,
    TrendPattern,
    TrendReport,
    ValidatedEvidenceAssembly,
    ValidatedEvidenceFact,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fact(**overrides) -> ValidatedEvidenceFact:
    defaults = dict(
        field_name="legal_name",
        value="Acme Corp",
        display_value="Acme Corp",
        extraction_type="AUTO_EXTRACTED",
        source_file="relationship_spend_profile.md",
        source_section="identity",
        confidence="HIGH",
        trust_level="OFFICIAL",
        freshness_status="CURRENT",
        conflict_flag=False,
        conflict_values=[],
        quality_score=0.9,
        validated_at="2026-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return ValidatedEvidenceFact(**defaults)


def _make_finding(**overrides) -> Finding:
    defaults = dict(
        finding_id="f-001",
        title="SLA breach",
        severity="HIGH",
        why="Breached 3 of 5 SLAs in last quarter",
        evidence_ids=["ev-001"],
        source="SCORE",
        status="OPEN",
        created_at="2026-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return Finding(**defaults)


# ---------------------------------------------------------------------------
# ValidatedEvidenceFact
# ---------------------------------------------------------------------------

def test_validated_evidence_fact_round_trip():
    f = _make_fact()
    assert ValidatedEvidenceFact.from_dict(f.to_dict()) == f


def test_validated_evidence_fact_conflict_flag_true():
    f = _make_fact(conflict_flag=True, conflict_values=["Acme Corp", "Acme Corporation"])
    result = ValidatedEvidenceFact.from_dict(f.to_dict())
    assert result.conflict_flag is True
    assert result.conflict_values == ["Acme Corp", "Acme Corporation"]


def test_validated_evidence_fact_conflict_values_empty_by_default():
    f = _make_fact(conflict_flag=False, conflict_values=[])
    result = ValidatedEvidenceFact.from_dict(f.to_dict())
    assert result.conflict_values == []


# ---------------------------------------------------------------------------
# ValidatedEvidenceAssembly
# ---------------------------------------------------------------------------

def test_validated_evidence_assembly_round_trip():
    assembly = ValidatedEvidenceAssembly(
        vendor_id="v-001",
        programme_id="prog-1",
        facts=[_make_fact(), _make_fact(field_name="hq_country", value="US")],
        completeness_pct=0.85,
        conflict_count=1,
        stale_count=0,
        missing_count=2,
        validated_at="2026-01-01T00:00:00Z",
    )
    result = ValidatedEvidenceAssembly.from_dict(assembly.to_dict())
    assert result.vendor_id == "v-001"
    assert len(result.facts) == 2
    assert result.facts[0].field_name == "legal_name"
    assert result.facts[1].field_name == "hq_country"
    assert result.completeness_pct == 0.85


def test_validated_evidence_assembly_empty_facts():
    assembly = ValidatedEvidenceAssembly(
        vendor_id="v-001", programme_id="prog-1", facts=[],
        completeness_pct=0.0, conflict_count=0, stale_count=0,
        missing_count=0, validated_at="2026-01-01T00:00:00Z",
    )
    result = ValidatedEvidenceAssembly.from_dict(assembly.to_dict())
    assert result.facts == []


# ---------------------------------------------------------------------------
# DimensionScore + ScoreBundle
# ---------------------------------------------------------------------------

def test_dimension_score_round_trip():
    ds = DimensionScore(
        dimension="delivery_reliability",
        score=75,
        prior_score=70,
        delta=5,
        trend_direction="IMPROVING",
    )
    assert DimensionScore.from_dict(ds.to_dict()) == ds


def test_score_bundle_round_trip():
    sb = ScoreBundle(
        vendor_id="v-001",
        cri_score=72,
        prior_cri=68,
        cri_delta=4,
        health_band="WATCH",
        dimension_scores=[
            DimensionScore("delivery_reliability", 80, None, None, None),
            DimensionScore("responsiveness", 65, 60, 5, "IMPROVING"),
        ],
        operational_metrics={"sla_compliance_pct": 0.92},
        portfolio_rank=None,
        category_rank=None,
        scored_at="2026-01-01T00:00:00Z",
    )
    result = ScoreBundle.from_dict(sb.to_dict())
    assert result.cri_score == 72
    assert len(result.dimension_scores) == 2
    assert result.dimension_scores[1].trend_direction == "IMPROVING"
    assert result.portfolio_rank is None


# ---------------------------------------------------------------------------
# CommercialAnalysisResult
# ---------------------------------------------------------------------------

def test_commercial_analysis_result_all_none_round_trips():
    car = CommercialAnalysisResult(
        vendor_id="v-001",
        contract_type="UNKNOWN",
        contract_type_confidence="LOW",
        utilisation_score=None,
        licence_waste_pct=None,
        cost_per_seat=None,
        shelfware_flag=False,
        sla_adherence_pct=None,
        delivery_score=None,
        milestone_status=None,
        penalty_exposure=None,
        uptime_pct=None,
        incident_trend=None,
        mttr_days=None,
        commercial_risk_level="LOW",
        commercial_findings=[],
        spend_efficiency_score=None,
        renewal_risk_scenarios=[],
        spend_efficiency_narrative=None,
        analysed_at="2026-01-01T00:00:00Z",
    )
    result = CommercialAnalysisResult.from_dict(car.to_dict())
    assert result.vendor_id == "v-001"
    assert result.utilisation_score is None
    assert result.spend_efficiency_narrative is None
    assert result.commercial_findings == []


def test_commercial_analysis_result_with_data_round_trips():
    car = CommercialAnalysisResult(
        vendor_id="v-001",
        contract_type="SAAS",
        contract_type_confidence="HIGH",
        utilisation_score=0.72,
        licence_waste_pct=0.15,
        cost_per_seat=120.0,
        shelfware_flag=True,
        sla_adherence_pct=None,
        delivery_score=None,
        milestone_status=None,
        penalty_exposure=None,
        uptime_pct=None,
        incident_trend=None,
        mttr_days=None,
        commercial_risk_level="MEDIUM",
        commercial_findings=["LICENCE_WASTE"],
        spend_efficiency_score=0.65,
        renewal_risk_scenarios=[{"scenario": "auto_renew", "probability": 0.8}],
        spend_efficiency_narrative="Spend is above market rate.",
        analysed_at="2026-01-01T00:00:00Z",
    )
    result = CommercialAnalysisResult.from_dict(car.to_dict())
    assert result.shelfware_flag is True
    assert result.commercial_findings == ["LICENCE_WASTE"]
    assert result.spend_efficiency_narrative == "Spend is above market rate."


# ---------------------------------------------------------------------------
# TrendReport with ActionLearning
# ---------------------------------------------------------------------------

def test_action_learning_round_trip():
    al = ActionLearning(
        action_type="ESCALATION",
        action_taken_at="2025-06-01",
        before_score=55,
        after_score=68,
        delta=13,
        outcome_label="IMPROVED",
        insight="Escalation led to SLA reset.",
    )
    assert ActionLearning.from_dict(al.to_dict()) == al


def test_trend_report_round_trip():
    tr = TrendReport(
        vendor_id="v-001",
        dimension_trends={"delivery_reliability": {"direction": "IMPROVING"}},
        action_learning=[
            ActionLearning("ESCALATION", "2025-06-01", 55, 68, 13, "IMPROVED", None),
        ],
        action_learning_summary="One escalation improved scores.",
        spend_trend={"direction": "STABLE"},
        sla_trend={"response_time_direction": "IMPROVING"},
        sentiment_trend={"direction": "STABLE"},
        trend_computed_at="2026-01-01T00:00:00Z",
        data_points_available=12,
    )
    result = TrendReport.from_dict(tr.to_dict())
    assert result.vendor_id == "v-001"
    assert len(result.action_learning) == 1
    assert result.action_learning[0].outcome_label == "IMPROVED"


def test_trend_report_empty_action_learning():
    tr = TrendReport(
        vendor_id="v-001",
        dimension_trends={},
        action_learning=[],
        action_learning_summary=None,
        spend_trend={},
        sla_trend={},
        sentiment_trend={},
        trend_computed_at="2026-01-01T00:00:00Z",
        data_points_available=0,
    )
    result = TrendReport.from_dict(tr.to_dict())
    assert result.action_learning == []
    assert result.action_learning_summary is None


# ---------------------------------------------------------------------------
# QAPair with EvidenceCitations
# ---------------------------------------------------------------------------

def test_evidence_citation_round_trip():
    ec = EvidenceCitation(
        evidence_id="ev-001",
        source_file="contract.pdf",
        source_section="§ 11.2",
        extraction_type="AUTO_EXTRACTED",
        quality_score=0.95,
        display_text="SLA Exhibit A · § 11.2 [AUTO-EXTRACTED]",
    )
    assert EvidenceCitation.from_dict(ec.to_dict()) == ec


def test_qa_pair_round_trip():
    qp = QAPair(
        question_id="q-001",
        question="Is there a signed MSA?",
        answer_text="Yes, MSA signed 2024-01-15.",
        confidence="HIGH",
        completeness="COMPLETE",
        answered_by="inquiry_engine",
        evidence_citations=[
            EvidenceCitation("ev-001", "msa.pdf", None, "AUTO_EXTRACTED", 0.9,
                             "MSA · page 1 [AUTO-EXTRACTED]"),
        ],
        missing_evidence=[],
        tier=1,
        answered_at="2026-01-01T00:00:00Z",
    )
    result = QAPair.from_dict(qp.to_dict())
    assert result.question_id == "q-001"
    assert len(result.evidence_citations) == 1
    assert result.evidence_citations[0].evidence_id == "ev-001"


# ---------------------------------------------------------------------------
# FindingsBundle with nba=None
# ---------------------------------------------------------------------------

def test_findings_bundle_nba_none_round_trips():
    fb = FindingsBundle(
        vendor_id="v-001",
        findings=[_make_finding()],
        gaps=[ANGap("Missing SLA data", "BLOCKING", "Upload SLA document")],
        nba=None,
        top_findings=[_make_finding()],
        triage_tasks=[{"triage_type": "GAP_RESOLUTION"}],
        generated_at="2026-01-01T00:00:00Z",
    )
    result = FindingsBundle.from_dict(fb.to_dict())
    assert result.nba is None
    assert len(result.findings) == 1
    assert result.gaps[0].severity == "BLOCKING"


def test_findings_bundle_with_nba_round_trips():
    nba = NBA("Escalate SLA breach", "3 breaches detected", "procurement_owner",
              "THIS_WEEK", True, "f-001", "2026-01-01T00:00:00Z")
    fb = FindingsBundle(
        vendor_id="v-001",
        findings=[],
        gaps=[],
        nba=nba,
        top_findings=[],
        triage_tasks=[],
        generated_at="2026-01-01T00:00:00Z",
    )
    result = FindingsBundle.from_dict(fb.to_dict())
    assert result.nba is not None
    assert result.nba.action == "Escalate SLA breach"


# ---------------------------------------------------------------------------
# NarrativeBundle with empty lists
# ---------------------------------------------------------------------------

def test_narrative_bundle_empty_lists_round_trips():
    nb = NarrativeBundle(
        vendor_id="v-001",
        vendor_summary="Vendor is performing adequately.",
        finding_narratives=[],
        commercial_summary=None,
        qa_summaries=[],
        evidence_citations=[],
        redaction_flags=[],
        generated_at="2026-01-01T00:00:00Z",
    )
    result = NarrativeBundle.from_dict(nb.to_dict())
    assert result.finding_narratives == []
    assert result.qa_summaries == []
    assert result.commercial_summary is None


def test_narrative_bundle_with_data_round_trips():
    nb = NarrativeBundle(
        vendor_id="v-001",
        vendor_summary="Summary.",
        finding_narratives=[
            FindingNarrative("f-001", "SLA breach detected.", "factual", "3 SLAs breached.", False),
        ],
        commercial_summary="Spend is on track.",
        qa_summaries=[QASummary("q-001", "Is there an MSA?", "Yes, MSA is signed.")],
        evidence_citations=["MSA · page 1 [AUTO-EXTRACTED]"],
        redaction_flags=["f-002"],
        generated_at="2026-01-01T00:00:00Z",
    )
    result = NarrativeBundle.from_dict(nb.to_dict())
    assert len(result.finding_narratives) == 1
    assert result.finding_narratives[0].finding_id == "f-001"
    assert result.qa_summaries[0].prose_summary == "Yes, MSA is signed."


# ---------------------------------------------------------------------------
# Historical state types
# ---------------------------------------------------------------------------

def test_historical_score_state_empty_runs_round_trips():
    hs = HistoricalScoreState(vendor_id="v-001", runs=[])
    result = HistoricalScoreState.from_dict(hs.to_dict())
    assert result.runs == []


def test_historical_score_state_with_runs():
    hs = HistoricalScoreState(
        vendor_id="v-001",
        runs=[{"run_at": "2026-01-01", "cri_score": 72, "health_band": "WATCH"}],
    )
    result = HistoricalScoreState.from_dict(hs.to_dict())
    assert result.runs[0]["cri_score"] == 72


def test_historical_evidence_state_round_trip():
    hes = HistoricalEvidenceState(
        vendor_id="v-001",
        prior_assembly_at="2025-12-01T00:00:00Z",
        fact_snapshot={"legal_name": {"value": "Acme", "quality_score": 0.9}},
    )
    result = HistoricalEvidenceState.from_dict(hes.to_dict())
    assert result.fact_snapshot["legal_name"]["value"] == "Acme"


def test_historical_qa_state_round_trip():
    hq = HistoricalQAState(
        vendor_id="v-001",
        prior_pairs=[{"question_id": "q-001", "answer_text": "Yes"}],
    )
    result = HistoricalQAState.from_dict(hq.to_dict())
    assert result.prior_pairs[0]["question_id"] == "q-001"


def test_historical_commercial_state_none_fields():
    hcs = HistoricalCommercialState(
        vendor_id="v-001",
        prior_analysis_at="2025-12-01T00:00:00Z",
        prior_contract_type=None,
        prior_risk_level=None,
    )
    result = HistoricalCommercialState.from_dict(hcs.to_dict())
    assert result.prior_contract_type is None
    assert result.prior_risk_level is None


def test_action_outcome_history_round_trip():
    aoh = ActionOutcomeHistory(
        vendor_id="v-001",
        actions=[{"action_type": "ESCALATION", "before_cri": 55, "after_cri": 68}],
    )
    result = ActionOutcomeHistory.from_dict(aoh.to_dict())
    assert result.actions[0]["action_type"] == "ESCALATION"


# ---------------------------------------------------------------------------
# Config types
# ---------------------------------------------------------------------------

def test_question_set_item_round_trip():
    qsi = QuestionSetItem(
        question_id="q-001",
        question="Is there a signed MSA?",
        tier=1,
        dimension="risk_compliance",
        contract_types=["SAAS", "SERVICES"],
    )
    assert QuestionSetItem.from_dict(qsi.to_dict()) == qsi


def test_question_set_item_empty_contract_types():
    qsi = QuestionSetItem("q-002", "Describe SLA terms.", 2, "delivery_reliability", [])
    result = QuestionSetItem.from_dict(qsi.to_dict())
    assert result.contract_types == []


def test_scoring_config_round_trip():
    sc = ScoringConfig(
        dimension_weights={"delivery_reliability": 0.3, "responsiveness": 0.2},
        health_band_thresholds={"HEALTHY": 80, "WATCH": 65},
        tier_cri_thresholds={"STRATEGIC": 75, "TRANSACTIONAL": 50},
        spike_multiplier=1.5,
    )
    result = ScoringConfig.from_dict(sc.to_dict())
    assert result.spike_multiplier == 1.5
    assert result.dimension_weights["delivery_reliability"] == 0.3


# ---------------------------------------------------------------------------
# ANRunResult with all None optional fields
# ---------------------------------------------------------------------------

def test_an_run_result_all_none_round_trips():
    r = ANRunResult(
        vendor_id="v-001",
        programme_id="prog-1",
        status="COMPLETED",
        cri_score=None,
        health_band=None,
        finding_count=0,
        nba_action=None,
        pcs_before=None,
        pcs_after=None,
        tools_run=[],
        skip_reason=None,
        error=None,
        analysed_at="2026-01-01T00:00:00Z",
    )
    result = ANRunResult.from_dict(r.to_dict())
    assert result.cri_score is None
    assert result.health_band is None
    assert result.nba_action is None
    assert result.skip_reason is None
    assert result.error is None
    assert result.tools_run == []


def test_an_run_result_with_data_round_trips():
    r = ANRunResult(
        vendor_id="v-001",
        programme_id="prog-1",
        status="COMPLETED",
        cri_score=72,
        health_band="WATCH",
        finding_count=3,
        nba_action="Escalate SLA",
        pcs_before=0.75,
        pcs_after=0.85,
        tools_run=["evidence_validator", "scoring_engine"],
        skip_reason=None,
        error=None,
        analysed_at="2026-01-01T00:00:00Z",
    )
    result = ANRunResult.from_dict(r.to_dict())
    assert result.cri_score == 72
    assert result.health_band == "WATCH"
    assert result.pcs_after == 0.85
    assert len(result.tools_run) == 2


# ---------------------------------------------------------------------------
# Enum str subclass checks (JSON serialisable without extra conversion)
# ---------------------------------------------------------------------------

def test_all_enums_are_str_subclasses():
    for enum_cls in [
        ExtractionType, FreshnessStatus, ContractType, TrendDirection, TrendPattern,
        FindingSeverity, FindingStatus, FindingSource, GapSeverityAN,
        CommercialRisk, QACompleteness, ANRunStatus,
    ]:
        for member in enum_cls:
            assert isinstance(member, str), f"{enum_cls.__name__}.{member.name} is not a str"


def test_enum_values_json_serialisable():
    import json
    values = [
        ExtractionType.AUTO_EXTRACTED,
        FreshnessStatus.CURRENT,
        ContractType.SAAS,
        TrendDirection.IMPROVING,
        TrendPattern.CYCLICAL,
        FindingSeverity.HIGH,
        FindingStatus.OPEN,
        FindingSource.SCORE,
        GapSeverityAN.BLOCKING,
        CommercialRisk.MEDIUM,
        QACompleteness.COMPLETE,
        ANRunStatus.COMPLETED,
    ]
    # Should not raise
    serialised = json.dumps(values)
    parsed = json.loads(serialised)
    assert "AUTO_EXTRACTED" in parsed
    assert "COMPLETED" in parsed
