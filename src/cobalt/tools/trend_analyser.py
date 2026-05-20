"""Tool 4 (Process 4) — trend_analyser.

Time series analysis on scores, spend, SLA metrics, and sentiment over time.
Detects trends, velocity, inflection points, and patterns. Learns from prior actions.

Writes to workspace: No — returns TrendReport in memory.
LLM: Conditional — one call for action learning insight when >= 3 completed actions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from cobalt.core.llm_call import llm_call
from cobalt.models.schemas.an_schema import (
    ActionLearning,
    ActionOutcomeHistory,
    CommercialAnalysisResult,
    HistoricalScoreState,
    ScoreBundle,
    TrendReport,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

IMPROVING_THRESHOLD: float = 3.0
DECLINING_THRESHOLD: float = -3.0
MIN_POINTS_FOR_DIRECTION: int = 2
MIN_POINTS_FOR_PATTERN: int = 3
ACTION_LEARNING_THRESHOLD: int = 3
IMPROVED_DELTA: int = 5
WORSENED_DELTA: int = -5

_DIMENSIONS = [
    "delivery_reliability",
    "responsiveness",
    "commercial_value",
    "risk_compliance",
    "relationship_trend",
]

_UNKNOWN_DIM_TREND = {
    "direction": "UNKNOWN",
    "velocity": None,
    "inflection_point": None,
    "pattern": "UNKNOWN",
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _months_between(date_str_a: str, date_str_b: str) -> float:
    """Return elapsed months between two ISO date strings. Returns 0 if unparseable."""
    try:
        def _parse(s: str) -> datetime:
            s = s.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                # Try date-only
                return datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)

        a = _parse(date_str_a)
        b = _parse(date_str_b)
        delta = b - a
        months = delta.total_seconds() / (30.44 * 24 * 3600)
        return max(0.0, months)
    except Exception:
        return 0.0


def _detect_pattern(scores: list[int]) -> str:
    """Detect trend pattern from a score series. Requires >= 3 points."""
    if len(scores) < MIN_POINTS_FOR_PATTERN:
        return "UNKNOWN"

    deltas = [scores[i + 1] - scores[i] for i in range(len(scores) - 1)]

    # CYCLICAL: direction alternates IMPROVING/DECLINING 2+ times
    signs = []
    for d in deltas:
        if d > IMPROVING_THRESHOLD:
            signs.append(1)
        elif d < DECLINING_THRESHOLD:
            signs.append(-1)
        else:
            signs.append(0)

    alternations = sum(
        1 for i in range(len(signs) - 1)
        if signs[i] != 0 and signs[i + 1] != 0 and signs[i] != signs[i + 1]
    )
    if alternations >= 2:
        return "CYCLICAL"

    # ACCELERATING: |velocity of recent half| > |velocity of older half|
    mid = len(scores) // 2
    if mid > 0 and len(scores) - mid > 0:
        older_velocity = abs(scores[mid] - scores[0]) / mid
        recent_velocity = abs(scores[-1] - scores[mid]) / (len(scores) - mid)
        if recent_velocity > older_velocity and older_velocity > 0:
            return "ACCELERATING"

    # STEADY: consistent direction, velocity change < 20% between consecutive pairs
    if len(deltas) >= 2:
        delta_magnitudes = [abs(d) for d in deltas if abs(d) > 0]
        if delta_magnitudes:
            avg = sum(delta_magnitudes) / len(delta_magnitudes)
            if avg > 0:
                max_deviation = max(abs(m - avg) / avg for m in delta_magnitudes)
                all_same_sign = all(d >= 0 for d in deltas) or all(d <= 0 for d in deltas)
                if all_same_sign and max_deviation < 0.20:
                    return "STEADY"

    return "UNKNOWN"


def _detect_inflection(scores: list[int], run_dates: list[str]) -> str | None:
    """Return the run_at date where direction reversal first occurred, or None."""
    if len(scores) < MIN_POINTS_FOR_PATTERN:
        return None

    for i in range(1, len(scores) - 1):
        prev_delta = scores[i] - scores[i - 1]
        curr_delta = scores[i + 1] - scores[i]
        if (prev_delta > IMPROVING_THRESHOLD and curr_delta < DECLINING_THRESHOLD) or \
           (prev_delta < DECLINING_THRESHOLD and curr_delta > IMPROVING_THRESHOLD):
            # run_dates includes dates for historical runs; index i corresponds to run i-1
            # scores = [hist_0, hist_1, ..., hist_n, current]
            # run_dates = [hist_0.run_at, hist_1.run_at, ..., hist_n.run_at]
            # inflection at score[i] = run_dates[i-1]  (since scores[0] = hist_0)
            if i - 1 < len(run_dates):
                return run_dates[i - 1]
            return None
    return None


def _compute_dimension_trend(
    scores: list[int],
    run_dates: list[str],
    now_str: str,
) -> dict:
    """Compute direction, velocity, inflection_point, pattern for one dimension."""
    if len(scores) < MIN_POINTS_FOR_DIRECTION:
        return dict(_UNKNOWN_DIM_TREND)

    oldest_date = run_dates[0] if run_dates else now_str
    months = _months_between(oldest_date, now_str)
    velocity = (scores[-1] - scores[0]) / months if months > 0 else 0.0

    if velocity > IMPROVING_THRESHOLD:
        direction = "IMPROVING"
    elif velocity < DECLINING_THRESHOLD:
        direction = "DECLINING"
    else:
        direction = "STABLE"

    inflection = _detect_inflection(scores, run_dates)
    pattern = _detect_pattern(scores)

    return {
        "direction": direction,
        "velocity": round(velocity, 2),
        "inflection_point": inflection,
        "pattern": pattern,
    }


def _current_dim_score(current_scores: ScoreBundle, dimension: str) -> int:
    """Extract current score for a dimension from ScoreBundle."""
    for ds in current_scores.dimension_scores:
        if ds.dimension == dimension:
            return ds.score
    return 20  # default when missing


def _compute_action_learning(
    action_history: ActionOutcomeHistory | None,
    runs: list[dict],
) -> list[ActionLearning]:
    """Correlate completed actions with CRI changes before/after."""
    if action_history is None or not runs:
        return []

    results: list[ActionLearning] = []
    for action in action_history.actions:
        taken_at = action.get("taken_at") or action.get("action_taken_at", "")
        action_type = action.get("action_type", "")

        before_runs = [r for r in runs if r.get("run_at", "") < taken_at]
        after_runs = [r for r in runs if r.get("run_at", "") > taken_at]

        if not before_runs or not after_runs:
            continue

        before_score = before_runs[-1]["cri_score"]
        after_score = after_runs[0]["cri_score"]
        delta = after_score - before_score

        if delta > IMPROVED_DELTA:
            outcome_label = "IMPROVED"
        elif delta < WORSENED_DELTA:
            outcome_label = "WORSENED"
        else:
            outcome_label = "NO_CHANGE"

        results.append(ActionLearning(
            action_type=action_type,
            action_taken_at=taken_at,
            before_score=before_score,
            after_score=after_score,
            delta=delta,
            outcome_label=outcome_label,
            insight=None,
        ))

    return results


def _llm_action_insight(
    action_learning: list[ActionLearning],
    vendor_id: str,
) -> str | None:
    """Call LLM for action learning summary. Returns None on failure."""
    system = (
        "You are a procurement analyst summarising what vendor management actions work. "
        "Write 1-2 sentences only. Return JSON only."
    )
    action_list = [
        {"action_type": a.action_type, "outcome_label": a.outcome_label, "delta": a.delta}
        for a in action_learning
    ]
    user = (
        f"Actions taken for vendor {vendor_id}:\n{action_list}\n\n"
        'Return: {"summary": "1-2 sentence insight on which action types improve performance"}'
    )
    try:
        result = llm_call(prompt=user, system=system, expect_json=True)
        if isinstance(result, dict):
            return result.get("summary")
        return None
    except Exception:
        logger.warning("[trend_analyser] LLM action insight failed for %r", vendor_id)
        return None


def _build_unknown_report(vendor_id: str, current_scores: ScoreBundle) -> TrendReport:
    """Return all-UNKNOWN TrendReport for first-run / insufficient data."""
    now = _now_iso()
    dimension_trends = {dim: dict(_UNKNOWN_DIM_TREND) for dim in _DIMENSIONS}

    return TrendReport(
        vendor_id=vendor_id,
        dimension_trends=dimension_trends,
        action_learning=[],
        action_learning_summary=None,
        spend_trend={"direction": "UNKNOWN", "velocity": None, "yoy_delta": None},
        sla_trend={"response_time_direction": "UNKNOWN", "breach_rate_direction": "UNKNOWN"},
        sentiment_trend={"direction": "UNKNOWN", "last_signal_date": None},
        trend_computed_at=now,
        data_points_available=1,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_trends(
    vendor_id: str,
    current_scores: ScoreBundle,
    current_commercial: CommercialAnalysisResult,
    historical_scores: HistoricalScoreState | None,
    action_history: ActionOutcomeHistory | None,
) -> TrendReport:
    """Analyse trends for a vendor across CRI dimensions and action outcomes.

    Returns all-UNKNOWN report on first run (no historical state).
    At most 1 LLM call — only when >= 3 completed action learning items.
    """
    now = _now_iso()

    runs = historical_scores.runs if historical_scores else []
    total_points = len(runs) + 1  # +1 for current run

    if total_points < MIN_POINTS_FOR_DIRECTION:
        report = _build_unknown_report(vendor_id, current_scores)
        report.data_points_available = total_points
        return report

    # ------------------------------------------------------------------
    # Per-dimension trend analysis
    # ------------------------------------------------------------------
    run_dates = [r.get("run_at", "") for r in runs]
    dimension_trends: dict[str, dict] = {}

    for dim in _DIMENSIONS:
        hist_scores = [
            r["dimension_scores"].get(dim, 20)
            for r in runs
            if "dimension_scores" in r
        ]
        current_dim = _current_dim_score(current_scores, dim)
        scores = hist_scores + [current_dim]

        dimension_trends[dim] = _compute_dimension_trend(scores, run_dates, now)

    # ------------------------------------------------------------------
    # Action learning (deterministic)
    # ------------------------------------------------------------------
    action_learning = _compute_action_learning(action_history, runs)

    # ------------------------------------------------------------------
    # Action learning insight — LLM (only when >= 3 items)
    # ------------------------------------------------------------------
    action_learning_summary: str | None = None
    if len(action_learning) >= ACTION_LEARNING_THRESHOLD:
        action_learning_summary = _llm_action_insight(action_learning, vendor_id)

    # ------------------------------------------------------------------
    # V1: Spend / SLA / sentiment trends are UNKNOWN (signal_processor V2)
    # ------------------------------------------------------------------
    spend_trend: dict = {"direction": "UNKNOWN", "velocity": None, "yoy_delta": None}
    sla_trend: dict = {"response_time_direction": "UNKNOWN", "breach_rate_direction": "UNKNOWN"}
    sentiment_trend: dict = {"direction": "UNKNOWN", "last_signal_date": None}

    logger.debug(
        "[trend_analyser] vendor=%r points=%d action_learning=%d",
        vendor_id, total_points, len(action_learning),
    )

    return TrendReport(
        vendor_id=vendor_id,
        dimension_trends=dimension_trends,
        action_learning=action_learning,
        action_learning_summary=action_learning_summary,
        spend_trend=spend_trend,
        sla_trend=sla_trend,
        sentiment_trend=sentiment_trend,
        trend_computed_at=now,
        data_points_available=total_points,
    )
