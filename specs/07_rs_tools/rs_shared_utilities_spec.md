# rs_shared_utilities (Process 3 Core Utilities)
# FIXED: gap_analyzer CRITICAL severity removed — max is MAJOR (Issue 4)
# gap_analyzer tests updated to reflect MAJOR as worst case

## Overview

**Files:**
- `src/cobalt/core/name_matching.py`
- `src/cobalt/core/confidence_scorer.py`
- `src/cobalt/core/gap_analyzer.py`
- `src/cobalt/core/staleness.py`

**Role:** Four stateless utility modules shared across all P3 tools. No LLM calls. No external
calls. No file I/O. Pure functions only.

---

## Purpose

These utilities are built first (Phase 0) because every P3 tool depends on them. They live in
`src/cobalt/core/` per the project directory convention — shared infrastructure belongs in core,
not inside tool files.

---

## `name_matching.py`

Fuzzy vendor name matching. Used by `structured_data_collector` to match spreadsheet rows to the
target vendor and by document intelligence for candidate deduplication.

**Source:** Reuses the Jaro-Winkler logic extracted from `entity_resolution.py` (lines ~80–120).
Extracts — does not copy — to avoid drift.

**Side effect on existing code:** `external_source_collector.py` contains private functions
`_strip_suffixes()` and `_strip_corporate_suffixes()` that duplicate what `normalise_for_match()`
will do. When implementing `name_matching.py`, also update `external_source_collector.py` to import
`normalise_for_match` from this module and delete those two private functions. Run the full test
suite after — must stay at existing passing count.

### Public API

```python
def fuzzy_match(name_a: str, name_b: str) -> float:
    """
    Jaro-Winkler similarity between two strings.
    Returns 0.0–1.0. Case-insensitive. Operates on normalised inputs.
    """

def normalise_for_match(name: str) -> str:
    """
    Prepare a name for matching:
    1. Lowercase
    2. Strip legal suffixes: Ltd, Limited, Inc, Corp, LLC, GmbH, S.A., Plc, Co., etc.
    3. Collapse whitespace and strip leading/trailing spaces
    4. Remove punctuation except hyphens between words
    """

def best_match(
    query: str,
    candidates: list[str],
    threshold: float = 0.85,
) -> tuple[str, float] | None:
    """
    Find the best matching candidate for the query.
    Both query and candidates are normalised before comparison.
    Returns (best_candidate_original_string, score) or None if best score < threshold.
    """
```

### Behaviour notes

- `fuzzy_match` always normalises both inputs before comparing.
- `best_match` returns the **original** (un-normalised) candidate string alongside the score —
  callers should not normalise the returned candidate.
- When `candidates` is empty → `best_match` returns `None`.
- When multiple candidates tie on score → returns the one appearing earliest in the list.

### Legal suffixes stripped by `normalise_for_match`

`Ltd`, `Limited`, `Inc`, `Incorporated`, `Corp`, `Corporation`, `LLC`, `LLP`, `Plc`, `GmbH`,
`S.A.`, `S.A.S.`, `S.r.l.`, `B.V.`, `N.V.`, `Co.`, `& Co`, `and Co`, `Holdings`, `Group`,
`International`, `Worldwide`, `Global`

Suffix stripping is recursive (e.g. `"Acme Corp Ltd"` → strip `Ltd` → strip `Corp` → `"Acme"`).

### Tests required

- `fuzzy_match("Microsoft", "Microsoft")` → 1.0
- `fuzzy_match("Microsoft", "Microsft")` → > 0.90 (typo tolerance)
- `fuzzy_match("Acme Ltd", "Acme Inc")` → after normalisation: `"acme"` vs `"acme"` → 1.0
- `normalise_for_match("Acme Corporation Ltd")` → `"acme"`
- `normalise_for_match("  IBM Global Services  ")` → `"ibm"`
- `best_match("Google", ["Alphabet Inc", "Google LLC", "Microsoft Corp"], threshold=0.85)`
  → `("Google LLC", 1.0)`
- `best_match("Xyz", ["Apple", "Amazon"], threshold=0.85)` → `None`
- `best_match("", [])` → `None`

---

## `confidence_scorer.py`

Per-field confidence scoring based on source trust and corroboration. Used by `spend_aggregator`
and `rs_profile_assembler`.

### Public API

```python
def score_field(
    value: Any,
    source_trust: str,          # TrustLevel value: OFFICIAL / SYSTEM_EXPORT / USER_SUBMITTED
    corroborating_sources: int, # How many independent sources agree on this value
    age_days: int,              # Days since this value was last verified
) -> str:
    """
    Return a confidence level string: HIGH / MEDIUM / LOW / MISSING.

    Rules:
    - value is None or empty → MISSING (regardless of other params)
    - OFFICIAL trust + ≥ 2 corroborating sources + age < 90 days → HIGH
    - OFFICIAL trust + 1 source OR age 90–365 days → MEDIUM
    - SYSTEM_EXPORT trust + ≥ 1 source + age < 180 days → MEDIUM
    - USER_SUBMITTED trust → LOW (always, regardless of corroboration)
    - age > 365 days → cap at LOW regardless of trust or corroboration
    """

def aggregate_confidence(levels: list[str]) -> str:
    """
    Lowest-wins aggregation.
    Precedence (worst to best): MISSING > LOW > INFERRED > MEDIUM > HIGH
    Returns the worst level present in the list.
    Empty list → MISSING.
    """
```

### Trust hierarchy

OFFICIAL > SYSTEM_EXPORT > USER_SUBMITTED

USER_SUBMITTED data is always LOW confidence by design — this reflects that vendor self-reported
data and uploaded spreadsheets have not been independently verified.

### Tests required

- `score_field(None, "OFFICIAL", 3, 30)` → `MISSING`
- `score_field("value", "OFFICIAL", 2, 30)` → `HIGH`
- `score_field("value", "OFFICIAL", 1, 30)` → `MEDIUM`
- `score_field("value", "SYSTEM_EXPORT", 1, 60)` → `MEDIUM`
- `score_field("value", "USER_SUBMITTED", 5, 1)` → `LOW`
- `score_field("value", "OFFICIAL", 3, 400)` → `LOW` (age cap)
- `aggregate_confidence(["HIGH", "MEDIUM", "LOW"])` → `LOW`
- `aggregate_confidence(["HIGH", "HIGH"])` → `HIGH`
- `aggregate_confidence([])` → `MISSING`
- `aggregate_confidence(["MISSING", "HIGH"])` → `MISSING`

---

## `gap_analyzer.py`

Analyses a profile dict for missing, low-confidence, and stale fields. Produces a `GapReport`.
Used by `rs_profile_assembler`.

**`GapReport` dataclass is defined in `rs_schema.py`, not here.** This module imports it:

```python
from cobalt.models.schemas.rs_schema import GapReport
```

This placement avoids a circular import: `rs_schema.py` → no core imports; `gap_analyzer.py` →
imports `GapReport` from `rs_schema.py`.

### Public API

```python
from cobalt.models.schemas.rs_schema import GapReport

def analyse_gaps(
    profile_dict: dict,
    required_fields: list[str],
    age_thresholds: dict,        # {field_name: max_age_days}
) -> GapReport:
    """
    Inspect a profile dict and classify every missing/weak field.

    Checks:
    1. Missing: field not in dict OR value is None
    2. Low confidence: field present but confidence sub-key = LOW or MISSING
    3. Stale: field has a timestamp sub-key older than max_age_days in age_thresholds

    Returns a GapReport with gap_severity of MAJOR, MINOR, or NONE.
    CRITICAL is never produced here — it is assigned by rs_profile_assembler
    when it combines a MAJOR gap report with data_completeness = NONE.
    """
```

### Gap severity rules
# FIX-4: CRITICAL removed from gap_analyzer. Max severity from this function is MAJOR.
# rs_profile_assembler elevates MAJOR → CRITICAL when data_completeness = NONE.

| Severity | Condition |
|---|---|
| `MAJOR` | ≥ 1 required field is missing (field not in dict OR value is None) |
| `MINOR` | No required fields missing; ≥ 1 low-confidence or stale field present |
| `NONE` | No missing, low-confidence, or stale fields |

**Why CRITICAL is not produced here:** `gap_analyzer` only sees field presence and confidence. It
does not have access to `data_completeness`. The assembler combines both signals to decide CRITICAL.
Keeping severity determination in one place (the assembler) prevents inconsistency.

### Recommended actions

Generated automatically based on findings:

| Finding | Recommended action |
|---|---|
| `spend_total_ttm_usd` missing | "Upload AP extract or connect ERP system" |
| `contract_count = 0` | "Upload contract documents for extraction" |
| `relationship_type = UNKNOWN` | "Provide spend data or contract documents to enable classification" |
| `dependency_tier` missing | "Classification incomplete — add spend or contract data" |
| Any stale field | "Re-run Process 3 to refresh spend and contract data" |

### Tests required

- `required_fields = ["spend_total_ttm_usd"]`; profile has `spend_total_ttm_usd = None`
  → `gap_severity = MAJOR`, `missing_fields = ["spend_total_ttm_usd"]`
- Profile has all required fields at HIGH confidence → `gap_severity = NONE`
- Field with confidence `LOW` → appears in `low_confidence_fields`, `gap_severity = MINOR`
- Stale field (last_updated older than threshold) → in `stale_fields`
- `recommended_actions` not empty when `missing_fields` not empty
- `analyse_gaps` returns `GapReport` instance (imported from `rs_schema`)
- `gap_severity` is never `CRITICAL` — `analyse_gaps` only returns MAJOR / MINOR / NONE
- Two required fields both missing → `gap_severity = MAJOR` (not CRITICAL — CRITICAL is assembler)
- `analyse_gaps` result has correct `gap_severity = MAJOR` when required field missing
- Round-trip test for `GapReport` lives in `test_rs_schema.py` (not here)

---

## `staleness.py`

Date-based freshness checks. Used by the orchestrator gate check and `gap_analyzer`.

### Public API

```python
def is_stale(last_updated_iso: str | None, max_age_days: int) -> bool:
    """
    Return True if:
    - last_updated_iso is None (never updated = always stale)
    - OR the timestamp is older than max_age_days calendar days from today
    """

def days_since(iso_timestamp: str | None) -> int | None:
    """
    Return number of full calendar days elapsed since iso_timestamp.
    Returns None if iso_timestamp is None.
    """

def staleness_tier(days: int | None) -> str:
    """
    Categorise freshness:
    - None → UNKNOWN
    - 0–30 → FRESH
    - 31–90 → AGEING
    - > 90 → STALE
    """
```

### Timestamp format

Accepts ISO 8601 strings: `"2025-10-01"` (date) or `"2025-10-01T14:00:00Z"` (datetime). Both are
parsed correctly. Timezone-naive datetimes compared as UTC.

### Tests required

- `is_stale(None, 30)` → `True`
- `is_stale("2025-01-01", 30)` on a date 29 days later → `False`
- `is_stale("2025-01-01", 30)` on a date 31 days later → `True`
- `days_since(None)` → `None`
- `days_since("2025-01-01")` called on `2025-01-15` → `14`
- `staleness_tier(None)` → `"UNKNOWN"`
- `staleness_tier(15)` → `"FRESH"`
- `staleness_tier(60)` → `"AGEING"`
- `staleness_tier(100)` → `"STALE"`
