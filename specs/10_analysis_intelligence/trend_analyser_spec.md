# trend_analyser (AN-04)

## Overview

**Process:** Process 4 — Analysis & Intelligence
**Stage:** Stage 5 — Trend Analysis
**File:** `src/cobalt/tools/trend_analyser.py`
**Role:** Time series analysis on scores, spend, SLA metrics, and sentiment over time.
Detects trends, velocity, inflection points, and patterns. Learns from prior actions.
**Writes to workspace:** No — returns `TrendReport` in memory.
**LLM:** Conditional — one call for action learning insight when >= 3 completed actions.

---

## Purpose

Feeds AN-06 finding_engine with trend-based signals and urgency modifiers. Feeds
AN-07 narrative_engine with historical context. Requires historical state written by
the orchestrator after prior runs. On first run, returns all UNKNOWN — this is correct.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `current_scores` | AN-02 output | Current CRI and dimension scores |
| `current_commercial` | AN-03 output | Current commercial metrics |
| `historical_scores` | Prior runs (optional) | Prior CRI and dimension score series |
| `action_history` | Prior actions (optional) | Actions taken and their outcomes |

---

## Output

Returns `TrendReport` in memory.

**First-run output (no historical state):**
```json
{
  "vendor_id": "V-XXXX-001",
  "dimension_trends": {
    "delivery_reliability": {"direction": "UNKNOWN", "velocity": null,
                              "inflection_point": null, "pattern": "UNKNOWN"},
    ...
  },
  "action_learning": [],
  "action_learning_summary": null,
  "spend_trend": {"direction": "UNKNOWN", "velocity": null, "yoy_delta": null},
  "sla_trend": {"response_time_direction": "UNKNOWN", "breach_rate_direction": "UNKNOWN"},
  "sentiment_trend": {"direction": "UNKNOWN", "last_signal_date": null},
  "trend_computed_at": "2025-10-01T14:00:00Z",
  "data_points_available": 1
}
```

---

## Skills

### 1. Data availability check

```python
runs = historical_scores.runs if historical_scores else []
total_points = len(runs) + 1   # +1 for current run
data_points_available = total_points

if total_points < 2:
    # Return all-UNKNOWN TrendReport immediately
    return _build_unknown_report(vendor_id, current_scores)
```

### 2. Per-dimension trend (requires >= 2 data points)

For each dimension in DIMENSION_WEIGHTS keys:
```python
# Build score series: [oldest, ..., most_recent, current]
historical_dim_scores = [
    run["dimension_scores"].get(dimension, 20)
    for run in runs
    if "dimension_scores" in run
]
scores = historical_dim_scores + [current_dim_score]

# Velocity: points per month
oldest_date = runs[0]["run_at"]
months_elapsed = _months_between(oldest_date, now)
velocity = (scores[-1] - scores[0]) / months_elapsed if months_elapsed > 0 else 0

# Direction
if velocity > 3:    direction = "IMPROVING"
elif velocity < -3: direction = "DECLINING"
else:               direction = "STABLE"
```

### 3. Inflection point detection (requires >= 3 data points)

For each dimension, iterate through consecutive (score_n-1, score_n, score_n+1) triplets:
```python
prev_delta = scores[i] - scores[i-1]
curr_delta = scores[i+1] - scores[i]

# Direction reversal: was improving now declining, or vice versa
if (prev_delta > 3 and curr_delta < -3) or (prev_delta < -3 and curr_delta > 3):
    inflection_point = runs[i]["run_at"]   # date of the run where reversal occurred
```

### 4. Pattern detection (requires >= 3 data points)

```python
# CYCLICAL: direction alternates IMPROVING/DECLINING 2+ times
# ACCELERATING: |velocity| of recent half > |velocity| of older half
# SEASONAL: same quarter shows highest score across 2+ years
# STEADY: consistent direction, velocity change < 20% between consecutive pairs
# UNKNOWN: < 3 points or no pattern matches
```

### 5. Action learning (deterministic correlation)

For each action in `action_history.actions` (if not None):
```python
# Find score run just before action_taken_at
before_runs = [r for r in runs if r["run_at"] < action["taken_at"]]
after_runs  = [r for r in runs if r["run_at"] > action["taken_at"]]

if before_runs and after_runs:
    before_score = before_runs[-1]["cri_score"]
    after_score  = after_runs[0]["cri_score"]
    delta = after_score - before_score
    
    if delta > 5:    outcome_label = "IMPROVED"
    elif delta < -5: outcome_label = "WORSENED"
    else:            outcome_label = "NO_CHANGE"
    
    action_learning.append(ActionLearning(...))
```

### 6. Action learning insight — LLM call (only when >= 3 completed ActionLearning items)

When `len(action_learning) >= 3`:

One `llm_call()` model=gpt-4o, temperature=0, max_tokens=150:

```
SYSTEM: "You are a procurement analyst summarising what vendor management actions work.
         Write 1-2 sentences only. Return JSON only."

USER: "Actions taken for vendor {vendor_id}:
       {[{action_type, outcome_label, delta} for a in action_learning]}

       Return: {\"summary\": \"1-2 sentence insight on which action types improve performance\"}"
```

On LLM failure → `action_learning_summary = None`, no raise.

---

## Internal structure

```python
def analyse_trends(
    vendor_id: str,
    current_scores: ScoreBundle,
    current_commercial: CommercialAnalysisResult,
    historical_scores: HistoricalScoreState | None,
    action_history: ActionOutcomeHistory | None,
) -> TrendReport:

def _build_unknown_report(vendor_id, current_scores) -> TrendReport
def _compute_dimension_trend(scores: list[int], run_dates: list[str]) -> dict
def _detect_inflection(scores: list[int], run_dates: list[str]) -> str | None
def _detect_pattern(scores: list[int]) -> str
def _compute_action_learning(action_history, runs) -> list[ActionLearning]
def _months_between(date_str_a: str, date_str_b: str) -> float
```

---

## Tests required — tests/tools/test_trend_analyser.py

- historical_scores=None → all directions=UNKNOWN, data_points_available=1, no crash
- action_history=None → action_learning=[], no crash
- 1 prior run (2 total points) → direction computable, pattern=UNKNOWN
- 2 prior runs (3 total), CRI sequence 65→70→78 → direction=IMPROVING
- 2 prior runs (3 total), CRI sequence 78→70→62 → direction=DECLINING
- velocity > 3 → direction=IMPROVING; velocity -2 → direction=STABLE
- 3 runs alternating scores → pattern=CYCLICAL
- action_history with 2 completed actions → no LLM call (below threshold)
- action_history with 3 completed actions → LLM called for insight
- LLM insight fails → action_learning_summary=None, no crash
- ActionLearning delta > 5 → outcome_label=IMPROVED
- ActionLearning delta -7 → outcome_label=WORSENED
- ActionLearning delta 2 → outcome_label=NO_CHANGE
- inflection detected when direction reverses between consecutive runs
