# rs_profile_assembler
# FIXED: contract_count read from RelationshipSpendProfile.contract_count field (Issue 3)
# FIXED: gap_severity CRITICAL elevation logic clarified — assembler assigns it, not gap_analyzer (Issue 4)
# FIXED: profile_status CRITICAL condition aligned with corrected gap_severity rules (Issue 4)

## Overview

**Process:** Process 3 — Relationship & Spend Data Gathering
**Stages covered:** Stages 5–6 (Assembly + Atomic Write)
**File:** `src/cobalt/tools/rs_profile_assembler.py`
**Role:** Merge all P3 outputs into the canonical relationship and spend profile. Write
`relationship_spend_profile.md`. Update PCS. Does not call any adapters or LLM.
**Writes to workspace:** Yes — **the only tool in Process 3 that writes to the vendor workspace.**
**Agent:** Vendor Manager Agent orchestrates write.

---

## Purpose

The commit step for Process 3. Takes all upstream outputs and produces one authoritative
`relationship_spend_profile.md` written atomically to the vendor workspace. Write only happens
after full assembly. If assembly fails, a minimal profile recording the failure is written instead
— the prior profile (if any) is preserved.

**No LLM. No external calls. Pure assembly and atomic write.**

This tool is the only P3 write path, consistent with Rule 1: VW Agent is the only post-intake
writer to vendor workspace files. The assembler executes the write on behalf of the VW Agent.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` / `programme_id` | Caller | Workspace path resolution |
| `structured_bundle` | Tool 1 | Collection metadata, arrival modes used, warnings |
| `doc_intelligence` | Tool 2 | Extracted contract terms, extraction warnings |
| `spend_aggregation` | Tool 3 | Spend summary, anomalies, quality flags |
| `classification` | Tool 4 | Relationship type, dependency score, renewal urgency |
| `current_pcs` | Caller | PCS score before Process 3 contribution |

---

## Output

**Primary write:** `relationship_spend_profile.md` written to vendor workspace at:
`workspace/{programme_id}/{vendor_id}/relationship_spend_profile.md`

**Secondary writes:**
- Ledger entry appended to `coverage.md` via `append_md()`
- DB row synced via explicit `sync_to_db()` call after `atomic_write()`

**In-memory return:** `RelationshipSpendProfile` dataclass.

### relationship_spend_profile.md YAML front-matter

```yaml
vendor_id: V-XXXX-001
programme_id: PROG-001
profile_version: 1
created_at: "2025-10-01T14:10:00Z"
last_updated: "2025-10-01T14:10:00Z"
pcs_contribution: 0.17
pcs_total: 0.93
dependency_tier: HIGH
relationship_type: STRATEGIC
spend_total_ttm_usd: 92000.0
contract_count: 1
flags:
  - CONTRACT_DEVIATION
```

### Markdown sections written

```
## Spend Summary
Period table: year-quarter | spend_usd | records
Category breakdown: category | spend_usd | share %
Anomalies table: type | description | severity

## Contract Terms
One subsection per ContractTerms record:
### Contract {n}: {document_type} ({effective_date} – {expiry_date})
Fields: value, currency, payment terms, auto-renewal, notice period, governing law,
        SLA summary, key obligations (list), termination clauses (list)
Extraction confidence noted.

## Relationship Classification
Type | Dependency Score | Dependency Tier | Single Source Risk | Contract Coverage
Renewal urgency | Relationship age | Classification confidence | LLM used
Reasoning (if llm_used = True)

## Data Quality
Completeness tier | Quality flags (list)
Collection warnings (list)
Extraction warnings (list)

## Gaps
Missing fields (list) | Low-confidence fields (list) | Gap severity
Recommended actions (list)
```

---

## Skills

### 1. PCS contribution calculation

**Process 3 maximum contribution: 0.20**

Components are additive. Points are awarded based on the best condition met (not cumulative within
the same component).

| Component | Points | Condition |
|---|---|---|
| Spend data — sparse or partial | 0.06 | `data_completeness` = SPARSE or PARTIAL |
| Spend data — full | 0.10 | `data_completeness` = FULL |
| Contract present | 0.05 | ≥ 1 `ContractTerms` with non-null `total_value` OR non-null `effective_date` |
| Classification done | 0.03 | `relationship_type` ≠ UNKNOWN |
| High confidence | 0.02 | `classification_confidence` = HIGH |

**Spend data:** Either SPARSE/PARTIAL (0.06) or FULL (0.10) — whichever applies, not both.
FULL earns 0.10, not 0.16.

**Maximum:** 0.10 + 0.05 + 0.03 + 0.02 = 0.20.

**Final PCS:** `pcs_total = min(1.0, current_pcs + pcs_contribution)`.

On assembly failure: `pcs_contribution = 0.0`, `pcs_total = current_pcs` (no contribution).

### 2. Conflict reconciliation

Checks for semantic inconsistencies across Tool 1–4 outputs.

| Conflict check | Flag raised |
|---|---|
| `spend_total_all_time_usd` deviates > 20% from sum of contract `total_value` values | `CONTRACT_DEVIATION` |
| Spend records exist but no `ContractTerms` extracted | `UNCOVERED_SPEND` |
| `spend_total_all_time_usd = 0` but at least one contract has non-null `total_value` | `SPEND_BELOW_CONTRACT` |
| `relationship_type = UNKNOWN` | `CLASSIFICATION_INCOMPLETE` |
| `renewal_urgency = URGENT` | `CONTRACT_RENEWAL_URGENT` |
| All spend records LOW/UNMATCHED match confidence | `LOW_DATA_CONFIDENCE` |

These flags are written into the `flags` list in the YAML front-matter.

### 3. Gap analysis

Calls `gap_analyzer.analyse_gaps()` on the assembled profile dict.

**FIX-3: `contract_count` is read from `RelationshipSpendProfile.contract_count` (set by the
assembler as `len(doc_intelligence.extracted_contracts)`) — not computed inline here.**

**Critical P3 fields assessed:**

| Field | Gap severity if missing |
|---|---|
| `spend_total_ttm_usd` | MAJOR |
| `relationship_type` (when UNKNOWN) | MAJOR |
| `dependency_tier` | MAJOR |
| `contract_count` = 0 | MINOR |
| `renewal_urgency` (when UNKNOWN) | MINOR |
| `spend_total_all_time_usd` | MINOR |

**Overall gap severity — assembler-level assessment:**

# FIX-4: gap_analyzer returns MAJOR / MINOR / NONE.
# The assembler applies this additional rule to determine CRITICAL at assembly level.

| Assembled severity | Condition |
|---|---|
| CRITICAL | `gap_report.gap_severity = MAJOR` AND `data_completeness = NONE` |
| MAJOR | `gap_report.gap_severity = MAJOR` (and `data_completeness` ≠ NONE) |
| MINOR | `gap_report.gap_severity = MINOR` |
| NONE | `gap_report.gap_severity = NONE` |

**Why the assembler applies CRITICAL separately:** `gap_analyzer` only sees field presence and
confidence values — it does not have access to `data_completeness`. The assembler has both the
gap report and the spend summary, so it is the correct place to apply the CRITICAL rule.

Gap report stored in `## Gaps` section of markdown.

### 4. Profile status classification

| Status | Conditions |
|---|---|
| `COMPLETE` | `data_completeness` = FULL; `relationship_type` ≠ UNKNOWN; assembled gap severity = NONE or MINOR |
| `PARTIAL` | `data_completeness` = PARTIAL or SPARSE; OR assembled gap severity = MINOR only |
| `MINIMAL` | `data_completeness` = NONE; no contract terms extracted |
| `FAILED` | Assembly raised an exception — prior profile preserved; minimal error record written |

### 5. Version management

**First write:** `profile_version = 1`, `created_at` = now.

**Subsequent writes (re-runs):** `profile_version` incremented by 1 from prior profile.
`created_at` preserved from first write. `last_updated` = now.

If prior profile cannot be read (does not exist or is corrupt) → treat as first write.

### 6. Workspace write sequence

1. Compute `contract_count = len(doc_intelligence.extracted_contracts)`
2. Assemble full `RelationshipSpendProfile` dict (including `contract_count`)
3. Run conflict reconciliation → collect `flags`
4. Run gap analysis via `gap_analyzer.analyse_gaps()` → collect `gap_report`
5. Apply assembler-level CRITICAL elevation: if `gap_report.gap_severity = MAJOR` AND
   `data_completeness = NONE` → assembled_gap_severity = CRITICAL; else use gap_report.gap_severity
6. Compute `pcs_contribution` and `pcs_total`
7. Serialise to YAML front-matter + markdown body
8. Call `atomic_write(path, content)` — writes the file atomically
9. Call `sync_to_db()` **explicitly** with the four RS fields — do not rely on `atomic_write()`
   triggering DB sync automatically. In V1 the `sync_to_db()` call inside `atomic_write()` is a
   no-op placeholder. Follow the same pattern as `enriched_profile_creator.py` which calls
   `sync_to_db()` directly after the file write.
   **Prerequisite:** confirm the four RS columns (`rs_last_updated`, `spend_total_usd`,
   `dependency_tier`, `relationship_type`) exist in `db/models.py` before implementing this step.
10. Append ledger entry to `coverage.md` via `append_md()`
11. Return `RelationshipSpendProfile`

**On assembly exception (steps 1–7):** Catch exception, write minimal `FAILED` profile:
```yaml
vendor_id: V-XXXX-001
profile_version: 1
last_updated: "<now>"
pcs_contribution: 0.0
flags:
  - PROFILE_ASSEMBLY_FAILED
error: "<exception message>"
```
Prior profile content preserved (if it exists). `pcs_contribution = 0.0`.

**`LedgerWriteError` from `append_md()`:** Propagate — HALT per Rule 4. This is the only
condition where this tool re-raises.

### 7. DB sync fields

`sync_to_db()` is called **explicitly** after `atomic_write()` (not relied upon to be called
inside it). It updates these columns in `VendorIntelligence`:
- `rs_last_updated` ← `last_updated` timestamp
- `spend_total_usd` ← `spend_summary.total_usd_ttm`
- `dependency_tier` ← `classification.dependency_tier`
- `relationship_type` ← `classification.relationship_type`

**Build prerequisite:** These four columns must be added to `db/models.py` via Alembic migration
before implementing this tool. Confirm they exist before writing the DB sync call. Do not implement
the sync call against a model that does not have the columns.

---

## Flags produced

| Flag | Trigger |
|---|---|
| `CONTRACT_DEVIATION` | Spend vs contract total deviation > 20% |
| `UNCOVERED_SPEND` | Spend records exist but no contract extracted |
| `SPEND_BELOW_CONTRACT` | Spend is zero but contract has `total_value` |
| `CLASSIFICATION_INCOMPLETE` | `relationship_type = UNKNOWN` |
| `CONTRACT_RENEWAL_URGENT` | `renewal_urgency = URGENT` from classifier |
| `LOW_DATA_CONFIDENCE` | All spend records LOW or UNMATCHED match confidence |
| `PROFILE_ASSEMBLY_FAILED` | Assembly exception; minimal profile written |

---

## Routing

| Profile status | Next step |
|---|---|
| `COMPLETE` | Vendor advances; Planning Agent notes P3 complete |
| `PARTIAL` | Advances; VW Agent schedules re-run when more data available |
| `MINIMAL` | VW Agent dispatches check-in request to vendor |
| `FAILED` | Triage task created; cannot advance until human review |
| `UNCOVERED_SPEND` flag | VW Agent initiates contract request campaign |
| `CONTRACT_RENEWAL_URGENT` flag | VW Agent escalates to renewal planning immediately |

---

## Internal structure

```python
def assemble_rs_profile(
    vendor_id: str,
    programme_id: str,
    structured_bundle: StructuredDataBundle,
    doc_intelligence: DocumentIntelligenceResult,
    spend_aggregation: SpendAggregationResult,
    classification: RelationshipClassification,
    current_pcs: float,
) -> RelationshipSpendProfile:

def _compute_pcs_contribution(spend_summary, classification, doc_intelligence) -> float
def _reconcile_conflicts(spend_summary, contract_terms, classification) -> list[str]
def _compute_assembled_gap_severity(gap_report: GapReport, data_completeness: str) -> str
    # FIX-4: Applies CRITICAL elevation: MAJOR + data_completeness=NONE → CRITICAL
def _classify_profile_status(spend_summary, classification, assembled_gap_severity: str) -> str
def _build_data_sources_list(bundle, doc_result) -> list[str]
def _write_rs_profile_md(profile: RelationshipSpendProfile, path: Path) -> None
def _read_prior_version(path: Path) -> int
def _write_minimal_failed_profile(path: Path, vendor_id: str, error: str) -> None
```

---

## Tests required

- Full inputs → profile written to correct path via `atomic_write`
- YAML front-matter contains all required keys after write including `contract_count`
- `contract_count` in profile equals `len(doc_intelligence.extracted_contracts)`
- `data_completeness = FULL` + contract + HIGH classification → `pcs_contribution = 0.20`
- `data_completeness = PARTIAL` + contract + classification done → `pcs_contribution = 0.14`
- `data_completeness = NONE`, no contracts → `pcs_contribution = 0.0`
- `data_completeness = SPARSE` + no contracts + classification done → `pcs_contribution = 0.09`
- Spend 50% above contract total → `CONTRACT_DEVIATION` flag in profile
- Spend records exist, no contracts → `UNCOVERED_SPEND` flag
- Spend = 0, contract `total_value` = 50000 → `SPEND_BELOW_CONTRACT` flag
- `relationship_type = UNKNOWN` → `CLASSIFICATION_INCOMPLETE` flag
- `renewal_urgency = URGENT` → `CONTRACT_RENEWAL_URGENT` flag
- Assembly exception (mocked) → minimal FAILED profile written, `pcs_contribution = 0.0`, no raise
- Prior profile present → `profile_version` incremented on re-run
- `created_at` preserved on re-run, `last_updated` updated
- `LedgerWriteError` from `append_md` → propagates (not swallowed)
- `pcs_total = min(1.0, current_pcs + contribution)` — clamped correctly when sum > 1.0
- Gap severity MAJOR + `data_completeness = NONE` → assembled_gap_severity = CRITICAL
- Gap severity MAJOR + `data_completeness = PARTIAL` → assembled_gap_severity = MAJOR
- Gap severity MINOR → assembled_gap_severity = MINOR (never elevated to MAJOR or CRITICAL)
- `profile_status = MINIMAL` when `data_completeness = NONE` and no contract terms
