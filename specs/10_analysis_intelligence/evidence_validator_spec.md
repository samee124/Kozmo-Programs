# evidence_validator (AN-01)

## Overview

**Process:** Process 4 — Analysis & Intelligence
**Stage:** Stage 1 — Evidence Validation
**File:** `src/cobalt/tools/evidence_validator.py`
**Role:** Validates all incoming evidence before any reasoning begins. Scores source
confidence per item. Detects staleness, conflicts, and missing fields. The quality gate —
no reasoning runs on unvalidated evidence.
**Writes to workspace:** No — returns `ValidatedEvidenceAssembly` in memory.
**LLM:** None. Pure quality assessment.

---

## Purpose

Every downstream AN tool reads `ValidatedEvidenceAssembly`. AN-01 is the single entry
point for all evidence into the analysis chain. It assigns a `quality_score` to every
fact so downstream tools can weight evidence by trustworthiness.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `programme_id` | Caller | Workspace path resolution |
| `doc_intelligence` | RS-02 output | Contract terms and document facts |
| `structured_bundle` | RS-01 output | Spend records and structured data |
| `signal_bundle` | signal_processor (optional) | Check-in and email signals |
| `vendor_file` | Workspace | Parsed entity.md + relationship_spend_profile.md fields |
| `historical_state` | Prior run (optional) | Prior evidence state for freshness comparison |

`doc_intelligence`, `structured_bundle`, `signal_bundle`, `historical_state` may all be
`None`. Handle each gracefully — return assembly with MISSING facts for absent sources.

---

## Output

Returns `ValidatedEvidenceAssembly` in memory.

```json
{
  "vendor_id": "V-XXXX-001",
  "programme_id": "PROG-001",
  "facts": [
    {
      "field_name": "contract_term_end",
      "value": "2026-08-31",
      "display_value": "31 Aug 2026",
      "extraction_type": "AUTO_EXTRACTED",
      "source_file": "MSA_Northstar_2024.pdf",
      "source_section": "§ 4.1",
      "confidence": "HIGH",
      "trust_level": "OFFICIAL",
      "freshness_status": "CURRENT",
      "conflict_flag": false,
      "conflict_values": [],
      "quality_score": 1.0,
      "validated_at": "2025-10-01T14:00:00Z"
    }
  ],
  "completeness_pct": 0.73,
  "conflict_count": 0,
  "stale_count": 1,
  "missing_count": 3,
  "validated_at": "2025-10-01T14:00:00Z"
}
```

---

## Skills

### 1. Evidence collection from each source

**From `doc_intelligence` (DocumentIntelligenceResult):**
For each `ContractTerms` in `extracted_contracts`:
  For each non-None field: create one `ValidatedEvidenceFact`.
  `extraction_type = AUTO_EXTRACTED`
  `trust_level = OFFICIAL` (vendor signed the contract)
  `source_file = ContractTerms.document_id`
  `source_section = None` (document_intelligence does not track sections in V1)

**From `structured_bundle` (StructuredDataBundle):**
Aggregate spend metrics from `raw_spend_records`:
  Create fact for `spend_total_ttm_usd` (sum of TTM records with amount_usd)
  Create fact for `invoice_count` (len of all records)
  Create fact for `po_count` (records with non-null po_number)
  `extraction_type = COMPUTED`
  `trust_level = trust_level of first OFFICIAL or SYSTEM_EXPORT record, else USER_SUBMITTED`

**From `signal_bundle` (dict, optional):**
If not None: for each signal item in signal_bundle.get("signals", []):
  Create fact for the signal content.
  `extraction_type = SIGNAL`
  `trust_level = USER_SUBMITTED`

**From `vendor_file` (dict):**
Read key fields from entity.md section and relationship_spend_profile.md section:
  relationship_type, dependency_tier, primary_owner, renewal_date, auto_renew
  `extraction_type = COMPUTED`
  `trust_level = SYSTEM_EXPORT`

### 2. Quality score computation

```
TRUST_WEIGHTS = {
    "OFFICIAL":        1.00,
    "SYSTEM_EXPORT":   0.85,
    "USER_SUBMITTED":  0.65,
    "AI_INFERRED":     0.50,
}
RECENCY_WEIGHTS = {"CURRENT": 1.0, "STALE": 0.5, "MISSING": 0.0}
CONFLICT_PENALTY = {True: 0.7, False: 1.0}

quality_score = trust_weight × recency_weight × conflict_penalty
Clamp to [0.0, 1.0]
```

### 3. Freshness check via `staleness.is_stale()`

```python
FRESHNESS_THRESHOLDS_DAYS = {
    "CONTRACT":         365,
    "MSA":              365,
    "SOW":              365,
    "SLA_EXHIBIT":       45,
    "COMPLIANCE_CERT":  180,
    "CHECK_IN":          30,
    "INVOICE":           90,
    "SPEND":             90,
    "QBR":               90,
    "DEFAULT":           90,
}
```

Determine evidence_type from extraction_type + source_file extension/name.
Call `staleness.is_stale(retrieved_at, threshold)`.
Set `freshness_status = STALE` if stale, `CURRENT` if not.

If `historical_state` is None → treat all facts as CURRENT (no history to compare against).

### 4. Conflict detection

Group facts by `field_name`.
If same field_name has 2+ facts from different `source_file` values with different `value`:
  Set `conflict_flag = True` on all of them.
  Set `conflict_values = [v1, v2, ...]`
  Recompute `quality_score` with conflict_penalty = 0.7

### 5. Missing field detection

```python
EXPECTED_FIELDS = {
    "contract_term_start", "contract_term_end", "auto_renew",
    "notice_period_days", "total_contract_value", "sla_terms",
    "primary_owner", "relationship_type", "dependency_tier",
    "spend_total_ttm_usd", "renewal_date",
}
```

For each field in EXPECTED_FIELDS not present in any collected fact:
  Create `ValidatedEvidenceFact` with:
    `value = None`
    `freshness_status = MISSING`
    `quality_score = 0.0`
    `confidence = LOW`
    `trust_level = USER_SUBMITTED` (placeholder)

### 6. Completeness scoring

```python
present_and_current = count of facts where freshness_status == CURRENT
completeness_pct = present_and_current / len(EXPECTED_FIELDS)
```

---

## Internal structure

```python
def validate_evidence(
    vendor_id: str,
    programme_id: str,
    doc_intelligence: "DocumentIntelligenceResult | None",
    structured_bundle: "StructuredDataBundle | None",
    signal_bundle: dict | None,
    vendor_file: dict,
    historical_state: "HistoricalEvidenceState | None",
) -> ValidatedEvidenceAssembly:

def _facts_from_doc_intelligence(doc_intelligence) -> list[ValidatedEvidenceFact]
def _facts_from_structured_bundle(structured_bundle) -> list[ValidatedEvidenceFact]
def _facts_from_signals(signal_bundle) -> list[ValidatedEvidenceFact]
def _facts_from_vendor_file(vendor_file) -> list[ValidatedEvidenceFact]
def _compute_quality_score(trust_level, freshness_status, conflict_flag) -> float
def _check_freshness(fact, historical_state) -> str     # returns FreshnessStatus value
def _detect_conflicts(facts) -> list[ValidatedEvidenceFact]
def _add_missing_fields(facts) -> list[ValidatedEvidenceFact]
```

---

## Tests required — tests/tools/test_evidence_validator.py

- OFFICIAL + CURRENT fact → quality_score = 1.0 × 1.0 × 1.0 = 1.0
- USER_SUBMITTED + CURRENT + no conflict → quality_score = 0.65
- OFFICIAL + STALE → quality_score = 1.0 × 0.5 × 1.0 = 0.50
- OFFICIAL + CURRENT + conflict → quality_score = 1.0 × 1.0 × 0.7 = 0.70
- Two sources give different values for same field → conflict_flag=True on both, conflict_values has both
- Field in EXPECTED_FIELDS not in any source → ValidatedEvidenceFact with freshness_status=MISSING, quality_score=0.0
- doc_intelligence=None → no contract facts, no crash
- structured_bundle=None → no spend facts, no crash
- signal_bundle=None → no signal facts, no crash
- historical_state=None → all facts treated as CURRENT, no crash
- All sources None + empty vendor_file → assembly with all 11 EXPECTED_FIELDS as MISSING
- completeness_pct = 0.0 when all expected fields missing
- completeness_pct = 1.0 when all expected fields present and CURRENT
