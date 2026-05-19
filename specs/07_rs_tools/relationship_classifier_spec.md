# relationship_classifier
# FIXED: spend_ttm_usd → spend_summary.total_usd_ttm throughout (Issue 1)
# FIXED: LLM prompt uses correct field name (Issue 1)
# FIXED: CRITICAL test case corrected from 0.80 to 0.85 (Issue 5)
# FIXED: Additional test case added to confirm 0.80 = HIGH not CRITICAL (Issue 5)

## Overview

**Process:** Process 3 — Relationship & Spend Data Gathering
**Stages covered:** Stage 4 (Relationship Classification)
**File:** `src/cobalt/tools/relationship_classifier.py`
**Role:** Classify the nature and strategic importance of the vendor relationship. Score dependency
level. Uses LLM only when signals are ambiguous (dependency score 0.35–0.65).
**Writes to workspace:** No — returns `RelationshipClassification` in memory.
**Agent:** LLM call via `llm_call()` in ambiguous band only; rule-based otherwise.

---

## Purpose

Produces the relationship classification and dependency score that determines how much strategic
attention this vendor warrants. The majority of vendors are classified by deterministic rules —
LLM is reserved for the ambiguous middle band to avoid unnecessary cost and latency.

The output of this tool directly informs the `dependency_tier` and `relationship_type` fields in
the `relationship_spend_profile.md`, which are the primary signals used by the VW Agent for
scheduling and escalation decisions.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `spend_summary` | Tool 3 output | Spend totals, TTM, completeness |
| `contract_terms` | Tool 2 output | Contract coverage, value, duration, auto-renewal |
| `entity_profile` | Workspace `entity.md` | Entity type, category_hint, known flags |
| `known_facts` | Enrichment profile | category, company_size_band, strategic indicators |

---

## Output

Returns `RelationshipClassification` in memory.

```json
{
  "vendor_id": "V-XXXX-001",
  "relationship_type": "STRATEGIC",
  "dependency_score": 0.78,
  "dependency_tier": "HIGH",
  "single_source_risk": true,
  "contract_coverage": "FULLY_COVERED",
  "relationship_age_days": 1095,
  "renewal_urgency": "WATCH",
  "classification_confidence": "HIGH",
  "llm_used": false,
  "reasoning": null
}
```

---

## Skills

### 1. Rule-based dependency scoring

Scores six signals independently, applies weights, and sums to a clamped 0.0–1.0 score.

| Signal | Rule | Weight |
|---|---|---|
| Spend concentration | `spend_summary.total_usd_ttm / SPEND_NORMALISATION_CEILING_USD` (see note) | 0.25 |
| Contract coverage | FULLY_COVERED = 1.0 / PARTIALLY_COVERED = 0.5 / UNCOVERED = 0.0 | 0.20 |
| Single source risk | True = 1.0 / False = 0.0 | 0.15 |
| Contract duration | > 36 months = 1.0 / 12–36 months = 0.5 / < 12 months = 0.0 | 0.15 |
| Auto-renewal | True = 1.0 / False or unknown = 0.0 | 0.10 |
| Strategic category | IT_INFRASTRUCTURE / CORE_OPERATIONS = 1.0 / other known = 0.5 / unknown = 0.3 | 0.15 |

**Weights sum to 1.0. Final score = sum(signal × weight), clamped to [0.0, 1.0].**

**Spend concentration note:** No global spend total is available in V1. Use
`spend_summary.total_usd_ttm` directly as a proxy normalised against a module-level ceiling
constant:

```python
SPEND_NORMALISATION_CEILING_USD = 500_000
# Default suitable for SME / mid-market programmes.
# Override for enterprise programmes where routine vendors may exceed $500K TTM.
```

# FIX-1: The field on SpendSummary is total_usd_ttm — NOT spend_ttm_usd.
# Always access spend_summary.total_usd_ttm in implementation.
# The scoring note column header is labelled "spend concentration" for readability
# but the actual attribute accessed is spend_summary.total_usd_ttm.

Signal value = `min(spend_summary.total_usd_ttm / SPEND_NORMALISATION_CEILING_USD, 1.0)`.
If `spend_summary.total_usd_ttm` is `None` or zero → signal value = 0.0.

**Contract duration:** Computed from `effective_date` to `expiry_date` of the longest active
contract. If no dates → signal value = 0.0.

**Strategic category:** Derived from `known_facts.category` if available; else
`entity_profile.category_hint`.

### 2. Relationship type and dependency tier

**Type from score:**

| Score range | Relationship type |
|---|---|
| ≥ 0.70 | STRATEGIC |
| 0.50 – 0.69 | PREFERRED |
| 0.30 – 0.49 | TRANSACTIONAL |
| < 0.30 | INCIDENTAL |
| No spend AND no contracts | UNKNOWN |

**Dependency tier from score:**

# FIX-5: CRITICAL requires score ≥ 0.85. Test case using 0.80 was wrong — corrected in tests.

| Tier | Condition |
|---|---|
| CRITICAL | score ≥ 0.85 AND `single_source_risk = True` |
| HIGH | score ≥ 0.70 (or score ≥ 0.60 if `single_source_risk = True`) |
| MEDIUM | score 0.40 – 0.69 (not already HIGH) |
| LOW | score < 0.40 |
| (none set) | `relationship_type = UNKNOWN` |

**Tier evaluation order:** Check CRITICAL first, then HIGH, then MEDIUM, then LOW. Once a tier
matches, stop. A score of 0.80 with `single_source_risk = True` does NOT reach CRITICAL because
0.80 < 0.85. It lands on HIGH (score ≥ 0.70).

### 3. LLM classification (ambiguous band only)

**Triggers only when:** Rule-based score is in the range 0.35–0.65 (inclusive) AND
`spend_summary.data_completeness` is not `NONE`.

Outside this band → rule-based result is used directly, `llm_used = False`.

**LLM call:** One `llm_call()` via `src/cobalt/core/llm_call.py`. Model: `gpt-4o`, temperature 0,
max_tokens 200.

**Prompt template:**

```
You are classifying a vendor relationship for procurement risk management.
Based on the following signals, classify the relationship as exactly one of:
STRATEGIC, PREFERRED, TRANSACTIONAL, INCIDENTAL.

Signals:
- Spend TTM (USD): {spend_summary.total_usd_ttm}
- Data completeness: {spend_summary.data_completeness}
- Contract coverage: {contract_coverage}
- Contract duration (months): {duration_months}
- Auto-renews: {auto_renews}
- Single source risk: {single_source_risk}
- Vendor category: {category}
- Rule-based dependency score: {score:.2f}

Definitions:
- STRATEGIC: Core to operations; hard to replace; significant spend or critical function
- PREFERRED: Important; regularly used; some alternatives exist
- TRANSACTIONAL: Routine; easily replaceable; low criticality
- INCIDENTAL: Occasional; marginal spend; no dependency

Respond as JSON only: {"relationship_type": "...", "reasoning": "one sentence explanation"}
```

# FIX-1: Prompt uses spend_summary.total_usd_ttm — the actual SpendSummary field name.
# Do NOT use {spend_ttm_usd} — that attribute does not exist on SpendSummary.

**LLM result:** If valid JSON returned → use `relationship_type` from LLM, set `llm_used = True`,
store `reasoning`.

**LLM failure:** If `llm_call()` raises or returns invalid JSON → fall back to rule-based
classification using midpoint of ambiguous band (score treated as 0.50 → PREFERRED).
Set `llm_used = False`. No error raised.

### 4. Contract coverage detection

Evaluates `contract_terms` list from Tool 2.

| Coverage | Rule |
|---|---|
| `FULLY_COVERED` | ≥ 1 `ContractTerms` with non-null `expiry_date` in the future (today < expiry) |
| `PARTIALLY_COVERED` | ≥ 1 `ContractTerms` present but all are expired, OR have no `expiry_date`, OR `total_value` and dates are both null |
| `UNCOVERED` | `contract_terms` list is empty |

When `document_intelligence` result has only LOW-confidence extractions → treat as UNCOVERED.

### 5. Renewal urgency detection

Scans all non-null `expiry_date` values across `contract_terms`. All comparisons relative to today.

| Urgency | Rule |
|---|---|
| `URGENT` | Any contract expires within 90 calendar days |
| `WATCH` | Any contract expires within 180 calendar days (but none within 90) |
| `OK` | All contracts expire > 180 calendar days from today |
| `UNKNOWN` | No non-null `expiry_date` found in any `ContractTerms` |

Expired contracts (expiry in the past) → do not affect urgency (they are not upcoming renewals).

### 6. Single source risk detection

Set to `True` if all of the following hold:
- `known_facts` contains no references to competitor or alternative vendor relationships
  (check for keys: `alternatives`, `competitors`, `alternative_vendors`)
- `entity_profile` has no `alternatives` field
- `spend_summary.total_usd_all_time` is not None and > 0 (only flag risk if there is real spend)

Set to `False` otherwise (i.e., alternatives detected, or no spend at all).

### 7. Relationship age

If `entity_profile` contains `first_transacted_date` or `created_at` → compute
`relationship_age_days` as days from that date to today.

If not available → `relationship_age_days = None`.

### 8. Classification confidence

| Confidence | Conditions |
|---|---|
| `HIGH` | Rule-based (not LLM); `data_completeness` = FULL or PARTIAL; ≥ 1 contract term |
| `MEDIUM` | LLM-assisted; OR rule-based with SPARSE data; OR no contracts |
| `LOW` | No spend data AND no contracts; OR `relationship_type = UNKNOWN` |

---

## Flags produced

None raised by this tool — classification result fields carry all signals. Anomalies from Tool 3
are passed through unchanged by Tool 5.

---

## Routing

| Result | Next step |
|---|---|
| Any classification | Pass to Tool 5 (`rs_profile_assembler`) |
| `UNKNOWN` type (no spend, no contracts) | Tool 5 notes gap; `dependency_tier` left null in profile |
| `URGENT` renewal urgency | Tool 5 raises `CONTRACT_RENEWAL_URGENT` flag in profile |

---

## Internal structure

```python
# Module-level constants — configurable without changing spec
SPEND_NORMALISATION_CEILING_USD = 500_000
AMBIGUOUS_SCORE_LOW  = 0.35
AMBIGUOUS_SCORE_HIGH = 0.65

def classify_relationship(
    vendor_id: str,
    spend_summary: SpendSummary,
    contract_terms: list[ContractTerms],
    entity_profile: dict,
    known_facts: dict,
) -> RelationshipClassification:

def _score_dependency(spend_summary, contract_terms, entity_profile, known_facts) -> float
    # FIX-1: Uses spend_summary.total_usd_ttm — not spend_ttm_usd

def _classify_type_from_score(score: float, spend_ttm: float | None) -> str
def _classify_tier_from_score(score: float, single_source: bool) -> str
def _detect_contract_coverage(contract_terms: list[ContractTerms]) -> str
def _detect_renewal_urgency(contract_terms: list[ContractTerms]) -> str
def _check_single_source(entity_profile: dict, known_facts: dict, spend_summary: SpendSummary) -> bool
def _llm_classify(vendor_id: str, signals: dict) -> tuple[str, str] | None
def _compute_classification_confidence(score, spend_summary, contract_terms, llm_used) -> str
```

---

## Tests required

# FIX-5: Score 0.85 (not 0.80) is required for CRITICAL tier.
# Added explicit test that 0.80 produces HIGH, not CRITICAL.

- Score 0.85, `single_source_risk = True` → `dependency_tier = CRITICAL`
- Score 0.80, `single_source_risk = True` → `dependency_tier = HIGH` (0.80 < 0.85, does not reach CRITICAL)
- Score 0.75, `single_source_risk = False` → `dependency_tier = HIGH`, `relationship_type = STRATEGIC`
- Score 0.55 (ambiguous band) + valid completeness → LLM called exactly once
- Score 0.55 (ambiguous band) + LLM returns `STRATEGIC` → `relationship_type = STRATEGIC`, `llm_used = True`
- Score 0.70 (outside ambiguous band) → LLM not called, `llm_used = False`
- Score 0.15 → `relationship_type = INCIDENTAL`, `dependency_tier = LOW`, no LLM call
- LLM failure in ambiguous band → rule-based fallback, `llm_used = False`, no raise
- `data_completeness = NONE` + ambiguous score → LLM not called (condition not met)
- `contract_terms` empty → `contract_coverage = UNCOVERED`
- `expiry_date = today + 60 days` → `renewal_urgency = URGENT`
- `expiry_date = today + 150 days` → `renewal_urgency = WATCH`
- `expiry_date = today + 200 days` → `renewal_urgency = OK`
- All `expiry_date` null → `renewal_urgency = UNKNOWN`
- Expired contract (`expiry_date` in past) → does not set urgency
- `known_facts` has `alternatives` key → `single_source_risk = False`
- `spend_summary.total_usd_all_time = None` → `single_source_risk = False` (no real spend)
- No spend AND no contracts → `relationship_type = UNKNOWN`, `dependency_tier` not set
- `_score_dependency` accesses `spend_summary.total_usd_ttm` — AttributeError if `spend_ttm_usd` used instead
