"""Tool 2 (Process 4) — scoring_engine.

Converts Q&A answers and commercial metrics into a CRI score and five dimension
scores. Fully deterministic arithmetic — no LLM calls, no file writes.

Returns ScoreBundle in memory. Consumed by finding_engine downstream.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from cobalt.models.schemas.an_schema import (
    CommercialAnalysisResult,
    DimensionScore,
    HistoricalScoreState,
    QAPair,
    ScoreBundle,
    ScoringConfig,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

QA_TO_SCORE: dict[tuple[str, str], int] = {
    ("COMPLETE",     "HIGH"):   92,
    ("COMPLETE",     "MEDIUM"): 78,
    ("PARTIAL",      "HIGH"):   62,
    ("PARTIAL",      "MEDIUM"): 48,
    ("PARTIAL",      "LOW"):    35,
    ("UNANSWERABLE", "HIGH"):   25,
    ("UNANSWERABLE", "MEDIUM"): 20,
    ("UNANSWERABLE", "LOW"):    15,
}

DEFAULT_SCORE_WHEN_NO_ANSWER: int = 20

DIMENSION_QUESTIONS: dict[str, list[str]] = {
    "delivery_reliability": ["Q1"],
    "responsiveness":       ["Q2"],
    "commercial_value":     ["Q3"],
    "risk_compliance":      ["Q4"],
    "relationship_trend":   ["Q5", "Q6"],
}

COMMERCIAL_ADJUSTMENTS: dict[str, int] = {
    "LOW":       +8,
    "MEDIUM":     0,
    "HIGH":     -10,
    "CRITICAL": -20,
}

DIMENSION_WEIGHTS: dict[str, float] = {
    "delivery_reliability": 0.20,
    "responsiveness":       0.20,
    "commercial_value":     0.20,
    "risk_compliance":      0.20,
    "relationship_trend":   0.20,
}

assert sum(DIMENSION_WEIGHTS.values()) == 1.0, "DIMENSION_WEIGHTS must sum to 1.0"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _score_dimension(dimension: str, qa_pairs: list[QAPair]) -> int:
    """Compute raw score for one dimension from its mapped Q&A pairs."""
    question_ids = DIMENSION_QUESTIONS.get(dimension, [])
    relevant = [p for p in qa_pairs if p.question_id in question_ids]

    if not relevant:
        return DEFAULT_SCORE_WHEN_NO_ANSWER

    scores = [
        QA_TO_SCORE.get((p.completeness, p.confidence), DEFAULT_SCORE_WHEN_NO_ANSWER)
        for p in relevant
    ]
    return round(sum(scores) / len(scores))


def _apply_commercial_adjustment(base_score: int, commercial_risk: str) -> int:
    """Apply commercial risk adjustment to Commercial Value dimension score."""
    adjustment = COMMERCIAL_ADJUSTMENTS.get(commercial_risk, 0)
    return max(0, min(100, base_score + adjustment))


def _compute_cri(dim_scores: dict[str, int]) -> int:
    """Compute CRI as weighted average of dimension scores, clamped to [0, 100]."""
    raw = sum(dim_scores[dim] * DIMENSION_WEIGHTS[dim] for dim in DIMENSION_WEIGHTS)
    return max(0, min(100, round(raw)))


def _health_band(cri: int) -> str:
    """Classify CRI into health band."""
    if cri >= 80:
        return "HEALTHY"
    if cri >= 65:
        return "WATCH"
    if cri >= 50:
        return "AT_RISK"
    return "CRITICAL"


def _trend_direction(delta: int) -> str:
    """Classify per-dimension trend direction from delta."""
    if delta > 3:
        return "IMPROVING"
    if delta < -3:
        return "DECLINING"
    return "STABLE"


def _compute_deltas(
    current_dim_scores: dict[str, int],
    historical_scores: HistoricalScoreState | None,
) -> tuple[int | None, int | None, dict[str, dict]]:
    """Return (prior_cri, cri_delta, per_dimension_prior_data).

    per_dimension_prior_data keys: dimension → {prior_score, delta, trend_direction}
    """
    if not historical_scores or not historical_scores.runs:
        return None, None, {}

    last_run = historical_scores.runs[-1]
    prior_cri: int = last_run["cri_score"]
    prior_dim: dict = last_run.get("dimension_scores", {})

    dim_data: dict[str, dict] = {}
    for dim, current_score in current_dim_scores.items():
        prior_score = prior_dim.get(dim)
        if prior_score is not None:
            delta = current_score - prior_score
            dim_data[dim] = {
                "prior_score": prior_score,
                "delta": delta,
                "trend_direction": _trend_direction(delta),
            }

    return prior_cri, None, dim_data   # cri_delta computed by caller


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_scores(
    vendor_id: str,
    qa_pairs: list[QAPair],
    commercial_result: CommercialAnalysisResult,
    rs_profile: object,
    historical_scores: HistoricalScoreState | None,
    scoring_config: ScoringConfig,
) -> ScoreBundle:
    """Compute CRI score and five dimension scores for one vendor.

    Deterministic arithmetic — no LLM calls, no file writes.
    """
    now = _now_iso()

    # --- Score each dimension ---
    raw_dim_scores: dict[str, int] = {}
    for dim in DIMENSION_WEIGHTS:
        raw_dim_scores[dim] = _score_dimension(dim, qa_pairs)

    # Apply commercial adjustment to commercial_value dimension
    raw_dim_scores["commercial_value"] = _apply_commercial_adjustment(
        raw_dim_scores["commercial_value"],
        commercial_result.commercial_risk_level,
    )

    # --- CRI and health band ---
    cri_score = _compute_cri(raw_dim_scores)
    band = _health_band(cri_score)

    # --- Historical deltas ---
    prior_cri, _, dim_data = _compute_deltas(raw_dim_scores, historical_scores)
    cri_delta: int | None = None
    if prior_cri is not None:
        cri_delta = cri_score - prior_cri

    # --- Build DimensionScore objects ---
    dimension_scores: list[DimensionScore] = []
    for dim in DIMENSION_WEIGHTS:
        score = raw_dim_scores[dim]
        d = dim_data.get(dim, {})
        dimension_scores.append(DimensionScore(
            dimension=dim,
            score=score,
            prior_score=d.get("prior_score"),
            delta=d.get("delta"),
            trend_direction=d.get("trend_direction"),
        ))

    # --- Operational metrics ---
    operational_metrics: dict = {
        "sla_compliance_pct":   commercial_result.sla_adherence_pct,
        "avg_response_time":    None,   # V2
        "issue_resolution_rate": None,  # V2
        "open_findings_count":   0,     # filled by orchestrator after finding_engine
        "open_actions_count":    0,
    }

    logger.debug(
        "[scoring_engine] vendor=%r cri=%d band=%s delta=%s",
        vendor_id, cri_score, band, cri_delta,
    )

    return ScoreBundle(
        vendor_id=vendor_id,
        cri_score=cri_score,
        prior_cri=prior_cri,
        cri_delta=cri_delta,
        health_band=band,
        dimension_scores=dimension_scores,
        operational_metrics=operational_metrics,
        portfolio_rank=None,
        category_rank=None,
        scored_at=now,
    )
