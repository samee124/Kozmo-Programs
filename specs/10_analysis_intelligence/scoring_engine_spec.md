# scoring_engine (AN-02)

## Overview

**Process:** Process 4 — Analysis & Intelligence
**Stage:** Stage 4 — Scoring
**File:** `src/cobalt/tools/scoring_engine.py`
**Role:** Computes multi-dimensional vendor scores. Converts Q&A answers into dimension
scores. Computes CRI as weighted composite. Classifies vendor into health band.
**Writes to workspace:** No — returns `ScoreBundle` in memory.
**LLM:** None. Fully deterministic arithmetic.

---

## Purpose

Converts structured Q&A answers and commercial metrics into a single CRI score and
five dimension scores. Deterministic — same inputs always produce the same scores.
Scores feed AN-06 finding_engine for threshold-based finding detection.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `qa_pairs` | AN-05 output | Answers feed dimension scoring |
| `commercial_result` | AN-03 output | Adjusts Commercial Value dimension |
| `rs_profile` | RS-05 output | Relationship type for tier thresholds |
| `historical_scores` | Prior run (optional) | Prior CRI for delta computation |
| `scoring_config` | Config | Dimension weights and band thresholds |

---

## Output

Returns `ScoreBundle` in memory.

---

## Module-level constants

```python
# Q&A answer → raw dimension score
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
DEFAULT_SCORE_WHEN_NO_ANSWER = 20

# Which question_ids feed each dimension
DIMENSION_QUESTIONS: dict[str, list[str]] = {
    "delivery_reliability": ["Q1"],
    "responsiveness":       ["Q2"],
    "commercial_value":     ["Q3"],
    "risk_compliance":      ["Q4"],
    "relationship_trend":   ["Q5", "Q6"],
}

# Commercial risk adjustment to Commercial Value dimension
COMMERCIAL_ADJUSTMENTS: dict[str, int] = {
    "LOW":       +8,
    "MEDIUM":     0,
    "HIGH":     -10,
    "CRITICAL": -20,
}

# CRI dimension weights — must sum to 1.0
DIMENSION_WEIGHTS: dict[str, float] = {
    "delivery_reliability": 0.20,
    "responsiveness":       0.20,
    "commercial_value":     0.20,
    "risk_compliance":      0.20,
    "relationship_trend":   0.20,
}
```

---

## Skills

### 1. Dimension scoring

For each dimension in DIMENSION_QUESTIONS:
  questions = DIMENSION_QUESTIONS[dimension]
  relevant_pairs = [p for p in qa_pairs if p.question_id in questions]
  
  If no relevant pairs found:
    score = DEFAULT_SCORE_WHEN_NO_ANSWER
  Else:
    scores = [QA_TO_SCORE.get((p.completeness, p.confidence), DEFAULT_SCORE_WHEN_NO_ANSWER)
              for p in relevant_pairs]
    score = round(sum(scores) / len(scores))   # average if multiple questions

### 2. Commercial Value dimension adjustment

After computing base score from Q3:
```python
adjustment = COMMERCIAL_ADJUSTMENTS.get(commercial_result.commercial_risk_level, 0)
commercial_value_score = max(0, min(100, base_score + adjustment))
```

### 3. CRI computation

```python
cri_score = round(sum(
    dim_scores[dim] * DIMENSION_WEIGHTS[dim]
    for dim in DIMENSION_WEIGHTS
))
cri_score = max(0, min(100, cri_score))   # clamp to [0, 100]
```

### 4. Health band

```python
def _health_band(cri: int) -> str:
    if cri >= 80: return "HEALTHY"
    if cri >= 65: return "WATCH"
    if cri >= 50: return "AT_RISK"
    return "CRITICAL"
```

### 5. CRI delta and prior scores

```python
if historical_scores and historical_scores.runs:
    last_run = historical_scores.runs[-1]
    prior_cri = last_run["cri_score"]
    cri_delta  = cri_score - prior_cri
    
    # Per-dimension prior and delta
    prior_dim_scores = last_run.get("dimension_scores", {})
    # trend_direction per dimension:
    #   delta > 3  → IMPROVING
    #   delta < -3 → DECLINING
    #   else       → STABLE
else:
    prior_cri = None
    cri_delta = None
```

### 6. Operational metrics dict

Build from available Q&A evidence:
```python
operational_metrics = {
    "sla_compliance_pct":  commercial_result.sla_adherence_pct,
    "avg_response_time":   None,   # V2 — from structured_bundle when available
    "issue_resolution_rate": None, # V2
    "open_findings_count":   0,    # filled by orchestrator after finding_engine runs
    "open_actions_count":    0,    # filled by orchestrator
}
```

---

## Internal structure

```python
def compute_scores(
    vendor_id: str,
    qa_pairs: list[QAPair],
    commercial_result: CommercialAnalysisResult,
    rs_profile: "RelationshipSpendProfile",
    historical_scores: HistoricalScoreState | None,
    scoring_config: ScoringConfig,
) -> ScoreBundle:

def _score_dimension(dimension: str, qa_pairs: list[QAPair]) -> int
def _apply_commercial_adjustment(base_score: int, commercial_risk: str) -> int
def _compute_cri(dim_scores: dict[str, int]) -> int
def _health_band(cri: int) -> str
def _compute_deltas(current_scores, historical_scores) -> tuple[int | None, int | None, dict]
```

---

## Tests required — tests/tools/test_scoring_engine.py

- All Tier 1 COMPLETE + HIGH → each dimension = 92, CRI = 92, health_band=HEALTHY
- All UNANSWERABLE + LOW → each dimension = 15, CRI = 15, health_band=CRITICAL
- Q3 COMPLETE + HIGH base 92, commercial_risk=CRITICAL → commercial_value = 72 (92 - 20)
- Q3 COMPLETE + HIGH base 92, commercial_risk=LOW → commercial_value = 100 (clamped, 92 + 8)
- commercial_value clamped to [0, 100] — cannot go negative or above 100
- No historical scores → prior_cri=None, cri_delta=None
- Prior CRI=78, new CRI=65 → cri_delta=-13
- relationship_trend dimension averages Q5 and Q6 scores
- Q5 COMPLETE/HIGH (92) and Q6 PARTIAL/MEDIUM (48) → relationship_trend = round((92+48)/2) = 70
- No Q5 or Q6 in qa_pairs → relationship_trend = DEFAULT_SCORE_WHEN_NO_ANSWER = 20
- sum(DIMENSION_WEIGHTS.values()) == 1.0 (weights validation test)
- health_band thresholds: cri=80→HEALTHY, cri=79→WATCH, cri=65→WATCH, cri=64→AT_RISK, cri=50→AT_RISK, cri=49→CRITICAL
- DimensionScore.trend_direction = IMPROVING when delta > 3
- DimensionScore.trend_direction = DECLINING when delta < -3
- DimensionScore.trend_direction = STABLE when -3 <= delta <= 3
