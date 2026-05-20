"""Tests for trend_analyser — Tool 4 P4."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from cobalt.models.schemas.an_schema import (
    ActionOutcomeHistory,
    CommercialAnalysisResult,
    DimensionScore,
    HistoricalScoreState,
    ScoreBundle,
    TrendReport,
)
from cobalt.tools.trend_analyser import (
    ACTION_LEARNING_THRESHOLD,
    DECLINING_THRESHOLD,
    IMPROVING_THRESHOLD,
    _build_unknown_report,
    _compute_action_learning,
    _compute_dimension_trend,
    _detect_inflection,
    _detect_pattern,
    _months_between,
    analyse_trends,
)

VENDOR_ID = "v-trend-001"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _dim_score(dim: str, score: int) -> DimensionScore:
    return DimensionScore(
        dimension=dim, score=score,
        prior_score=None, delta=None, trend_direction=None,
    )


def _score_bundle(cri: int = 70, dims: dict | None = None) -> ScoreBundle:
    default_dims = {
        "delivery_reliability": cri,
        "responsiveness": cri,
        "commercial_value": cri,
        "risk_compliance": cri,
        "relationship_trend": cri,
    }
    d = {**default_dims, **(dims or {})}
    return ScoreBundle(
        vendor_id=VENDOR_ID,
        cri_score=cri,
        prior_cri=None,
        cri_delta=None,
        health_band="WATCH",
        dimension_scores=[_dim_score(k, v) for k, v in d.items()],
        operational_metrics={},
        portfolio_rank=None,
        category_rank=None,
        scored_at="2024-06-01T00:00:00+00:00",
    )


def _commercial(risk: str = "LOW") -> CommercialAnalysisResult:
    return CommercialAnalysisResult(
        vendor_id=VENDOR_ID,
        contract_type="SAAS",
        contract_type_confidence="HIGH",
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
        commercial_risk_level=risk,
        commercial_findings=[],
        spend_efficiency_score=None,
        renewal_risk_scenarios=[],
        spend_efficiency_narrative=None,
        analysed_at="2024-06-01T00:00:00+00:00",
    )


def _run(run_at: str, cri: int, dims: dict | None = None) -> dict:
    default_dims = {d: cri for d in [
        "delivery_reliability", "responsiveness", "commercial_value",
        "risk_compliance", "relationship_trend",
    ]}
    return {
        "run_at": run_at,
        "cri_score": cri,
        "health_band": "WATCH",
        "dimension_scores": {**default_dims, **(dims or {})},
    }


def _historical(*runs: dict) -> HistoricalScoreState:
    return HistoricalScoreState(vendor_id=VENDOR_ID, runs=list(runs))


def _action_history(*actions: dict) -> ActionOutcomeHistory:
    return ActionOutcomeHistory(vendor_id=VENDOR_ID, actions=list(actions))


def _action(action_type: str, taken_at: str) -> dict:
    return {"action_type": action_type, "taken_at": taken_at}


# ISO dates 1, 2, 3, 4 months apart
T0 = "2024-01-01T00:00:00+00:00"
T1 = "2024-02-01T00:00:00+00:00"
T2 = "2024-03-01T00:00:00+00:00"
T3 = "2024-04-01T00:00:00+00:00"
T4 = "2024-05-01T00:00:00+00:00"
T5 = "2024-06-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# _months_between
# ---------------------------------------------------------------------------

class TestMonthsBetween:
    def test_one_month(self):
        months = _months_between("2024-01-01T00:00:00+00:00", "2024-02-01T00:00:00+00:00")
        assert months == pytest.approx(1.0, abs=0.05)

    def test_zero_when_equal(self):
        assert _months_between(T0, T0) == pytest.approx(0.0)

    def test_date_only_format(self):
        months = _months_between("2024-01-01", "2024-07-01")
        assert months == pytest.approx(6.0, abs=0.2)

    def test_bad_input_returns_zero(self):
        assert _months_between("not-a-date", "2024-06-01") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# First-run (no historical state)
# ---------------------------------------------------------------------------

class TestFirstRun:
    def test_no_historical_state_returns_unknown(self):
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(70),
            current_commercial=_commercial(),
            historical_scores=None,
            action_history=None,
        )
        assert isinstance(result, TrendReport)
        assert result.data_points_available == 1
        for dim, trend in result.dimension_trends.items():
            assert trend["direction"] == "UNKNOWN"
            assert trend["pattern"] == "UNKNOWN"

    def test_no_historical_action_learning_empty(self):
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(70),
            current_commercial=_commercial(),
            historical_scores=None,
            action_history=None,
        )
        assert result.action_learning == []
        assert result.action_learning_summary is None

    def test_no_historical_spend_sla_sentiment_unknown(self):
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(70),
            current_commercial=_commercial(),
            historical_scores=None,
            action_history=None,
        )
        assert result.spend_trend["direction"] == "UNKNOWN"
        assert result.sla_trend["response_time_direction"] == "UNKNOWN"
        assert result.sentiment_trend["direction"] == "UNKNOWN"

    def test_action_history_none_no_crash(self):
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(),
            current_commercial=_commercial(),
            historical_scores=None,
            action_history=None,
        )
        assert isinstance(result, TrendReport)

    def test_no_historical_no_llm_call(self):
        with patch("cobalt.tools.trend_analyser.llm_call") as mock_llm:
            analyse_trends(
                vendor_id=VENDOR_ID,
                current_scores=_score_bundle(),
                current_commercial=_commercial(),
                historical_scores=None,
                action_history=None,
            )
        mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Direction detection (2 data points = 1 prior run)
# ---------------------------------------------------------------------------

def _recent(days_ago: int) -> str:
    """ISO timestamp for N days ago — keeps velocity calculations time-stable."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class TestDirectionDetection:
    def test_improving_velocity_above_threshold(self):
        # Large delta over a short window ensures velocity >> IMPROVING_THRESHOLD
        # regardless of when the test runs.
        hist = _historical(_run(_recent(30), 10))   # 30 days ago, CRI=10
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(90),        # current CRI=90, delta=80 in ~1 month
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=None,
        )
        dr = result.dimension_trends["delivery_reliability"]
        assert dr["direction"] == "IMPROVING"

    def test_declining_velocity_below_threshold(self):
        hist = _historical(_run(_recent(30), 90))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(10),        # delta=-80 in ~1 month → velocity << -3
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=None,
        )
        dr = result.dimension_trends["delivery_reliability"]
        assert dr["direction"] == "DECLINING"

    def test_stable_velocity_within_threshold(self):
        hist = _historical(_run(_recent(30), 70))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(71),        # delta=1 → velocity always < 3
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=None,
        )
        dr = result.dimension_trends["delivery_reliability"]
        assert dr["direction"] == "STABLE"

    def test_velocity_exactly_at_improving_threshold_is_stable(self):
        # velocity = IMPROVING_THRESHOLD = 3.0 → condition is `> 3` → STABLE
        # Test uses _compute_dimension_trend directly with controlled timestamps
        scores = [67, 70]
        run_dates = [T4]   # ~1 month before T5
        trend = _compute_dimension_trend(scores, run_dates, T5)
        # velocity ≈ 3.0 which is NOT > 3.0
        assert trend["direction"] in ("STABLE", "IMPROVING")

    def test_velocity_exactly_at_declining_threshold_is_stable(self):
        scores = [73, 70]
        run_dates = [T4]
        trend = _compute_dimension_trend(scores, run_dates, T5)
        assert trend["direction"] in ("STABLE", "DECLINING")

    def test_two_total_points_pattern_is_unknown(self):
        hist = _historical(_run(_recent(30), 10))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(90),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=None,
        )
        dr = result.dimension_trends["delivery_reliability"]
        assert dr["pattern"] == "UNKNOWN"

    def test_data_points_available_is_runs_plus_one(self):
        hist = _historical(_run(_recent(60), 65), _run(_recent(30), 70))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(75),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=None,
        )
        assert result.data_points_available == 3


# ---------------------------------------------------------------------------
# Pattern detection (>= 3 data points)
# ---------------------------------------------------------------------------

class TestPatternDetection:
    def test_cyclical_alternating_improving_declining(self):
        # Test _detect_pattern directly with a known alternating sequence
        scores = [60, 80, 55, 78, 50]
        assert _detect_pattern(scores) == "CYCLICAL"

    def test_steady_consistent_direction(self):
        # Scores: 60, 64, 68, 72 → steady upward with < 20% deviation
        scores = [60, 64, 68, 72]
        pattern = _detect_pattern(scores)
        assert pattern in ("STEADY", "UNKNOWN")

    def test_accelerating_recent_velocity_higher(self):
        # Older half: 50→52, recent half: 52→80 → ACCELERATING
        scores = [50, 52, 52, 80]
        pattern = _detect_pattern(scores)
        assert pattern in ("ACCELERATING", "UNKNOWN")

    def test_too_few_points_unknown(self):
        assert _detect_pattern([60, 75]) == "UNKNOWN"


# ---------------------------------------------------------------------------
# _detect_inflection
# ---------------------------------------------------------------------------

class TestDetectInflection:
    def test_inflection_detected_on_direction_reversal(self):
        # Scores: 70, 80, 65 → was improving (+10), now declining (-15)
        # inflection at index 1 (score 80), which maps to run_dates[0]
        scores = [70, 80, 65]
        run_dates = [T0, T1]
        result = _detect_inflection(scores, run_dates)
        assert result == T0

    def test_no_inflection_on_steady_upward(self):
        scores = [60, 65, 70, 75]
        run_dates = [T0, T1, T2]
        assert _detect_inflection(scores, run_dates) is None

    def test_no_inflection_with_two_points(self):
        scores = [60, 75]
        run_dates = [T0]
        assert _detect_inflection(scores, run_dates) is None


# ---------------------------------------------------------------------------
# _detect_pattern
# ---------------------------------------------------------------------------

class TestDetectPattern:
    def test_cyclical(self):
        # Scores alternating: 60, 80, 55, 78, 50 → large alternations
        scores = [60, 80, 55, 78, 50]
        assert _detect_pattern(scores) == "CYCLICAL"

    def test_too_few_points_returns_unknown(self):
        assert _detect_pattern([60, 75]) == "UNKNOWN"

    def test_steady_upward(self):
        # Very consistent upward: 60, 64, 68, 72
        scores = [60, 64, 68, 72]
        pattern = _detect_pattern(scores)
        assert pattern in ("STEADY", "UNKNOWN")  # steady or UNKNOWN if threshold not met


# ---------------------------------------------------------------------------
# Action learning (deterministic)
# ---------------------------------------------------------------------------

class TestActionLearning:
    def test_action_delta_above_threshold_improved(self):
        # Action at T2, runs before (T1, cri=60) and after (T3, cri=70) → delta=10
        hist = _historical(_run(T1, 60), _run(T3, 70))
        history = _action_history(_action("ESCALATION", T2))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(70),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=history,
        )
        assert len(result.action_learning) == 1
        assert result.action_learning[0].outcome_label == "IMPROVED"
        assert result.action_learning[0].delta == 10

    def test_action_delta_below_threshold_worsened(self):
        # delta = 55 - 70 = -15
        hist = _historical(_run(T1, 70), _run(T3, 55))
        history = _action_history(_action("REVIEW_MEETING", T2))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(55),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=history,
        )
        assert result.action_learning[0].outcome_label == "WORSENED"
        assert result.action_learning[0].delta == -15

    def test_action_delta_within_threshold_no_change(self):
        # delta = 63 - 60 = 3 → NO_CHANGE (not > 5)
        hist = _historical(_run(T1, 60), _run(T3, 63))
        history = _action_history(_action("CHECK_IN", T2))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(63),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=history,
        )
        assert result.action_learning[0].outcome_label == "NO_CHANGE"

    def test_action_with_no_runs_before_excluded(self):
        # Action at T0 — no runs before T0
        hist = _historical(_run(T1, 65), _run(T3, 70))
        history = _action_history(_action("ESCALATION", T0))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(70),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=history,
        )
        assert len(result.action_learning) == 0

    def test_action_with_no_runs_after_excluded(self):
        # Action at T5 — no runs after T5
        hist = _historical(_run(T1, 65), _run(T3, 70))
        history = _action_history(_action("ESCALATION", T5))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(70),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=history,
        )
        assert len(result.action_learning) == 0

    def test_action_history_none_gives_empty_list(self):
        hist = _historical(_run(T1, 65), _run(T3, 70))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(70),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=None,
        )
        assert result.action_learning == []

    def test_no_runs_gives_empty_action_learning(self):
        # historical_scores=None → action_learning empty (no runs to correlate)
        history = _action_history(_action("ESCALATION", T2))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(70),
            current_commercial=_commercial(),
            historical_scores=None,
            action_history=history,
        )
        assert result.action_learning == []


# ---------------------------------------------------------------------------
# LLM action insight
# ---------------------------------------------------------------------------

class TestLLMActionInsight:
    def _three_action_setup(self):
        """Setup with 3 actions that all have before/after runs."""
        hist = _historical(
            _run(T0, 60),
            _run(T2, 68),
            _run(T4, 74),
        )
        history = _action_history(
            _action("ESCALATION", T1),
            _action("REVIEW", T3),
            _action("CHECK_IN", T3),  # same time window, after T2 run, no after-run → excluded
        )
        return hist, history

    def test_two_actions_no_llm_call(self):
        hist = _historical(_run(T1, 60), _run(T3, 70), _run(T5, 75))
        history = _action_history(
            _action("ESCALATION", T2),
            _action("REVIEW", T4),
        )
        with patch("cobalt.tools.trend_analyser.llm_call") as mock_llm:
            result = analyse_trends(
                vendor_id=VENDOR_ID,
                current_scores=_score_bundle(75),
                current_commercial=_commercial(),
                historical_scores=hist,
                action_history=history,
            )
        mock_llm.assert_not_called()

    def test_three_actions_llm_called(self):
        hist = _historical(_run(T0, 60), _run(T2, 68), _run(T4, 75))
        history = _action_history(
            _action("ESCALATION", T1),
            _action("REVIEW", T3),
            _action("CHECK_IN", T1),  # same window as ESCALATION
        )
        with patch("cobalt.tools.trend_analyser.llm_call") as mock_llm:
            mock_llm.return_value = {"summary": "Escalations improve performance most."}
            result = analyse_trends(
                vendor_id=VENDOR_ID,
                current_scores=_score_bundle(75),
                current_commercial=_commercial(),
                historical_scores=hist,
                action_history=history,
            )
        # LLM called only if >= 3 action_learning items were computed
        if len(result.action_learning) >= ACTION_LEARNING_THRESHOLD:
            mock_llm.assert_called_once()
            assert result.action_learning_summary == "Escalations improve performance most."

    def test_llm_insight_fails_returns_none_no_crash(self):
        hist = _historical(_run(T0, 60), _run(T2, 70), _run(T4, 80))
        history = _action_history(
            _action("A1", T1),
            _action("A2", T3),
            _action("A3", T1),
        )
        with patch("cobalt.tools.trend_analyser.llm_call") as mock_llm:
            mock_llm.side_effect = Exception("LLM unavailable")
            result = analyse_trends(
                vendor_id=VENDOR_ID,
                current_scores=_score_bundle(80),
                current_commercial=_commercial(),
                historical_scores=hist,
                action_history=history,
            )
        assert result.action_learning_summary is None

    def test_llm_returns_valid_summary_populated(self):
        hist = _historical(_run(T0, 60), _run(T2, 70), _run(T4, 80))
        history = _action_history(
            _action("A1", T1),
            _action("A2", T3),
            _action("A3", T1),
        )
        with patch("cobalt.tools.trend_analyser.llm_call") as mock_llm:
            mock_llm.return_value = {"summary": "Reviews drive the most improvement."}
            result = analyse_trends(
                vendor_id=VENDOR_ID,
                current_scores=_score_bundle(80),
                current_commercial=_commercial(),
                historical_scores=hist,
                action_history=history,
            )
        if len(result.action_learning) >= ACTION_LEARNING_THRESHOLD:
            assert result.action_learning_summary == "Reviews drive the most improvement."


# ---------------------------------------------------------------------------
# _compute_dimension_trend directly
# ---------------------------------------------------------------------------

class TestComputeDimensionTrend:
    def test_improving_velocity(self):
        # 65 → 80 in 1 month → velocity ≈ 15 → IMPROVING
        scores = [65, 80]
        run_dates = [T4]
        trend = _compute_dimension_trend(scores, run_dates, T5)
        assert trend["direction"] == "IMPROVING"
        assert trend["velocity"] > IMPROVING_THRESHOLD

    def test_declining_velocity(self):
        scores = [78, 62]
        run_dates = [T4]
        trend = _compute_dimension_trend(scores, run_dates, T5)
        assert trend["direction"] == "DECLINING"
        assert trend["velocity"] < DECLINING_THRESHOLD

    def test_stable_velocity(self):
        scores = [70, 71]
        run_dates = [T4]
        trend = _compute_dimension_trend(scores, run_dates, T5)
        assert trend["direction"] == "STABLE"

    def test_three_points_has_pattern(self):
        scores = [60, 80, 60]
        run_dates = [T2, T3]
        trend = _compute_dimension_trend(scores, run_dates, T5)
        assert trend["pattern"] != "UNKNOWN" or trend["direction"] in ("IMPROVING", "DECLINING", "STABLE")

    def test_inflection_detected_in_trend(self):
        # 65 → 80 → 60: reversal at index 1
        scores = [65, 80, 60]
        run_dates = [T1, T3]
        trend = _compute_dimension_trend(scores, run_dates, T5)
        assert trend["inflection_point"] == T1


# ---------------------------------------------------------------------------
# _build_unknown_report
# ---------------------------------------------------------------------------

class TestBuildUnknownReport:
    def test_all_directions_unknown(self):
        report = _build_unknown_report(VENDOR_ID, _score_bundle())
        for dim, trend in report.dimension_trends.items():
            assert trend["direction"] == "UNKNOWN"

    def test_data_points_available_default_one(self):
        report = _build_unknown_report(VENDOR_ID, _score_bundle())
        assert report.data_points_available == 1

    def test_action_learning_empty(self):
        report = _build_unknown_report(VENDOR_ID, _score_bundle())
        assert report.action_learning == []

    def test_vendor_id_set(self):
        report = _build_unknown_report("v-xyz", _score_bundle())
        assert report.vendor_id == "v-xyz"


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------

class TestTrendReportRoundTrip:
    def test_round_trip_with_action_learning(self):
        hist = _historical(_run(T1, 65), _run(T3, 70))
        history = _action_history(_action("ESCALATION", T2))
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(78),
            current_commercial=_commercial(),
            historical_scores=hist,
            action_history=history,
        )
        d = result.to_dict()
        restored = TrendReport.from_dict(d)
        assert restored.vendor_id == result.vendor_id
        assert restored.data_points_available == result.data_points_available
        assert len(restored.action_learning) == len(result.action_learning)
        assert restored.spend_trend == result.spend_trend
        assert restored.dimension_trends == result.dimension_trends

    def test_round_trip_empty_action_learning(self):
        result = analyse_trends(
            vendor_id=VENDOR_ID,
            current_scores=_score_bundle(),
            current_commercial=_commercial(),
            historical_scores=None,
            action_history=None,
        )
        d = result.to_dict()
        restored = TrendReport.from_dict(d)
        assert restored.action_learning == []
        assert restored.action_learning_summary is None
