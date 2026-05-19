# rs_schema (Process 3 Data Models)
# FIXED: Added payment_terms_days to RawSpendRecord (Issue 2)
# FIXED: Added contract_count to RelationshipSpendProfile (Issue 3)
# FIXED: GapReport.gap_severity max is MAJOR — CRITICAL removed (Issue 4)

## Overview

**File:** `src/cobalt/models/schemas/rs_schema.py`
**Role:** All dataclasses for Process 3. Defines the data contracts between tools, the orchestrator,
and the workspace writer. Every class implements `to_dict()` / `from_dict()` for RuntimeEngine
snapshot compatibility.
**Depends on:** Nothing from enrichment_schema.py. Confidence values are plain string literals
throughout P3 — same pattern as existing DE tools.

---

## Purpose

Central schema file for Process 3. Analogous to `enrichment_schema.py` for Process 2. All P3 tools
import from this file — no schema classes are defined inside tool files.

`to_dict()` / `from_dict()` are required on all dataclasses so that RuntimeEngine can serialise step
results into `state.json` and deserialise them on crash recovery.

**Confidence values:** P3 uses plain string literals `"HIGH"`, `"MEDIUM"`, `"LOW"`, `"MISSING"` —
consistent with the existing enrichment tools. There is no `ConfidenceLevel` enum class in
`enrichment_schema.py` to import; do not create one here either.

**GapReport lives here:** `GapReport` is a dataclass requiring `to_dict()` / `from_dict()`. It is
defined in this file and `gap_analyzer.py` imports it from here — not the other way around. This
prevents circular imports.

**GapReport severity:** `gap_analyzer.analyse_gaps()` produces at most `MAJOR` as the worst
severity. `CRITICAL` is an assembler-level severity assigned by `rs_profile_assembler` when it
combines a MAJOR gap report with `data_completeness = NONE`. The `GapReport.gap_severity` field
uses the values MAJOR / MINOR / NONE only.

---

## Enumerations

### `ArrivalMode`

```python
class ArrivalMode(str, Enum):
    CONNECTOR   = "CONNECTOR"
    FILE_UPLOAD = "FILE_UPLOAD"
    CHECK_IN    = "CHECK_IN"
```

### `TrustLevel`

```python
class TrustLevel(str, Enum):
    OFFICIAL        = "OFFICIAL"       # Authoritative ERP connector
    SYSTEM_EXPORT   = "SYSTEM_EXPORT"  # Non-authoritative system export
    USER_SUBMITTED  = "USER_SUBMITTED" # File upload or check-in
```

**Trust hierarchy for confidence scoring:** OFFICIAL > SYSTEM_EXPORT > USER_SUBMITTED.

### `DocumentType`

```python
class DocumentType(str, Enum):
    CONTRACT    = "CONTRACT"
    INVOICE     = "INVOICE"
    SOW         = "SOW"
    AMENDMENT   = "AMENDMENT"
    QBR         = "QBR"
    COMPLIANCE  = "COMPLIANCE"
    OTHER       = "OTHER"
```

### `RelationshipType`

```python
class RelationshipType(str, Enum):
    STRATEGIC     = "STRATEGIC"
    PREFERRED     = "PREFERRED"
    TRANSACTIONAL = "TRANSACTIONAL"
    INCIDENTAL    = "INCIDENTAL"
    UNKNOWN       = "UNKNOWN"
```

### `DependencyTier`

```python
class DependencyTier(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"
```

### `ContractCoverage`

```python
class ContractCoverage(str, Enum):
    FULLY_COVERED     = "FULLY_COVERED"
    PARTIALLY_COVERED = "PARTIALLY_COVERED"
    UNCOVERED         = "UNCOVERED"
```

### `RenewalUrgency`

```python
class RenewalUrgency(str, Enum):
    URGENT  = "URGENT"
    WATCH   = "WATCH"
    OK      = "OK"
    UNKNOWN = "UNKNOWN"
```

### `DataCompleteness`

```python
class DataCompleteness(str, Enum):
    FULL    = "FULL"
    PARTIAL = "PARTIAL"
    SPARSE  = "SPARSE"
    NONE    = "NONE"
```

### `ProfileStatus`

```python
class ProfileStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL  = "PARTIAL"
    MINIMAL  = "MINIMAL"
    FAILED   = "FAILED"
```

### `RSRunStatus`

```python
class RSRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    SKIPPED   = "SKIPPED"
    BLOCKED   = "BLOCKED"
    FAILED    = "FAILED"
```

---

## Dataclasses

### `RawSpendRecord`

One line from any data source. Produced by `structured_data_collector`.

```python
@dataclass
class RawSpendRecord:
    source_id:            str           # e.g. "erp_connector_01" or "upload_abc123"
    arrival_mode:         str           # ArrivalMode value
    trust_level:          str           # TrustLevel value
    period_start:         str | None    # ISO date "YYYY-MM-DD"
    period_end:           str | None    # ISO date "YYYY-MM-DD"
    amount_raw:           str           # As received, may include currency symbol
    currency_raw:         str | None    # Raw currency string
    amount_usd:           float | None  # Normalised to USD; None if conversion failed
    category_raw:         str | None    # Category label from source
    cost_centre:          str | None
    po_number:            str | None
    invoice_ref:          str | None
    matched_vendor_id:    str | None    # Set if vendor match successful
    match_confidence:     str           # HIGH / MEDIUM / LOW / UNMATCHED
    payment_terms_days:   int | None    # FIX-2: Net payment terms (e.g. 30 for Net 30).
                                        # Populated from CSV payment terms column or
                                        # checkin_data["payment_terms_days"] when
                                        # produced by CHECK_IN mode.
                                        # None when source does not provide this field.
```

**`to_dict()`:** Returns all fields as a flat dict. `None` values serialised as JSON null.
**`from_dict(d)`:** Classmethod. Reconstructs from dict. Missing keys default to `None`.

**`payment_terms_days` population rules:**
- FILE_UPLOAD: read from the payment terms column per row if column is present; `None` otherwise.
- CHECK_IN: set from `checkin_data["payment_terms_days"]` on every spend record produced from
  that check-in; `None` if key absent.
- CONNECTOR: set from connector record field if present; `None` otherwise.

### `ContractTerms`

Structured data extracted from one document by `document_intelligence`.

```python
@dataclass
class ContractTerms:
    document_id:            str
    document_type:          str           # DocumentType value
    effective_date:         str | None    # ISO date
    expiry_date:            str | None    # ISO date
    auto_renews:            bool | None
    notice_period_days:     int | None
    total_value:            float | None
    currency:               str | None    # ISO 3-letter code
    payment_terms_days:     int | None
    governing_law:          str | None
    termination_clauses:    list[str]     # Never None; empty list if none found
    key_obligations:        list[str]     # Never None; empty list if none found
    sla_summary:            str | None
    extraction_confidence:  str           # HIGH / MEDIUM / LOW
```

**`to_dict()`:** Lists serialised as JSON arrays.
**`from_dict(d)`:** Lists default to `[]` if key absent.

### `StructuredDataBundle`

Complete output of `structured_data_collector`. Passed to Tools 2 and 3.

```python
@dataclass
class StructuredDataBundle:
    vendor_id:              str
    programme_id:           str
    collected_at:           str           # ISO timestamp
    arrival_modes_used:     list[str]     # ArrivalMode values actually attempted
    raw_spend_records:      list[RawSpendRecord]
    connector_metadata:     dict          # source_id → {name, records_pulled, errors, status}
    upload_metadata:        dict          # file_id → {filename, rows, errors}
    checkin_metadata:       dict          # checkin_id → {sent_at, received_at, fields_provided}
    collection_warnings:    list[str]
```

**`to_dict()`:** Nested dataclasses serialised via their own `to_dict()`.
**`from_dict(d)`:** Nested lists reconstructed via `RawSpendRecord.from_dict()`.

### `DocumentIntelligenceResult`

Complete output of `document_intelligence`. Passed to Tool 3 and Tool 5.

```python
@dataclass
class DocumentIntelligenceResult:
    vendor_id:              str
    documents_processed:    int
    documents_skipped:      int
    extracted_contracts:    list[ContractTerms]
    extraction_warnings:    list[str]
```

**`to_dict()`:** `extracted_contracts` serialised via `ContractTerms.to_dict()`.
**`from_dict(d)`:** `extracted_contracts` reconstructed via `ContractTerms.from_dict()`.

### `SpendSummary`

Aggregated spend view. Produced by `spend_aggregator`, used by classifier and assembler.

```python
@dataclass
class SpendSummary:
    total_usd_all_time:         float | None
    total_usd_ttm:              float | None   # Trailing 12 months
    total_usd_ytd:              float | None
    by_period:                  dict           # "YYYY-QN" → amount_usd
    by_category:                dict           # category_raw → amount_usd
    by_cost_centre:             dict           # cost_centre → amount_usd
    invoice_count:              int
    po_count:                   int
    payment_terms_days_avg:     int | None
    data_completeness:          str            # DataCompleteness value
    confidence:                 str            # HIGH / MEDIUM / LOW / NONE
```

### `SpendAggregationResult`

Complete output of `spend_aggregator`.

```python
@dataclass
class SpendAggregationResult:
    vendor_id:              str
    summary:                SpendSummary
    anomalies:              list[dict]     # {type, description, severity, ...}
    data_quality_flags:     list[str]
    aggregated_at:          str            # ISO timestamp
```

**`to_dict()`:** `summary` serialised via `SpendSummary.to_dict()`.
**`from_dict(d)`:** `summary` reconstructed via `SpendSummary.from_dict()`.

### `RelationshipClassification`

Complete output of `relationship_classifier`.

```python
@dataclass
class RelationshipClassification:
    vendor_id:                  str
    relationship_type:          str           # RelationshipType value
    dependency_score:           float         # 0.0–1.0
    dependency_tier:            str | None    # DependencyTier value; None if UNKNOWN type
    single_source_risk:         bool
    contract_coverage:          str           # ContractCoverage value
    relationship_age_days:      int | None
    renewal_urgency:            str           # RenewalUrgency value
    classification_confidence:  str           # HIGH / MEDIUM / LOW
    llm_used:                   bool
    reasoning:                  str | None    # LLM reasoning if llm_used=True
```

### `RelationshipSpendProfile`

Master P3 profile. Written to workspace by `rs_profile_assembler`.

```python
@dataclass
class RelationshipSpendProfile:
    vendor_id:                   str
    programme_id:                str
    profile_version:             int
    profile_status:              str           # ProfileStatus value
    created_at:                  str           # ISO timestamp; preserved on re-runs
    last_updated:                str           # ISO timestamp; updated on every run
    contract_count:              int           # FIX-3: len(doc_intelligence.extracted_contracts)
                                               # Set by assembler. Required for gap analysis
                                               # and YAML front-matter. Preserved in
                                               # to_dict()/from_dict() round-trips.
    spend_summary:               SpendSummary
    contract_terms:              list[ContractTerms]
    relationship_classification: RelationshipClassification
    gap_report:                  dict          # GapReport.to_dict() from gap_analyzer
    pcs_contribution:            float         # P3 contribution (max 0.20)
    pcs_total:                   float         # Updated overall PCS after P3
    flags:                       list[str]     # Conflict and quality flags
    data_sources:                list[str]     # Unique source IDs that contributed
```

**`to_dict()`:** All nested dataclasses serialised recursively.
**`from_dict(d)`:** Full reconstruction including nested types. `contract_count` defaults to 0
if key absent (backwards compatibility with any profiles written before this field was added).

### `RSRunResult`

Orchestrator result. Mirrors `EnrichmentRunResult` pattern.

```python
@dataclass
class RSRunResult:
    vendor_id:          str
    programme_id:       str           # Always set — never None
    status:             str           # RSRunStatus value
    pcs_before:         float | None
    pcs_after:          float | None
    tools_run:          list[str]     # Step IDs that executed
    flags_raised:       list[str]     # Flags from assembler
    profile_status:     str | None    # ProfileStatus value
    skip_reason:        str | None    # Reason if status=SKIPPED
    error:              str | None    # Error message if status=FAILED
```

### `GapReport`

Defined here (not in `gap_analyzer.py`) to avoid circular imports. `gap_analyzer.py` imports
this class from `rs_schema.py`.

```python
@dataclass
class GapReport:
    missing_fields:         list[str]
    low_confidence_fields:  list[str]
    stale_fields:           list[str]
    gap_severity:           str           # FIX-4: MAJOR / MINOR / NONE only.
                                          # CRITICAL is NOT produced by gap_analyzer.
                                          # rs_profile_assembler elevates MAJOR →
                                          # CRITICAL when data_completeness = NONE.
    recommended_actions:    list[str]

    def to_dict(self) -> dict: ...

    @classmethod
    def from_dict(cls, d: dict) -> "GapReport": ...
```

**`to_dict()`:** All list fields serialised as JSON arrays.
**`from_dict(d)`:** Lists default to `[]` if key absent.

---

## Import Map

```python
# rs_schema.py imports nothing from enrichment_schema — no ConfidenceLevel enum exists.
# Confidence values are plain string literals: "HIGH", "MEDIUM", "LOW", "MISSING".

# In all P3 tools:
from cobalt.models.schemas.rs_schema import (
    RawSpendRecord, ContractTerms, StructuredDataBundle,
    DocumentIntelligenceResult, SpendSummary, SpendAggregationResult,
    RelationshipClassification, RelationshipSpendProfile, RSRunResult,
    GapReport,
    ArrivalMode, TrustLevel, DocumentType, RelationshipType,
    DependencyTier, ContractCoverage, RenewalUrgency,
    DataCompleteness, ProfileStatus, RSRunStatus,
)

# gap_analyzer.py imports GapReport FROM rs_schema (not the other way):
from cobalt.models.schemas.rs_schema import GapReport
```

---

## Tests required

- `RawSpendRecord.to_dict()` → all fields serialised including `payment_terms_days`; `None` → null
- `RawSpendRecord.from_dict(d)` → reconstructed correctly; missing keys → `None`
- `RawSpendRecord` with `payment_terms_days=30` → round-trips correctly
- `RawSpendRecord` with `payment_terms_days=None` → round-trips correctly
- `ContractTerms.to_dict()` → lists serialised as JSON arrays
- `ContractTerms.from_dict(d)` → missing list keys default to `[]`
- `StructuredDataBundle.to_dict()` → nested `RawSpendRecord` list serialised
- `StructuredDataBundle.from_dict(d)` → nested list reconstructed via `RawSpendRecord.from_dict()`
- `SpendAggregationResult` round-trip: `from_dict(result.to_dict()) == result`
- `RelationshipSpendProfile` with `contract_count=2` → round-trips correctly
- `RelationshipSpendProfile.from_dict(d)` where `contract_count` absent → defaults to 0
- `RelationshipSpendProfile` round-trip: `from_dict(profile.to_dict()) == profile`
- `ArrivalMode.CONNECTOR` is a string (`str` subclass) → JSON serialisable without extra conversion
- `GapReport.to_dict()` / `from_dict()` round-trip equality
- `GapReport.gap_severity` is one of MAJOR / MINOR / NONE — never CRITICAL
- Confidence values are plain strings — no `ConfidenceLevel` class defined or imported anywhere in P3
- `gap_analyzer.py` imports `GapReport` from `rs_schema` — confirmed no circular import
