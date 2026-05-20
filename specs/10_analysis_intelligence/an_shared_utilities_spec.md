# an_shared_utilities (Process 4 Core Skills)

## Overview

**Files:**
- `src/cobalt/core/pcs.py`
- `src/cobalt/core/triage.py`
- `src/cobalt/core/state_classifier.py`

**Role:** Three stateless utility modules required by P4 tools. No LLM. No file I/O.
Pure functions only. Live in `src/cobalt/core/` alongside existing skills.

**Note:** `confidence_scorer.py`, `gap_analyzer.py`, and `staleness.py` already exist
from the P3 build. Do not recreate them. Import from them directly.

---

## `pcs.py`

Computes the P4 contribution to the Profile Confidence Score.

### Public API

```python
def compute_pcs(
    pcs_before: float,
    flags: list[str],
    process: str = "P4",
) -> tuple[float, float]:
    """
    Compute P4 PCS contribution and updated total.

    P4 maximum contribution: 0.10

    Components (additive):
      +0.05  if "CRI_COMPUTED" in flags
      +0.03  if "FINDINGS_DETECTED" in flags
      +0.02  if "ALL_DIMS_SCORED" in flags

    Returns: (pcs_contribution, pcs_total)
    pcs_total = min(1.0, pcs_before + pcs_contribution)
    """
```

**Flags the orchestrator sets before calling:**
- `"CRI_COMPUTED"` — set when scoring_engine produces a non-None cri_score
- `"FINDINGS_DETECTED"` — set when finding_engine produces >= 1 finding
- `"ALL_DIMS_SCORED"` — set when all 5 dimension scores are HIGH or MEDIUM

### Tests required — tests/core/test_pcs.py

- All three flags present → contribution = 0.10, total = min(1.0, pcs_before + 0.10)
- Only CRI_COMPUTED flag → contribution = 0.05
- No flags → contribution = 0.0, total = pcs_before
- pcs_before=0.95, all flags → pcs_total=1.0 (clamped, not 1.05)
- pcs_before=0.0, all flags → pcs_total=0.10

---

## `triage.py`

Builds structured triage task records for BLOCKING gaps detected by AN-06.

### Public API

```python
from datetime import datetime, timedelta

def generate_triage_tasks(
    gaps: list[dict],
    flags: list[str],
    vendor_id: str,
    programme_id: str,
    default_owner: str | None = None,
    due_days_blocking: int = 7,
    due_days_enrichment: int = 30,
) -> list[dict]:
    """
    Returns list of triage task dicts. One task per BLOCKING gap only.
    ENRICHMENT gaps are not included.

    Each task dict contains:
      triage_type, description, question, severity,
      vendor_id, programme_id, due_date (ISO string), recommended_owner
    """

def build_triage_task(
    triage_type: str,
    description: str,
    question: str,
    severity: str,
    vendor_id: str,
    programme_id: str,
    due_date: str,
    recommended_owner: str | None = None,
) -> dict:
    """Construct a single triage task dict."""
```

**Gap dict structure expected by generate_triage_tasks:**
```python
{"severity": "BLOCKING", "description": "...", "suggested_action": "..."}
```

**Recommended owner inference:**
- Gap description contains "compliance" or "certificate" → "compliance_owner"
- Gap description contains "contract" or "renewal" → "contract_owner"
- Gap description contains "spend" or "invoice" → "finance_owner"
- Otherwise → default_owner or "vendor_owner"

### Tests required — tests/core/test_triage.py

- One BLOCKING gap → one task with due_date = today + 7 days
- One ENRICHMENT gap → returns [] (not included)
- Mixed BLOCKING + ENRICHMENT → only BLOCKING task returned
- Empty gaps list → returns []
- Gap description contains "contract" → recommended_owner = "contract_owner"
- build_triage_task returns dict with all required keys

---

## `state_classifier.py`

Maps vendor state from CRI score and signals to a health label.
Used by the analysis orchestrator for DB sync after each run.

### Public API

```python
def classify_vendor_state(
    cri_score: float | None,
    open_findings: int,
    trend_direction: str | None,   # IMPROVING / STABLE / DECLINING / None
    renewal_days: int | None,
    flags: list[str],
) -> str:
    """
    Returns: HEALTHY / WATCH / AT_RISK / CRITICAL / UNKNOWN / ARCHIVED

    Rules evaluated in order — first match wins:
    1. "ARCHIVED" in flags                              → ARCHIVED
    2. cri_score is None                                → UNKNOWN
    3. Renewal elevation (applied after base band):
         renewal_days is not None
         AND renewal_days < 30
         AND cri_score < 70:
           elevate base band one level
           (WATCH → AT_RISK, AT_RISK → CRITICAL)
    4. cri_score >= 80 AND trend_direction != "DECLINING" → HEALTHY
    5. cri_score >= 65                                  → WATCH
    6. cri_score >= 50                                  → AT_RISK
    7. cri_score < 50                                   → CRITICAL
    """
```

**Elevation logic:** Compute base band from rules 4–7. Then if rule 3 applies, elevate by one:
- WATCH → AT_RISK
- AT_RISK → CRITICAL
- HEALTHY → WATCH (if renewal < 30d and cri < 70, HEALTHY cannot be reached since cri < 70 means at most WATCH)
- CRITICAL stays CRITICAL (already worst)

### Tests required — tests/core/test_state_classifier.py

- cri=None → UNKNOWN
- "ARCHIVED" in flags → ARCHIVED regardless of cri
- cri=85, trend=IMPROVING → HEALTHY
- cri=85, trend=DECLINING → WATCH (DECLINING overrides HEALTHY threshold)
- cri=70, trend=STABLE → WATCH
- cri=55 → AT_RISK
- cri=45 → CRITICAL
- cri=68, renewal_days=25 → AT_RISK (elevated from WATCH, rule 3 applies)
- cri=55, renewal_days=25 → CRITICAL (elevated from AT_RISK)
- cri=68, renewal_days=90 → WATCH (renewal_days >= 30 so rule 3 does not apply)
- open_findings and trend_direction do not affect base band calculation
  (they are inputs for future V2 weighting, accepted but not used in V1)
