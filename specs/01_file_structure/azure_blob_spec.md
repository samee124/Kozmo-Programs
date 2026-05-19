# Cobalt — Azure Blob Storage Specification

## Overview

- **Storage accounts:** One per environment — `cobaltprod`, `cobaltdev`, `cobalttest`
- **SDK:** All access via Azure SDK (`azure-storage-blob`) — never direct HTTP
- **V1 → V2 migration:** `atomic_write()` in V2 replaces `tmp.replace(path)` with `blob_client.upload_blob(overwrite=True)`. V1 uses local filesystem; this spec is the V2 target.
- **Multi-tenancy:** All workspace blobs are prefixed with `{UserId}` (e.g., `User001`). This scopes every file to its owner at the storage layer. No files from different users can be mixed.
- **Environment variables (V2):**
  - `AZURE_STORAGE_CONNECTION_STRING` — replaces `WORKSPACE_ROOT`
  - `AZURE_WORKSPACE_CONTAINER` — e.g., `cobalt-workspace-prod`
  - `AZURE_UPLOADS_CONTAINER` — e.g., `cobalt-uploads-prod`
  - `AZURE_BRAIN_CONTAINER` — `cobalt-brain` (no env suffix)

---

## Container Catalogue

Three containers. Container names are lowercase alphanumeric + hyphens (Azure requirement).

| Container name | Purpose | Blob types | Versioning | Soft delete |
|---|---|---|---|---|
| `cobalt-workspace-{env}` | All workspace `.md` and `.json` files | Block Blob + Append Blob | **ON** (point-in-time recovery of overwritten files) | 30 days |
| `cobalt-brain` | Brain JSON knowledge files — shared across all users, no env suffix | Block Blob | OFF (manual curation) | 7 days |
| `cobalt-uploads-{env}` | P3 uploaded documents (CSV, Excel, PDF) — write-once | Block Blob | OFF | 30 days |

`{env}` = `prod` / `dev` / `test`

**Access:** Private. No anonymous access. Access via connection string or managed identity only.

---

## Blob Path Convention — `cobalt-workspace-{env}`

All paths start with `{UserId}`. Virtual directories mirror the local `workspace/` hierarchy exactly. All separators are `/`.

```
{UserId}/                                       e.g., User001/
│
├── {ProgrammeId}/                              e.g., PROG-001/
│   │
│   ├── programme_run/
│   │   ├── programme_plan.md                   Planning Agent strategy for this run
│   │   ├── vendor_register.md                  All confirmed vendors with status
│   │   ├── deduplication_report.md             Summary of all dedup decisions made
│   │   ├── triage_queue.md                     Human review items pending
│   │   ├── run_log.md                          High-level run summary and outcomes
│   │   ├── enrichment_log.md                   [V2] Per-vendor P2 enrichment outcomes
│   │   ├── brain_update_queue.md               [V2] Pending Brain knowledge updates
│   │   ├── rs_log.md                           [P3] Per-vendor P3 run outcomes
│   │   ├── checkpoint.json                     Resume state for interrupted runs
│   │   └── intake_plans/
│   │       └── IP-{candidate_key}-{n:03d}.md   Per-candidate investigation plan
│   │
│   ├── workflows/                              [V2] Runtime crash recovery files
│   │   └── {workflow_id}/
│   │       ├── workflow.json                   EXECUTABLE TRUTH (Planning Agent writes)
│   │       ├── state.json                      EXECUTION STATE (RuntimeEngine updates)
│   │       └── plan.md                         AUDIT TRAIL (PlanRenderer derives)
│   │
│   └── v-{VendorId}/                           e.g., v-V-ABCD-001/
│       │
│       ├── identity/
│       │   ├── entity.md                       Canonical identity [IMMUTABLE: InputName field]
│       │   └── gate_results.md                 Investigation gate pass/fail record
│       │
│       ├── cost_file/
│       │   ├── spend.md                        ERP spend signal
│       │   ├── contract.md                     Contract terms (OBSERVED or NOT_FOUND)
│       │   └── coverage.md                     PCS score, gaps, enrichment ledger
│       │
│       ├── profile/
│       │   ├── vendor_profile.md               [P2] Enriched research profile
│       │   └── relationship_spend_profile.md   [P3] Spend totals, classification, contracts
│       │
│       ├── connectors/
│       │   └── {source_id}.json                [P3] ERP connector stub data files
│       │
│       ├── evidence/
│       │   └── ev-{type}-{id}.md               Evidence files [IMMUTABLE after creation]
│       │
│       └── execution/
│           └── ledger.md                       Action history [APPEND-ONLY — Append Blob]
```

### Path Construction Rules

| Blob path segment | Value | Example |
|---|---|---|
| Level 1 | `{UserId}` | `User001` |
| Level 2 | `{ProgrammeId}` | `PROG-001` |
| Level 3 | `programme_run/` or `workflows/` or `v-{VendorId}/` | `v-V-ABCD-001/` |
| Level 4 | subdirectory name | `identity/` |
| Level 5 | filename | `entity.md` |

Full example: `User001/PROG-001/v-V-ABCD-001/identity/entity.md`

---

## Blob Path Convention — `cobalt-brain`

Flat structure. No user prefix — Brain JSON files are shared across all accounts. Loaded once at process startup and cached in-process.

```
cobalt-brain/
├── known_vendors.json          Registry of known canonical vendors
├── rebrand_map.json            Company rebrand history (old name → new name)
├── alias_map.json              Vendor aliases and trading names
├── acquisition_map.json        M&A history (acquired → acquiring company)
└── brand_map.json              Brand → parent company mapping
```

Brain files are updated only by human curation or the Brain Update Queue process (V2). They are never written by individual programme runs.

---

## Blob Path Convention — `cobalt-uploads-{env}`

User-scoped. Write-once. P3 document intelligence reads uploaded PDFs, CSVs, and Excel files from here. Files are never overwritten after upload.

```
cobalt-uploads-{env}/
└── {UserId}/                                   e.g., User001/
    └── {ProgrammeId}/                          e.g., PROG-001/
        └── {VendorId}/                         e.g., V-ABCD-001/
            └── {file_id}/                      e.g., upload_abc12345/
                ├── {original_filename}         e.g., ap_extract_q1.csv
                └── metadata.json
```

**`metadata.json` schema:**

```json
{
  "file_id": "upload_abc12345",
  "original_filename": "ap_extract_q1.csv",
  "trust_level": "USER_SUBMITTED",
  "uploaded_at": "2025-10-01T14:00:00Z",
  "user_id": "User001",
  "programme_id": "PROG-001",
  "vendor_id": "V-ABCD-001",
  "content_type": "text/csv"
}
```

`file_id` is generated at upload time as `upload_{8-char-hex}` (e.g., `upload_abc12345`). Used to correlate the upload with the `uploaded_files` parameter passed to `structured_data_collector.collect_structured_data()`.

---

## Blob Metadata Tags

Applied to every workspace blob at write time via the `upload_blob()` call's `metadata` parameter. Tags enable filtering and lifecycle management without reading blob content — Azure Storage supports filter-by-tag queries at scale.

| Tag key | Example value | Applied to | Purpose |
|---|---|---|---|
| `user_id` | `User001` | All workspace + upload blobs | Multi-tenant filtering: list all blobs for a user across programmes |
| `programme_id` | `PROG-001` | All workspace + upload blobs | Programme-scoped listing: find all files for a specific run |
| `vendor_id` | `V-ABCD-001` | Vendor-level blobs | Vendor-scoped listing. Use `_programme` for programme-level files (no vendor) |
| `file_type` | `entity_md` | All | Type-specific queries and lifecycle rules. Values: `entity_md`, `vendor_profile_md`, `relationship_spend_profile_md`, `workflow_json`, `state_json`, `evidence_md`, `ledger_md`, `coverage_md`, etc. |
| `process` | `P1` | All | Process attribution. Values: `P1`, `P2`, `P3`, `SHARED`, `RUNTIME`. Used for process-level cost allocation. |
| `immutable` | `true` | `entity.md`, `evidence/*` | Guard flag. Application reads this before deciding whether to allow overwrite. `true` → raise `FileOwnershipViolation` |
| `append_only` | `true` | `ledger.md` | Guard flag. Application reads this before deciding whether to use `append_block()` instead of `upload_blob()`. |

---

## Immutability and Append-Only Rules

| File | Local V1 rule | Azure V2 enforcement |
|---|---|---|
| `entity.md` — `InputName` field | `atomic_write()` checks `InputName`, raises `FileOwnershipViolation` if it would change | **Application-layer guard only.** Do NOT use Azure Immutable Storage Policy — it is too strict and would block all field updates, including valid ones. |
| `evidence/ev-*.md` | Never overwritten after creation | `upload_blob(overwrite=False)` — raises `BlobAlreadyExistsError` if the blob already exists. Caught and re-raised as `FileOwnershipViolation`. |
| `ledger.md` | `append_md()` only — never full overwrite | **Azure Append Blob type.** Created as an Append Blob on first write. Each `append_md()` call → `append_block(content_chunk)`. Never use Block Blob for this file. |
| Regular `.md` files | `tmp.replace(path)` — atomic overwrite | Block Blob, `upload_blob(overwrite=True)`. Blob versioning provides rollback equivalent of the tmp-file pattern. |
| `workflow.json` | `apply_revision()` increments version, never full replace | Block Blob, `upload_blob(overwrite=True)`. Version number in JSON content tracks revision history. |
| `state.json` | Atomic update per step completion | Block Blob, `upload_blob(overwrite=True)`. Previous state version preserved in blob version history. |

---

## Access Tier Lifecycle Policy

Applied via Azure Storage Lifecycle Management rules on each container.

### `cobalt-workspace-{env}`

| Condition | Action | Reason |
|---|---|---|
| Blob not modified for 90 days | Move to **Cool** tier | Active programmes run < 90 days. Completed programmes rarely re-read. |
| Blob not modified for 365 days | Move to **Archive** tier | Historical records. Rarely accessed; archive cost is ~20% of Hot. |

### `cobalt-uploads-{env}`

| Condition | Action | Reason |
|---|---|---|
| Blob not modified for 30 days | Move to **Cool** tier | Uploads are read once during P3 extraction, then rarely accessed again. |

### `cobalt-brain`

Always stays **Hot**. Brain JSON files are loaded at every process startup. Cool/Archive retrieval latency would delay process initialisation.

---

## atomic_write() V2 Adaptation

In V1, `atomic_write()` writes to local filesystem:

```python
# V1 — local filesystem
with tmp_path.open("w", encoding="utf-8") as f:
    f.write(content)
tmp_path.replace(final_path)   # atomic rename
```

In V2 (Azure Blob), the implementation changes:

```python
# V2 — Azure Blob Storage

# Regular files (Block Blob):
blob_client = container_client.get_blob_client(blob_path)
blob_client.upload_blob(
    content,
    overwrite=True,
    metadata={"user_id": user_id, "file_type": file_type, ...}
)

# Append-only files (ledger.md — Append Blob):
blob_client = container_client.get_blob_client(blob_path)
if not blob_client.exists():
    blob_client.create_append_blob()
blob_client.append_block(content_chunk)
```

No temporary file is needed — Azure upload is atomic per blob. Blob versioning (enabled on the workspace container) provides rollback capability equivalent to the tmp-file replace pattern.

The `sync_to_db()` call sequence is **unchanged** — it still fires after a successful blob write, not inside the blob upload call itself.

---

## Naming Conventions

| Element | Convention | Example |
|---|---|---|
| UserId | `User{NNN}` — three-digit, zero-padded | `User001`, `User042` |
| ProgrammeId | `PROG-{NN}` or custom slug | `PROG-001`, `q4-2025-review` |
| VendorId | `V-{XXXX}-{NNN}` — four-char code + three-digit index | `V-ABCD-001` |
| Vendor blob folder | `v-{VendorId}` | `v-V-ABCD-001` |
| WorkflowId | `wf-{type}-{vendor_id}-{epoch}` | `wf-enrich-V-ABCD-001-1696156800` |
| file_id (uploads) | `upload_{8-char-hex}` | `upload_abc12345` |
| candidate_key | Normalised input slug (lowercase, hyphens) | `ibm-corporation` |
| investigation plan | `IP-{candidate_key}-{n:03d}.md` — three-digit, zero-padded | `IP-ibm-corporation-001.md` |
| Container names | Lowercase alphanumeric + hyphens | `cobalt-workspace-prod` |

---

## Multi-Tenancy Summary

The `UserId` prefix enforces tenant isolation at every layer:

| Layer | Isolation mechanism |
|---|---|
| Azure Blob paths | All paths begin with `{UserId}/` — no cross-user path collisions possible |
| Blob metadata tags | `user_id` tag on every blob — Azure filter-by-tag queries are always user-scoped |
| SQL Server tables | `UserId` column on `ProgrammeRun`, `VendorIntelligence`, `VendorCheckin`, `TriageItem`, `WorkflowIndex` — all queries filter by UserId |
| Application layer | `UserAccount` table enforces account existence before programme creation |

No shared-state file crosses user boundaries. The `cobalt-brain` container is the only cross-user resource — it is read-only from the perspective of individual programme runs.
