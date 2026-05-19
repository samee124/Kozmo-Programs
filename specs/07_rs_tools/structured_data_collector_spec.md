# structured_data_collector
# FIXED: payment_terms_days populated on RawSpendRecord from all three arrival modes (Issue 2)

## Overview

**Process:** Process 3 — Relationship & Spend Data Gathering
**Stages covered:** Stage 1 (Raw Data Collection)
**File:** `src/cobalt/tools/structured_data_collector.py`
**Role:** Collect all raw structured spend, contract metadata, and ownership data from every
connected system and structured file upload. Three arrival modes — CONNECTOR, FILE_UPLOAD, CHECK_IN.
**Writes to workspace:** No — returns `StructuredDataBundle` in memory.
**Agent:** Vendor Manager Agent triggers. No LLM calls. No external HTTP.

---

## Purpose

First gate into Process 3. Gathers every raw data record that can be collected from internal
systems, uploaded spreadsheets, or a vendor-submitted check-in response. Nothing is aggregated,
interpreted, or scored here. Downstream tools operate on the bundle this tool produces.

**No LLM calls. No external HTTP.** V1 connectors read from local workspace stub directories. All
failures become collection warnings — this tool never raises.

**Arrival mode order when `arrival_modes=None`:** CONNECTOR → FILE_UPLOAD → CHECK_IN. This order
is deterministic and documented in the function docstring so deduplication behaviour is predictable.
Records from earlier modes appear first in `raw_spend_records`; deduplication uses the first
occurrence as canonical.

The key distinction from Process 2 evidence gathering is that this tool deals exclusively with
*internal* data: how much your organisation spent, what contracts exist, what a vendor has
self-reported. It never queries external registries or the web.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `programme_id` | Caller | Workspace path resolution |
| `arrival_modes` | Caller | Which modes to attempt (`None` = try all three in order: CONNECTOR → FILE_UPLOAD → CHECK_IN) |
| `connector_config` | Caller / VW Agent | Connector identifiers and credentials |
| `uploaded_files` | Caller | List of `{file_id, path, trust_level}` dicts |
| `checkin_data` | Caller | Structured dict from check-in response |

---

## Output

Returns `StructuredDataBundle` in memory. Not persisted to workspace.

```json
{
  "vendor_id": "V-XXXX-001",
  "programme_id": "PROG-001",
  "collected_at": "2025-10-01T14:00:00Z",
  "arrival_modes_used": ["FILE_UPLOAD"],
  "raw_spend_records": [
    {
      "source_id": "upload_abc123",
      "arrival_mode": "FILE_UPLOAD",
      "trust_level": "USER_SUBMITTED",
      "period_start": "2025-01-01",
      "period_end": "2025-03-31",
      "amount_raw": "£12,400",
      "currency_raw": "GBP",
      "amount_usd": 15620.0,
      "category_raw": "IT Services",
      "cost_centre": "CC-042",
      "po_number": "PO-2025-0041",
      "invoice_ref": "INV-90123",
      "matched_vendor_id": "V-XXXX-001",
      "match_confidence": "HIGH",
      "payment_terms_days": 30
    }
  ],
  "connector_metadata": {},
  "upload_metadata": {
    "upload_abc123": {"filename": "ap_extract_q1.csv", "rows": 14, "errors": []}
  },
  "checkin_metadata": {},
  "collection_warnings": []
}
```

---

## Skills

### 1. CONNECTOR mode

Reads records from a registered ERP or AP system connector.

**V1:** Connector is a stub. Looks for JSON files in `workspace/{programme_id}/{vendor_id}/connectors/`.
Each file is a list of raw spend record dicts conforming to `RawSpendRecord` field names. Reads all
JSON files found and converts to `RawSpendRecord`.

**Trust level:** `OFFICIAL` if connector is marked authoritative in `connector_config`; otherwise
`SYSTEM_EXPORT`.

**`payment_terms_days`:** Read directly from the connector record dict if the field is present.
Set to `None` if the field is absent from the connector record.

**V2:** Real ERP adapter (SAP, Oracle, Coupa, etc.) via CONNECTOR arrival mode. Deferred to TODO.md.

No connector config → mode skipped; `connector_metadata` entry records `{status: "NO_CONFIG"}`.

### 2. FILE_UPLOAD mode

Reads structured spend data from CSV or Excel files provided as absolute paths.

**Supported file types:** `.csv`, `.xlsx`, `.xls`

**Supported columns (auto-detected by fuzzy header matching):**

| Canonical field | Accepted header aliases |
|---|---|
| Vendor name | `vendor`, `supplier`, `payee`, `company`, `vendor name`, `supplier name` |
| Amount | `amount`, `value`, `total`, `cost`, `spend`, `invoice amount` |
| Currency | `currency`, `ccy`, `currency code` |
| Period start | `period start`, `from date`, `start date`, `date from` |
| Period end | `period end`, `to date`, `end date`, `invoice date`, `date` |
| PO number | `po`, `po number`, `purchase order`, `po ref` |
| Invoice ref | `invoice`, `invoice ref`, `invoice number`, `inv ref` |
| Cost centre | `cost centre`, `cost center`, `cc`, `department`, `dept` |
| Category | `category`, `spend category`, `type`, `service type` |
| Payment terms | `payment terms`, `payment terms days`, `net terms`, `net days`, `terms` |

Column detection uses `name_matching.normalise_for_match()` on headers — lowercases and strips
punctuation before comparison.

**`payment_terms_days` from FILE_UPLOAD:**
# FIX-2: payment_terms_days is populated per row from the payment terms column.
If the payment terms column is present and the row value is a parseable integer → set
`payment_terms_days` on that `RawSpendRecord`. If column absent or value not parseable → `None`.

**Vendor name matching:** Each row's vendor field is matched against the target vendor's canonical
name and known aliases using `name_matching.best_match()`.

| Match confidence | Jaro-Winkler threshold |
|---|---|
| HIGH | ≥ 0.90 |
| MEDIUM | 0.75 – 0.89 |
| LOW | 0.60 – 0.74 |
| UNMATCHED | < 0.60 |

**Rows with no vendor column:** Matched to target vendor at MEDIUM confidence (upload is presumed
vendor-specific by nature of the collection request).

**Trust level:** `USER_SUBMITTED` always for file uploads.

### 3. CHECK_IN mode

Parses a structured check-in response dict provided by the VW Agent after a vendor has replied
to a check-in request.

**Expected keys (all optional):**

| Key | Mapped to |
|---|---|
| `spend_ytd` | `amount_usd` with `period_start` = current year start |
| `spend_ttm` | `amount_usd` with `period_start` = 12 months ago |
| `currency` | `currency_raw` |
| `contract_ref` | `po_number` |
| `contract_expiry` | Carries forward to contract context |
| `payment_terms_days` | `payment_terms_days` on every spend record produced from this check-in |
| `po_coverage` | Boolean indicator in `checkin_metadata` |
| `notes` | Stored verbatim in `checkin_metadata` |

Each recognised key that contains a numeric spend value produces one `RawSpendRecord` with
`arrival_mode=CHECK_IN`, `trust_level=USER_SUBMITTED`.

**`payment_terms_days` from CHECK_IN:**
# FIX-2: If checkin_data contains "payment_terms_days" as a parseable integer,
# set payment_terms_days on EVERY RawSpendRecord produced from this check-in.
# If the key is absent or not parseable → payment_terms_days = None on all records.

Unrecognised keys → logged to `collection_warnings` as `UNKNOWN_CHECKIN_KEY_{key}`, not discarded.

### 4. Currency normalisation

Applied to every `RawSpendRecord` regardless of arrival mode.

Steps:
1. Strip leading/trailing whitespace
2. Remove currency symbols (`£`, `€`, `$`, `¥`, `A$`, `C$`, `Fr`)
3. Remove thousand separators (commas in non-EUR locales; periods in EUR locales)
4. Parse to Python `float`
5. Apply static exchange rate to convert to USD

**V1 supported currencies and static rates:**

| Currency | Code | Rate to USD |
|---|---|---|
| US Dollar | USD | 1.00 |
| British Pound | GBP | 1.26 |
| Euro | EUR | 1.08 |
| Australian Dollar | AUD | 0.65 |
| Canadian Dollar | CAD | 0.74 |
| Japanese Yen | JPY | 0.0067 |
| Swiss Franc | CHF | 1.11 |

Unknown or unsupported currency code → `amount_usd = None`; adds `collection_warnings` entry
`UNKNOWN_CURRENCY_{code}`.

### 5. Record deduplication

Within a single collection run, exact duplicates are collapsed.

**Duplicate definition:** Same `invoice_ref` (non-null) AND same `period_start` AND same
`amount_raw`.

Collapsed duplicate count noted in the relevant `upload_metadata.errors` or
`connector_metadata.errors`.

Duplicates across arrival modes are not automatically merged — they are kept separately as they may
represent legitimate cross-validation. The spend aggregator handles cross-mode deduplication.

---

## Flags produced

Flags are carried in `collection_warnings` (plain strings, not a separate enum field).

| Warning | Condition |
|---|---|
| `NO_CONNECTOR_CONFIG` | CONNECTOR mode attempted but `connector_config` is None or empty |
| `CONNECTOR_DIR_MISSING` | CONNECTOR mode: stub directory not found |
| `CONNECTOR_PARSE_ERROR_{source_id}` | JSON in connector stub directory is malformed |
| `UNKNOWN_CURRENCY_{code}` | Currency found but not in static rate table |
| `UNKNOWN_CHECKIN_KEY_{key}` | Check-in dict contains key not in expected schema |
| `LOW_MATCH_CONFIDENCE_RECORDS` | ≥ 20% of records matched at LOW confidence |
| `UNMATCHED_RECORDS_PRESENT` | Any records below 0.60 match threshold |
| `FILE_PARSE_ERROR_{file_id}` | File could not be read (wrong format, corrupt, encoding error) |
| `NO_DATA_ANY_MODE` | All three modes returned zero records after processing |
| `EMPTY_FILE_{file_id}` | File opened successfully but contained zero data rows |

---

## Routing

| Result | Next step |
|---|---|
| Bundle with ≥ 1 record | Pass to Tool 2 (`document_intelligence`) and Tool 3 (`spend_aggregator`) in parallel |
| `NO_DATA_ANY_MODE` warning | Return bundle to orchestrator; Tool 5 writes profile with `NONE` spend completeness |
| File parse error | Skip that file; continue with remaining records; log `FILE_PARSE_ERROR_{file_id}` |
| Connector stub dir missing | Skip CONNECTOR mode; attempt FILE_UPLOAD and CHECK_IN; log warning |

---

## Internal structure

```python
def collect_structured_data(
    vendor_id: str,
    programme_id: str,
    arrival_modes: list[str] | None = None,
    connector_config: dict | None = None,
    uploaded_files: list[dict] | None = None,
    checkin_data: dict | None = None,
) -> StructuredDataBundle:
    """
    Collect raw spend records from all available sources.
    When arrival_modes is None, attempts modes in order: CONNECTOR → FILE_UPLOAD → CHECK_IN.
    Never raises — all failures become collection_warnings.
    """

def _collect_from_connectors(vendor_id, programme_id, config) -> list[RawSpendRecord]
    # Reads payment_terms_days from connector record dict if present

def _collect_from_file_upload(vendor_id, files) -> list[RawSpendRecord]
    # Reads payment_terms_days from payment terms column per row if column present

def _collect_from_checkin(vendor_id, checkin_data) -> list[RawSpendRecord]
    # Sets payment_terms_days on all produced records from checkin_data["payment_terms_days"]

def _normalise_currency(amount_raw, currency_raw) -> tuple[float | None, str]
def _match_to_vendor(record, vendor_id, canonical_name, aliases) -> tuple[str | None, str]
def _deduplicate_records(records) -> list[RawSpendRecord]
```

---

## Tests required

- FILE_UPLOAD with valid CSV → `RawSpendRecords` with correct field mapping
- FILE_UPLOAD with Excel (.xlsx) → `RawSpendRecords` parsed correctly
- FILE_UPLOAD CSV with payment terms column `"payment terms days"` value `30`
  → `payment_terms_days = 30` on produced records
- FILE_UPLOAD CSV with no payment terms column → `payment_terms_days = None` on all records
- CHECK_IN dict with `spend_ytd` and `payment_terms_days: 45`
  → `RawSpendRecord.payment_terms_days = 45`
- CHECK_IN dict with `spend_ytd` and no `payment_terms_days` key
  → `RawSpendRecord.payment_terms_days = None`
- CHECK_IN dict with `spend_ytd` → one `RawSpendRecord` with `arrival_mode=CHECK_IN`
- All three modes active → combined bundle with correct `arrival_modes_used`
- Currency `"£1,200"` → `amount_usd = 1512.0`, `currency_raw = "GBP"`
- Currency `"€2.500,00"` (EU decimal) → `amount_usd` correctly parsed
- Unknown currency `"ZAR"` → `amount_usd = None`, warning `UNKNOWN_CURRENCY_ZAR`
- Vendor name `"Micro Strategies Ltd"` in CSV for vendor `"MicroStrategy"`
  → confidence MEDIUM or HIGH (fuzzy match)
- No vendor column in CSV → all rows matched at MEDIUM confidence
- No connector config → `NO_CONNECTOR_CONFIG` warning, no raise
- Empty file → `EMPTY_FILE_{file_id}` warning, zero records
- Corrupt file → `FILE_PARSE_ERROR_{file_id}` warning, no raise
- Duplicate rows (same `invoice_ref` + `period_start` + `amount_raw`) → collapsed to one
- Unrecognised check-in key → `UNKNOWN_CHECKIN_KEY_{key}` warning, key not discarded from metadata
- No data from any mode → `NO_DATA_ANY_MODE` warning, empty bundle returned without raise
