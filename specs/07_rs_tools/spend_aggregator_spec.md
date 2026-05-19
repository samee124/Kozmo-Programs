# spend_aggregator

## Overview

**Process:** Process 3 — Relationship & Spend Data Gathering
**Stages covered:** Stage 3 (Spend Aggregation + Anomaly Detection)
**File:** `src/cobalt/tools/spend_aggregator.py`
**Role:** Aggregate and normalise all raw spend records into decision-ready views. No LLM. Pure calculation and rule-based anomaly detection.
**Writes to workspace:** No — returns `SpendAggregationResult` in memory.
**Agent:** None — deterministic calculation only.

---

## Purpose

Transforms the flat list of `RawSpendRecord` objects from Tool 1 into structured spend summaries, period breakdowns, anomaly signals, and data quality flags. All calculation is rule-based. No LLM. No external calls.

Records that cannot be summed (null `amount_usd`) are counted toward invoice totals but excluded from USD aggregations. Records with LOW or UNMATCHED confidence are excluded from calculations but produce data quality flags.

This tool is the single source of truth for spend numbers used in relationship classification (Tool 4) and profile assembly (Tool 5).

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor attribution |
| `raw_records` | Tool 1 output | All collected spend records |
| `contract_terms` | Tool 2 output | Used for contract-vs-actual deviation check |

---

## Output

Returns `SpendAggregationResult` in memory.

```json
{
  "vendor_id": "V-XXXX-001",
  "summary": {
    "total_usd_all_time": 284000.0,
    "total_usd_ttm": 92000.0,
    "total_usd_ytd": 46000.0,
    "by_period": {
      "2024-Q3": 44000.0,
      "2024-Q4": 48000.0,
      "2025-Q1": 48000.0,
      "2025-Q2": 44000.0
    },
    "by_category": {
      "IT Services": 142000.0,
      "Consulting": 142000.0
    },
    "by_cost_centre": {
      "CC-042": 180000.0,
      "CC-011": 104000.0
    },
    "invoice_count": 24,
    "po_count": 18,
    "payment_terms_days_avg": 30,
    "data_completeness": "PARTIAL",
    "confidence": "MEDIUM"
  },
  "anomalies": [
    {
      "type": "SPEND_SPIKE",
      "description": "Q1 2025 spend 3.8x rolling 4-quarter average",
      "severity": "MEDIUM"
    }
  ],
  "data_quality_flags": ["HIGH_VALUE_UNMATCHED"],
  "aggregated_at": "2025-10-01T14:05:00Z"
}
```

---

## Skills

### 1. Record filtering

Applied before any calculation.

**Included in sums:** Records with `match_confidence` = HIGH or MEDIUM AND `amount_usd` is not None.

**Counted but excluded from sums:** Records with `amount_usd = None` (counted in `invoice_count`).

**Excluded from sums, tracked in flags:** Records with `match_confidence` = LOW or UNMATCHED (still counted in `invoice_count` if they have a parseable amount).

Records are never deleted — only classified. The full record list remains available for anomaly detection.

### 2. Period aggregation

Groups HIGH/MEDIUM records by `(year, quarter)` derived from `period_start`. If `period_start` is null, falls back to `period_end`. If both null, record contributes to all-time total only.

**Quarter derivation:** Month 1-3 → Q1, Month 4-6 → Q2, Month 7-9 → Q3, Month 10-12 → Q4.

**Period key format:** `"YYYY-QN"` (e.g., `"2025-Q1"`).

| View | Definition |
|---|---|
| TTM | Records with `period_end` (or `period_start` if no `period_end`) in the last 12 calendar months from today |
| YTD | Records with `period_start` in the current calendar year |
| All-time | All HIGH/MEDIUM records regardless of date |

Records with no date fields → included in all-time total only; no `by_period` entry.

### 3. Dimension aggregation

Groups HIGH/MEDIUM records separately by `category_raw` and `cost_centre`.

Null values grouped under `"UNCATEGORISED"`. Whitespace-only values normalised to `"UNCATEGORISED"`.

Returns dicts: `{dimension_value: sum_usd}`, sorted by value descending.

### 4. Auxiliary counts

**`invoice_count`:** Total records in input (all confidences, including null-currency). Represents number of AP lines.

**`po_count`:** Records where `po_number` is non-null and non-empty.

**`payment_terms_days_avg`:** Mean of all non-null `payment_terms_days` across HIGH/MEDIUM records. `None` if no records have this field.

### 5. Anomaly detection

Four rule-based anomaly types. All evaluated against HIGH/MEDIUM records only.

**Thresholds are module-level constants:**
```python
SPIKE_MULTIPLIER = 3.0
LARGE_INVOICE_USD = 10_000
SPEND_GAP_QUARTERS = 3
MIN_PERIODS_FOR_GAP = 6
```

| Anomaly type | Detection rule | Severity |
|---|---|---|
| `SPEND_SPIKE` | Any period's spend > `SPIKE_MULTIPLIER` × rolling 4-period average (excluding current period) | MEDIUM |
| `DUPLICATE_INVOICE` | Two or more records with identical non-null `invoice_ref` | HIGH |
| `MISSING_PO` | Record with `amount_usd ≥ LARGE_INVOICE_USD` and `po_number` is null or empty | LOW |
| `SPEND_GAP` | ≥ `SPEND_GAP_QUARTERS` consecutive calendar quarters with zero records, when total covered period ≥ `MIN_PERIODS_FOR_GAP` quarters | LOW |

**Anomaly output structure:**
```json
{
  "type": "SPEND_SPIKE",
  "description": "Q2 2025 spend $48,000 is 4.2x rolling average of $11,400",
  "severity": "MEDIUM",
  "period": "2025-Q2",
  "value_usd": 48000.0
}
```

For `DUPLICATE_INVOICE`: includes `invoice_ref` and count of duplicates.
For `MISSING_PO`: includes `invoice_ref` (if any) and `amount_usd`.
For `SPEND_GAP`: includes start and end of gap period.

### 6. Contract-vs-actual deviation

If `contract_terms` is non-empty and any `ContractTerms.total_value` is non-null:

Sum all `total_value` across contracts. Compare against `total_usd_all_time`.

If deviation exceeds 20%:
- Over-contract (actual > contract): adds anomaly `CONTRACT_DEVIATION` with `description = "Actual spend X% above contract total"`, severity HIGH.
- Under-contract (actual < 80% of contract): adds anomaly `CONTRACT_DEVIATION` with `description = "Actual spend X% below contract total"`, severity MEDIUM.

Skipped when: no contract `total_value` present; OR `total_usd_all_time` is None.

### 7. Data completeness assessment

Evaluated after all calculations complete.

| Level | Conditions |
|---|---|
| `FULL` | All HIGH/MEDIUM records have `amount_usd`; dates present on ≥ 90% of records; ≥ 2 distinct periods; ≥ 3 records total |
| `PARTIAL` | ≥ 1 record with `amount_usd = None`; OR dates missing on > 10% of records; OR only 1 distinct period |
| `SPARSE` | < 3 HIGH/MEDIUM records total |
| `NONE` | Zero records in input |

### 8. Summary confidence

Derived from `data_completeness` and source trust levels.

| Confidence | Conditions |
|---|---|
| `HIGH` | Completeness = FULL; ≥ 1 OFFICIAL or SYSTEM_EXPORT source |
| `MEDIUM` | Completeness = PARTIAL; or FULL but all USER_SUBMITTED |
| `LOW` | Completeness = SPARSE; or significant HIGH_VALUE_UNMATCHED flag |
| `NONE` | Completeness = NONE |

### 9. Data quality flags

| Flag | Condition |
|---|---|
| `NO_CURRENCY_DATA` | All records have `amount_usd = None` |
| `UNMATCHED_RECORDS_PRESENT` | Any UNMATCHED or LOW records in input |
| `HIGH_VALUE_UNMATCHED` | Any UNMATCHED record where `amount_raw` parses to > `LARGE_INVOICE_USD` |
| `DATES_MISSING` | > 50% of records have both `period_start` and `period_end` null |
| `DUPLICATE_INVOICES_FOUND` | `DUPLICATE_INVOICE` anomaly detected |
| `NO_PO_COVERAGE` | `po_count` = 0 and `invoice_count` > 0 |

---

## Routing

| Result | Next step |
|---|---|
| Any summary produced | Pass to Tool 4 (`relationship_classifier`) and Tool 5 (`rs_profile_assembler`) |
| `NONE` completeness | Pass with `data_completeness = NONE`; classifier uses `spend_ttm = 0` |
| `NO_CURRENCY_DATA` | Pass with null totals; classifier notes data gap |

---

## Internal structure

```python
def aggregate_spend(
    vendor_id: str,
    raw_records: list[RawSpendRecord],
    contract_terms: list[ContractTerms],
) -> SpendAggregationResult:

def _filter_matched(records: list[RawSpendRecord]) -> list[RawSpendRecord]
def _sum_by_period(records: list[RawSpendRecord]) -> dict
def _sum_by_dimension(records: list[RawSpendRecord], dimension: str) -> dict
def _compute_ttm(records: list[RawSpendRecord]) -> float | None
def _compute_ytd(records: list[RawSpendRecord]) -> float | None
def _detect_anomalies(records: list[RawSpendRecord], summary: SpendSummary) -> list[dict]
def _assess_completeness(records: list[RawSpendRecord]) -> str
def _compute_quality_flags(records: list[RawSpendRecord], anomalies: list[dict]) -> list[str]
def _contract_deviation_check(records, contract_terms) -> list[dict]
```

---

## Tests required

- 6 records spanning 3 quarters with full currency data → `data_completeness = FULL`
- 2 records only → `data_completeness = SPARSE`
- Zero records → `data_completeness = NONE`, no raise
- TTM calculation: 5 quarters of records → `total_usd_ttm` = sum of last 4 quarters only
- YTD calculation: records in prior year excluded from `total_usd_ytd`
- Records with null `period_start` and `period_end` → in all-time total, absent from `by_period`
- Duplicate `invoice_ref` `"INV-001"` on two records → `DUPLICATE_INVOICE` anomaly, severity HIGH
- Record `amount_usd = 15000`, `po_number = None` → `MISSING_PO` anomaly, severity LOW
- Q3 spend 4x rolling average → `SPEND_SPIKE` anomaly, description includes multiplier
- No spend for Q1, Q2, Q3 when records span 8 quarters → `SPEND_GAP` anomaly
- All records `amount_usd = None` → `NO_CURRENCY_DATA` flag, no raise, totals all null
- UNMATCHED record with `amount_raw` = `"25000"` → `HIGH_VALUE_UNMATCHED` flag
- Contract `total_value = 100000`, actual spend `= 155000` → `CONTRACT_DEVIATION` anomaly HIGH (55% over)
- Contract `total_value = 100000`, actual spend `= 60000` → `CONTRACT_DEVIATION` anomaly MEDIUM (40% under)
- No `total_value` in any contract → deviation check skipped entirely
- `by_category` with null `category_raw` → grouped under `"UNCATEGORISED"`
- `payment_terms_days_avg` = mean of non-null values; null if no records have the field
