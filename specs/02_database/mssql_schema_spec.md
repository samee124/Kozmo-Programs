# Cobalt — SQL Server Schema Specification

## Overview

- **Database engine:** SQL Server 2019+ (Azure SQL Database compatible)
- **Admin tool:** SSMS (SQL Server Management Studio)
- **ORM:** SQLAlchemy 2.0 with `mssql+pyodbc` dialect (replaces `psycopg2` from V1 development)
- **Role:** Scheduling and projection layer only — the source of truth is workspace `.md` files in Azure Blob Storage
- **Connection string env var:** `DATABASE_URL=mssql+pyodbc://...`
- **Naming convention:** PascalCase for all table names and column names

The database answers operational questions fast (which vendors need work now?, which triage items are overdue?) without re-reading blob files. It is never authoritative — it is always a projection of what workspace files say.

---

## PostgreSQL → SQL Server Type Mapping

| PostgreSQL (V1 dev) | SQL Server (Production) | Reason |
|---|---|---|
| `VARCHAR(n)` | `NVARCHAR(n)` | Unicode support for international vendor names |
| `TEXT` | `NVARCHAR(MAX)` | Unbounded string |
| `TIMESTAMP` / `DATETIME` | `DATETIME2(7)` | ISO 8601, 100-nanosecond precision, always UTC |
| `BOOLEAN` | `BIT` | SQL Server has no BOOLEAN type; use 0/1 |
| `INTEGER` / `INT` | `INT` | Identical |
| `FLOAT` | `FLOAT(53)` | IEEE 754 double precision |
| `NUMERIC(4,3)` | `DECIMAL(4,3)` | DECIMAL preferred in T-SQL |
| `DECIMAL(18,2)` | `DECIMAL(18,2)` | Identical |
| `JSON` | `NVARCHAR(MAX)` + `CHECK (ISJSON(col) = 1)` | No native JSON column type in SQL Server pre-2025 |
| `DEFAULT NOW()` | `DEFAULT GETUTCDATE()` | Always UTC |
| `ON CONFLICT DO NOTHING` | `IF NOT EXISTS … INSERT` | No upsert shorthand |
| `LIMIT n` | `TOP n` (or `FETCH FIRST n ROWS ONLY`) | |
| `ORDER BY col NULLS LAST` | `ORDER BY CASE WHEN col IS NULL THEN 1 ELSE 0 END, col` | SQL Server has no NULLS LAST |
| `INTERVAL '90 days'` | `DATEADD(day, -90, GETUTCDATE())` | |

---

## Table 1: `UserAccount`

**Purpose:** Anchors all data to a user or organisation account. The `UserId` (e.g., `User001`) is the top-level prefix in Azure Blob Storage, scoping every workspace file to its owner. This is the foundation of multi-tenancy — no files from different users can be mixed at the storage layer.

```sql
CREATE TABLE UserAccount (
    UserId              NVARCHAR(50)     NOT NULL  CONSTRAINT PK_UserAccount PRIMARY KEY,
    UserName            NVARCHAR(200)    NOT NULL,
    Email               NVARCHAR(255)    NOT NULL,
    SubscriptionTier    NVARCHAR(50)     NOT NULL  DEFAULT 'STARTER',
    IsActive            BIT              NOT NULL  DEFAULT 1,
    CreatedAt           DATETIME2(7)     NOT NULL  DEFAULT GETUTCDATE(),

    CONSTRAINT UQ_UserAccount_Email UNIQUE (Email)
);
```

### Column Reference

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `UserId` | NVARCHAR(50) | NOT NULL | Primary key. Short slug used as top-level blob path prefix (e.g., `User001`). Kept short for blob path readability. |
| `UserName` | NVARCHAR(200) | NOT NULL | Display name of the user or organisation running Cobalt. Used in UI and reports. |
| `Email` | NVARCHAR(255) | NOT NULL UNIQUE | Contact email for notifications and account identification. Unique constraint prevents duplicate accounts. |
| `SubscriptionTier` | NVARCHAR(50) | NOT NULL | Account tier: `STARTER` / `PROFESSIONAL` / `ENTERPRISE`. Controls programme limits, vendor caps, and feature flags. |
| `IsActive` | BIT | NOT NULL | Soft-delete flag. Inactive accounts (`0`) cannot start new programmes but all historical data is preserved for audit. |
| `CreatedAt` | DATETIME2(7) | NOT NULL | Account creation timestamp in UTC. Audit field — never updated. |

---

## Table 2: `ProgrammeRun`

**Purpose:** Records every vendor intake run. A programme is a named collection of vendors from one or more input files, processed through P1 (Vendor Intake). Tracks aggregate progress counters and run status. Each programme belongs to exactly one user account.

```sql
CREATE TABLE ProgrammeRun (
    ProgrammeId         NVARCHAR(50)     NOT NULL  CONSTRAINT PK_ProgrammeRun PRIMARY KEY,
    UserId              NVARCHAR(50)     NOT NULL,
    ProgrammeName       NVARCHAR(200)    NULL,
    Status              NVARCHAR(50)     NULL,
    InputFile           NVARCHAR(500)    NULL,

    TotalVendors        INT              NOT NULL  DEFAULT 0,
    Confirmed           INT              NOT NULL  DEFAULT 0,
    Triage              INT              NOT NULL  DEFAULT 0,
    Discarded           INT              NOT NULL  DEFAULT 0,
    Blocked             INT              NOT NULL  DEFAULT 0,

    CreatedAt           DATETIME2(7)     NOT NULL  DEFAULT GETUTCDATE()
);

CREATE INDEX IX_ProgrammeRun_UserId ON ProgrammeRun (UserId);
```

### Column Reference

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `ProgrammeId` | NVARCHAR(50) | NOT NULL | Primary key. Unique run identifier (e.g., `PROG-001`). Also the second-level folder name in blob storage under the UserId prefix. |
| `UserId` | NVARCHAR(50) | NOT NULL | Which user account owns this programme. Soft reference to `UserAccount.UserId`. Indexed for fast per-user programme listing. |
| `ProgrammeName` | NVARCHAR(200) | NULL | Human-readable label (e.g., "Q4 2025 Supplier Review"). Set by user when creating the programme. |
| `Status` | NVARCHAR(50) | NULL | Run lifecycle: `PENDING` → `IN_PROGRESS` → `COMPLETE` / `FAILED`. Updated as intake tools execute. |
| `InputFile` | NVARCHAR(500) | NULL | Blob path or filename of the source CSV/Excel that triggered this run. Retained for audit and re-run capability. |
| `TotalVendors` | INT | NOT NULL | Total candidate count extracted from the input file(s). Set after `source_intake` completes. |
| `Confirmed` | INT | NOT NULL | Count of vendors with `CONFIRMED` status after P1 gate checks. Primary success metric for the run. |
| `Triage` | INT | NOT NULL | Count requiring human review (ambiguous identity, conflicting data). Drives triage queue prioritisation. |
| `Discarded` | INT | NOT NULL | Count ruled out: duplicates, unresolvable entities, or below confidence threshold. |
| `Blocked` | INT | NOT NULL | Count blocked by gate checks: sanctions hits, fraud flags, or missing required data. |
| `CreatedAt` | DATETIME2(7) | NOT NULL | When the programme run was initiated. Audit field — never updated. |

---

## Table 3: `VendorIntelligence`

**Purpose:** The central scheduling and projection table for the entire platform. One row per vendor per programme. The scheduler queries this table to answer "which vendors need work right now?" All column values are written exclusively by `sync_to_db()` handlers — never by direct database writes. This table never contains facts that disagree with the workspace `.md` files; it is a read-optimised projection of file state.

```sql
CREATE TABLE VendorIntelligence (
    -- Identity
    VendorId            NVARCHAR(50)     NOT NULL  CONSTRAINT PK_VendorIntelligence PRIMARY KEY,
    ProgrammeId         NVARCHAR(50)     NOT NULL,
    UserId              NVARCHAR(50)     NOT NULL,
    VendorName          NVARCHAR(500)    NOT NULL,
    InputName           NVARCHAR(500)    NOT NULL,

    -- Scheduling
    Status              NVARCHAR(50)     NOT NULL,
    NextActionDue       DATETIME2(7)     NULL,
    ReplyDeadline       DATETIME2(7)     NULL,
    LastRunAt           DATETIME2(7)     NULL,

    -- Process 1 — Classification
    DataClass           NVARCHAR(10)     NOT NULL  DEFAULT 'CLASS_D',
    IdentityConfidence  DECIMAL(4,3)     NOT NULL  DEFAULT 0.000,
    Category            NVARCHAR(100)    NULL,
    Tier                NVARCHAR(10)     NULL,
    PcsScore            INT              NOT NULL  DEFAULT 0,
    VifGenerated        BIT              NOT NULL  DEFAULT 0,

    -- Process 2 — Enrichment
    ProfileStatus       NVARCHAR(50)     NULL,
    LastEnrichedAt      DATETIME2(7)     NULL,
    Subcategory         NVARCHAR(100)    NULL,
    VendorType          NVARCHAR(50)     NULL,
    HqCountry           NVARCHAR(10)     NULL,
    CompanySizeBand     NVARCHAR(20)     NULL,

    -- Process 3 — Relationship & Spend
    RsLastUpdated       DATETIME2(7)     NULL,
    SpendTotalUsd       DECIMAL(18,2)    NULL,
    DependencyTier      NVARCHAR(20)     NULL,
    RelationshipType    NVARCHAR(50)     NULL,

    -- Process 4 — Analysis & Intelligence
    CriScore            INT              NULL,
    HealthBand          NVARCHAR(20)     NULL,
    VendorState         NVARCHAR(20)     NULL,
    LastAnalysedAt      DATETIME2(7)     NULL,

    -- Audit
    CreatedAt           DATETIME2(7)     NOT NULL  DEFAULT GETUTCDATE(),
    UpdatedAt           DATETIME2(7)     NOT NULL  DEFAULT GETUTCDATE(),

    CONSTRAINT UQ_Vendor_Programme UNIQUE (ProgrammeId, VendorId)
);

-- Indexes
CREATE INDEX IX_VI_Status         ON VendorIntelligence (Status);
CREATE INDEX IX_VI_NextActionDue  ON VendorIntelligence (NextActionDue);
CREATE INDEX IX_VI_ProgrammeId    ON VendorIntelligence (ProgrammeId);
CREATE INDEX IX_VI_UserId         ON VendorIntelligence (UserId);
CREATE INDEX IX_VI_ProfileStatus  ON VendorIntelligence (ProfileStatus)
    WHERE ProfileStatus IS NOT NULL;    -- Filtered: only rows with enrichment data
CREATE INDEX IX_VI_DependencyTier ON VendorIntelligence (DependencyTier)
    WHERE DependencyTier IS NOT NULL;   -- Filtered: only rows with P3 classification
CREATE INDEX IX_VI_HealthBand     ON VendorIntelligence (HealthBand)
    WHERE HealthBand IS NOT NULL;       -- Filtered: only rows with P4 analysis data
```

### Column Reference — Identity Group

Written once during P1. InputName is immutable after creation.

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `VendorId` | NVARCHAR(50) | NOT NULL | Primary key. Canonical vendor identifier assigned after entity resolution (e.g., `V-ABCD-001`). Stable across all three processes. |
| `ProgrammeId` | NVARCHAR(50) | NOT NULL | Which intake run this vendor belongs to. Used in scheduler query filter and blob path construction. |
| `UserId` | NVARCHAR(50) | NOT NULL | Denormalised from `ProgrammeRun` for fast multi-tenant queries without a join. Every vendor query that needs to be scoped to a user filters on this. |
| `VendorName` | NVARCHAR(500) | NOT NULL | Canonical resolved name post-entity-resolution (e.g., "Microsoft Corporation"). May differ from `InputName`. Updated if resolution improves. |
| `InputName` | NVARCHAR(500) | NOT NULL | Exact raw name as it appeared in the input file — **IMMUTABLE after creation**. Preserved for deduplication tracing and audit. Never overwritten, even when `VendorName` changes. |

### Column Reference — Scheduling Group

Updated by `action_queue.md` sync handler on every VW Agent tick.

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `Status` | NVARCHAR(50) | NOT NULL | Vendor lifecycle state. The scheduler excludes certain statuses. Valid values: `CONFIRMED`, `TRIAGE_REQUIRED`, `DISCARDED`, `BLOCKED`, `NEEDS_ACTION`, `WAITING_HUMAN_GATE`, `CHECKIN_SENT`, `SURVEY_PENDING`, `COMPLETE`, `PAUSED`. |
| `NextActionDue` | DATETIME2(7) | NULL | When VW Agent should next process this vendor. Primary scheduler filter: `NextActionDue <= GETUTCDATE()`. NULL means no action is pending. |
| `ReplyDeadline` | DATETIME2(7) | NULL | SLA deadline for vendor response to a check-in or survey. Used to trigger overdue escalation when `RespondedAt` remains null past this date. |
| `LastRunAt` | DATETIME2(7) | NULL | Timestamp of the last completed automated process run (any of P1/P2/P3). Used for stale vendor detection. |

### Column Reference — P1 Classification Group

Written when `entity.md` is first created by P1. Reflects the outcome of entity resolution and external validation.

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `DataClass` | NVARCHAR(10) | NOT NULL | Data confidence class assigned by P1: `CLASS_A` (verified by official registry such as Companies House) → `CLASS_B` (registry match, not authoritative) → `CLASS_C` (inferred) → `CLASS_D` (unverified, default). Determines which downstream operations are permitted. |
| `IdentityConfidence` | DECIMAL(4,3) | NOT NULL | 0.000–1.000 confidence score from `entity_resolution`. Represents certainty that the resolved identity correctly matches the input name. Used to route borderline cases to triage. |
| `Category` | NVARCHAR(100) | NULL | Primary spend category (e.g., `IT_SERVICES`, `PROFESSIONAL_SERVICES`, `FACILITIES`). Used for dashboard grouping, risk segmentation, and classifier signal. May be updated by P2 enrichment. |
| `Tier` | NVARCHAR(10) | NULL | Strategic tier set by user or inferred: `TIER_1` / `TIER_2` / `TIER_3`. TIER_1 vendors receive priority scheduling — they sort first in the `get_due_vendors` query. |
| `PcsScore` | INT | NOT NULL | Profile Completeness Score (0–100). Composite metric summed across processes: P1 contributes max 53, P2 contributes max 47, P3 contributes max 20, P4 contributes max 10. Total clamped at 100. Drives dashboard health indicators and highlights under-documented vendors. |
| `VifGenerated` | BIT | NOT NULL | Whether the Vendor Intelligence File (VIF) PDF/report has been generated and delivered for this vendor. Used by reporting to identify vendors awaiting their VIF. |

### Column Reference — P2 Enrichment Group

Written when `vendor_profile.md` is created by P2. Null until enrichment runs.

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `ProfileStatus` | NVARCHAR(50) | NULL | P2 enrichment outcome: `COMPLETE` / `PARTIAL` / `MINIMAL` / `FAILED`. Null means P2 has not run. Used to determine whether re-enrichment is needed. |
| `LastEnrichedAt` | DATETIME2(7) | NULL | When P2 enrichment last completed. Staleness check: vendors not enriched in 90 days are automatically re-queued by the scheduler. Null means never enriched. |
| `Subcategory` | NVARCHAR(100) | NULL | Granular subcategory from P2 research (e.g., `Cloud Infrastructure` under `IT_SERVICES`). More specific than `Category` — used for detailed spend analysis. |
| `VendorType` | NVARCHAR(50) | NULL | Legal entity type from P2: `COMPANY` / `INDIVIDUAL` / `GOVERNMENT` / `CHARITY` / `PARTNERSHIP`. Affects due diligence requirements and contract terms. |
| `HqCountry` | NVARCHAR(10) | NULL | ISO 3166-1 alpha-2 country code of headquarters (e.g., `GB`, `US`, `DE`). Used for jurisdiction-specific compliance rules, sanctions screening scope, and geographic spend reporting. |
| `CompanySizeBand` | NVARCHAR(20) | NULL | Size classification from P2: `MICRO` (< 10 employees) / `SMALL` / `MEDIUM` / `LARGE` / `ENTERPRISE` (> 10,000 employees). Affects risk weighting and relationship management strategy. |

### Column Reference — P3 Relationship & Spend Group

Written when `relationship_spend_profile.md` is created by P3. Null until P3 runs.

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `RsLastUpdated` | DATETIME2(7) | NULL | When P3 last completed for this vendor. Used by the P3 freshness gate: profiles updated within 30 days are skipped (no re-run needed). Null means P3 has never run. |
| `SpendTotalUsd` | DECIMAL(18,2) | NULL | Trailing 12-month (TTM) spend in USD from P3 aggregation. Stored here for fast dashboard spend totals without reading blob files. Null means no spend data collected. |
| `DependencyTier` | NVARCHAR(20) | NULL | P3 dependency classification: `CRITICAL` / `HIGH` / `MEDIUM` / `LOW`. Used for risk dashboards, escalation rules, and VW Agent scheduling priority. |
| `RelationshipType` | NVARCHAR(50) | NULL | P3 relationship classification: `STRATEGIC` / `PREFERRED` / `TRANSACTIONAL` / `INCIDENTAL` / `UNKNOWN`. Primary signal used by VW Agent for scheduling and escalation decisions. |

### Column Reference — P4 Analysis & Intelligence Group

Written when `analysis_result.md` is created by P4. Null until P4 runs.

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `CriScore` | INT | NULL | Composite Relationship Intelligence score 0–100. Computed by scoring_engine as a weighted average of 5 dimension scores (Delivery Reliability 20% + Responsiveness 20% + Commercial Value 20% + Risk & Compliance 20% + Relationship Trend 20%). Null means P4 has not run. |
| `HealthBand` | NVARCHAR(20) | NULL | CRI health classification: `HEALTHY` (≥80) / `WATCH` (≥65) / `AT_RISK` (≥50) / `CRITICAL` (<50). Used by dashboards, VW Agent scheduling, and escalation rules. Null means P4 has not run. |
| `VendorState` | NVARCHAR(20) | NULL | Compound state from state_classifier combining CRI, trend direction, and renewal proximity: `HEALTHY` / `WATCH` / `AT_RISK` / `CRITICAL` / `UNKNOWN` / `ARCHIVED`. Differs from HealthBand — renewal proximity within 30 days can elevate state one band above HealthBand. |
| `LastAnalysedAt` | DATETIME2(7) | NULL | UTC timestamp of last successful P4 analysis run. Used by the P4 freshness gate: vendors analysed within 30 days are skipped unless force=True. Null means P4 has never run. |

### Column Reference — Audit Group

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `CreatedAt` | DATETIME2(7) | NOT NULL | When this row was first inserted (at P1 completion). Immutable — never updated. |
| `UpdatedAt` | DATETIME2(7) | NOT NULL | Last time any column on this row was changed by a `sync_to_db()` handler. Updated on every sync call. |

---

## Table 4: `VendorCheckin`

**Purpose:** Tracks each check-in campaign dispatched to a vendor. When the VW Agent sends a check-in request (asking the vendor to self-report spend figures, contract references, and payment terms), a row is inserted here. Enables SLA tracking (was the deadline met?), response rate reporting, and P3 data arrival correlation.

```sql
CREATE TABLE VendorCheckin (
    CheckinId           NVARCHAR(50)     NOT NULL  CONSTRAINT PK_VendorCheckin PRIMARY KEY,
    VendorId            NVARCHAR(50)     NULL,
    ProgrammeId         NVARCHAR(50)     NULL,
    UserId              NVARCHAR(50)     NULL,

    SentAt              DATETIME2(7)     NULL,
    ReplyDeadline       DATETIME2(7)     NULL,
    RespondedAt         DATETIME2(7)     NULL,

    Status              NVARCHAR(50)     NULL
);

CREATE INDEX IX_VendorCheckin_VendorId    ON VendorCheckin (VendorId);
CREATE INDEX IX_VendorCheckin_ProgrammeId ON VendorCheckin (ProgrammeId);
CREATE INDEX IX_VendorCheckin_UserId      ON VendorCheckin (UserId);
```

### Column Reference

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `CheckinId` | NVARCHAR(50) | NOT NULL | Primary key. Unique identifier for this check-in campaign instance. |
| `VendorId` | NVARCHAR(50) | NULL | Which vendor this check-in was sent to. Soft reference (no FK constraint — DB is projection only). |
| `ProgrammeId` | NVARCHAR(50) | NULL | Which programme this check-in belongs to. Used for programme-level response rate reporting. |
| `UserId` | NVARCHAR(50) | NULL | Denormalised for fast per-user check-in status queries without joining through `VendorIntelligence`. |
| `SentAt` | DATETIME2(7) | NULL | When the check-in request was dispatched to the vendor. Start of the SLA window. |
| `ReplyDeadline` | DATETIME2(7) | NULL | SLA deadline. If `RespondedAt` is still null after this datetime, the VW Agent triggers an overdue escalation. |
| `RespondedAt` | DATETIME2(7) | NULL | When the vendor's response was received and processed. Null means no response yet. |
| `Status` | NVARCHAR(50) | NULL | Campaign status: `SENT` / `RESPONDED` / `OVERDUE` / `CANCELLED`. |

---

## Table 5: `TriageItem`

**Purpose:** Captures every situation where automated processing cannot make a confident decision and a human reviewer must intervene. Triage items are created when: two vendors resolve to the same canonical identity, an entity has conflicting data across sources, document extraction fails, or profile assembly fails. Each item has an SLA deadline to prevent backlog accumulation. Resolved by a human reviewer who picks from `Options` or provides free-text `Resolution`.

```sql
CREATE TABLE TriageItem (
    TriageId            NVARCHAR(50)     NOT NULL  CONSTRAINT PK_TriageItem PRIMARY KEY,
    VendorId            NVARCHAR(50)     NULL,
    ProgrammeId         NVARCHAR(50)     NULL,
    UserId              NVARCHAR(50)     NULL,

    RawInput            NVARCHAR(500)    NULL,
    TriageType          NVARCHAR(50)     NULL,
    Question            NVARCHAR(MAX)    NULL,
    Options             NVARCHAR(MAX)    NULL  CHECK (Options IS NULL OR ISJSON(Options) = 1),

    Status              NVARCHAR(50)     NOT NULL  DEFAULT 'PENDING',
    SlaDeadline         DATETIME2(7)     NULL,
    ResolvedAt          DATETIME2(7)     NULL,
    Resolution          NVARCHAR(MAX)    NULL,

    CreatedAt           DATETIME2(7)     NOT NULL  DEFAULT GETUTCDATE()
);

CREATE INDEX IX_TriageItem_VendorId  ON TriageItem (VendorId);
CREATE INDEX IX_TriageItem_UserId    ON TriageItem (UserId);
CREATE INDEX IX_TriageItem_Status    ON TriageItem (Status);
CREATE INDEX IX_TriageItem_SLA       ON TriageItem (SlaDeadline)
    WHERE Status = 'PENDING';   -- Filtered index: fast overdue SLA query on PENDING items only
```

### Column Reference

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `TriageId` | NVARCHAR(50) | NOT NULL | Primary key. Unique identifier for this triage item. |
| `VendorId` | NVARCHAR(50) | NULL | Which vendor triggered this triage requirement. |
| `ProgrammeId` | NVARCHAR(50) | NULL | Which programme this belongs to. |
| `UserId` | NVARCHAR(50) | NULL | Denormalised for fast per-user triage queue fetching. The triage queue UI lists items filtered by UserId. |
| `RawInput` | NVARCHAR(500) | NULL | The raw data or value that could not be resolved automatically (e.g., the conflicting vendor name). Shown to the reviewer for context. |
| `TriageType` | NVARCHAR(50) | NULL | What kind of human decision is needed: `IDENTITY_CONFLICT` / `DATA_QUALITY` / `CLASSIFICATION` / `PROFILE_ASSEMBLY_FAILED` / `DOCUMENT_UNREADABLE`. Drives the triage UI layout. |
| `Question` | NVARCHAR(MAX) | NULL | Human-readable question posed to the reviewer, generated by the Planning Agent at the time the triage item was created. |
| `Options` | NVARCHAR(MAX) | NULL | JSON array of resolution choices (e.g., `["Accept vendor A", "Accept vendor B", "Reject both"]`). The `ISJSON` check constraint ensures only valid JSON is stored. |
| `Status` | NVARCHAR(50) | NOT NULL | `PENDING` (awaiting review) / `IN_REVIEW` (reviewer has opened it) / `RESOLVED` (decision made) / `DISMISSED` (no action taken). The filtered index on `PENDING` makes the overdue SLA query fast. |
| `SlaDeadline` | DATETIME2(7) | NULL | When human review must be completed. Overdue items (Status=PENDING, SlaDeadline < now) trigger escalation notifications. |
| `ResolvedAt` | DATETIME2(7) | NULL | When a reviewer submitted their resolution. |
| `Resolution` | NVARCHAR(MAX) | NULL | The reviewer's decision — either the chosen option from `Options` or free-text justification. Stored for audit trail. |
| `CreatedAt` | DATETIME2(7) | NOT NULL | When this triage item was created. Used for ageing analysis and queue reporting. |

---

## Table 6: `WorkflowIndex` ⚠️ V2 Only — Not Created in V1

**Purpose:** Tracks every workflow execution instance for crash recovery, observability, and status lookup. In V1, crash recovery is handled by reading local `workflow.json` + `state.json` files. In V2 (Azure Blob deployment), this table provides fast workflow status without reading blob files for every query. The `WorkflowBlobPath` column stores the exact blob path to the workflow's JSON files for when full detail is needed.

```sql
CREATE TABLE WorkflowIndex (
    WorkflowId          NVARCHAR(100)    NOT NULL  CONSTRAINT PK_WorkflowIndex PRIMARY KEY,
    ProgrammeId         NVARCHAR(50)     NOT NULL,
    UserId              NVARCHAR(50)     NOT NULL,
    VendorId            NVARCHAR(50)     NULL,

    WorkflowType        NVARCHAR(50)     NOT NULL,
    Status              NVARCHAR(20)     NOT NULL,
    CurrentStepId       NVARCHAR(20)     NULL,

    Version             INT              NOT NULL  DEFAULT 1,
    ReplanningCount     INT              NOT NULL  DEFAULT 0,

    CreatedAt           DATETIME2(7)     NOT NULL,
    LastUpdated         DATETIME2(7)     NOT NULL,

    WorkflowBlobPath    NVARCHAR(500)    NOT NULL
);

CREATE INDEX IX_WorkflowIndex_Status      ON WorkflowIndex (Status);
CREATE INDEX IX_WorkflowIndex_VendorId    ON WorkflowIndex (VendorId);
CREATE INDEX IX_WorkflowIndex_ProgrammeId ON WorkflowIndex (ProgrammeId);
CREATE INDEX IX_WorkflowIndex_UserId      ON WorkflowIndex (UserId);
```

### Column Reference

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `WorkflowId` | NVARCHAR(100) | NOT NULL | Primary key. Human-readable unique ID encoding type, vendor, and epoch (e.g., `wf-enrich-V-ABCD-001-1696156800`). |
| `ProgrammeId` | NVARCHAR(50) | NOT NULL | Which programme this workflow belongs to. |
| `UserId` | NVARCHAR(50) | NOT NULL | Denormalised for fast per-user workflow listing without joining. |
| `VendorId` | NVARCHAR(50) | NULL | Which vendor this workflow is for. Null for programme-level workflows (e.g., intake batch workflows). |
| `WorkflowType` | NVARCHAR(50) | NOT NULL | `INTAKE_INVESTIGATION` / `ENRICHMENT` / `RS_DATA_GATHERING` / `ANALYSIS`. Determines which tool registry is used. |
| `Status` | NVARCHAR(20) | NOT NULL | `NOT_STARTED` / `IN_PROGRESS` / `COMPLETED` / `FAILED` / `BLOCKED`. Used by observability dashboard and crash recovery scanner. |
| `CurrentStepId` | NVARCHAR(20) | NULL | The step currently executing (e.g., `s3_aggregate`). Updated at each step transition. Used for live progress display. |
| `Version` | INT | NOT NULL | Incremented each time the Planning Agent replans this workflow. Crash recovery always loads the latest version. |
| `ReplanningCount` | INT | NOT NULL | Total replanning events across the workflow lifetime. High counts signal consistently ambiguous vendors or data quality issues. |
| `CreatedAt` | DATETIME2(7) | NOT NULL | When the workflow was first created by the Planning Agent. |
| `LastUpdated` | DATETIME2(7) | NOT NULL | Last time status or step changed. Used to detect stale/hung workflows. |
| `WorkflowBlobPath` | NVARCHAR(500) | NOT NULL | Full blob path to this workflow's `workflow.json` (e.g., `User001/PROG-001/workflows/wf-enrich-V-ABCD-001-.../workflow.json`). Used to fetch workflow detail on demand. |

---

## sync_to_db Mapping (Complete)

All workspace file writes go through `atomic_write()`, which calls `sync_to_db()` after every successful commit. This table documents every handler — the triggering file, the target table, and the exact columns updated.

| Workspace file written | Table | Operation | Columns set |
|---|---|---|---|
| `entity.md` (first write) | VendorIntelligence | INSERT | VendorId, ProgrammeId, UserId, VendorName, InputName, DataClass, IdentityConfidence, Category, Status='NEEDS_ACTION', NextActionDue=now, CreatedAt, UpdatedAt |
| `entity.md` (update) | VendorIntelligence | UPDATE | VendorName, DataClass, IdentityConfidence, Category, UpdatedAt |
| `coverage.md` | VendorIntelligence | UPDATE | PcsScore (from `overall_pcs`), UpdatedAt |
| `action_queue.md` | VendorIntelligence | UPDATE | Status, NextActionDue, LastRunAt, UpdatedAt |
| `vendor_profile.md` | VendorIntelligence | UPDATE | Category, Subcategory, VendorType, HqCountry, CompanySizeBand, ProfileStatus, LastEnrichedAt, UpdatedAt |
| `relationship_spend_profile.md` | VendorIntelligence | UPDATE | RsLastUpdated, SpendTotalUsd, DependencyTier, RelationshipType, UpdatedAt |
| `analysis_result.md` | VendorIntelligence | UPDATE | CriScore, HealthBand, VendorState, LastAnalysedAt, UpdatedAt |
| `workflow.json` (V2) | WorkflowIndex | UPSERT | WorkflowId, ProgrammeId, UserId, VendorId, WorkflowType, Status, Version, ReplanningCount, LastUpdated, WorkflowBlobPath |
| `state.json` (V2) | WorkflowIndex | UPDATE | Status, CurrentStepId, LastUpdated |

**Note:** `sync_to_db()` is called **explicitly** after `atomic_write()` in the tool code. In V1 the sync inside `atomic_write()` is a no-op placeholder. Tools call `sync_to_db()` as a separate explicit step. See `rs_profile_assembler_spec.md` for the pattern.

---

## Key T-SQL Queries

### Primary Scheduler Query — `get_due_vendors`

Returns the next vendors the VW Agent should process. Filters out statuses that indicate the vendor is waiting on something external. Orders by tier (TIER_1 first, nulls last) then by lowest PCS score (most incomplete vendors processed first).

```sql
SELECT TOP (@limit) VendorId
FROM VendorIntelligence
WHERE ProgrammeId = @programme_id
  AND NextActionDue <= GETUTCDATE()
  AND Status NOT IN (
      'WAITING_HUMAN_GATE', 'CHECKIN_SENT', 'SURVEY_PENDING', 'COMPLETE', 'PAUSED'
  )
ORDER BY
    CASE WHEN Tier IS NULL THEN 1 ELSE 0 END,  -- NULLS LAST: tiered vendors first
    Tier DESC,                                  -- TIER_1 > TIER_2 > TIER_3
    PcsScore ASC;                               -- Lowest completeness processed first
```

### Idempotent Vendor Insert — `insert_vendor`

Inserts a new vendor row only if it does not already exist. Safe to call multiple times (idempotent). Replaces PostgreSQL `ON CONFLICT DO NOTHING`.

```sql
IF NOT EXISTS (SELECT 1 FROM VendorIntelligence WHERE VendorId = @vendor_id)
BEGIN
    INSERT INTO VendorIntelligence
        (VendorId, ProgrammeId, UserId, VendorName, InputName, DataClass,
         IdentityConfidence, Status, NextActionDue, CreatedAt, UpdatedAt)
    VALUES
        (@vendor_id, @programme_id, @user_id, @vendor_name, @input_name, @data_class,
         @identity_confidence, 'NEEDS_ACTION', GETUTCDATE(), GETUTCDATE(), GETUTCDATE());
END;
```

### Vendor Status Update — `update_vendor_status`

Updates scheduling fields after each VW Agent tick. Always sets `UpdatedAt`.

```sql
UPDATE VendorIntelligence
SET Status         = @status,
    NextActionDue  = @next_action_due,
    UpdatedAt      = GETUTCDATE()
WHERE VendorId = @vendor_id;
```

### Vendors Needing Re-enrichment — `get_vendors_needing_enrichment`

Finds vendors whose P2 enrichment is absent or stale (older than 90 days).

```sql
SELECT VendorId FROM VendorIntelligence
WHERE ProgrammeId = @programme_id
  AND (
      ProfileStatus IS NULL
      OR LastEnrichedAt IS NULL
      OR LastEnrichedAt < DATEADD(day, -90, GETUTCDATE())
  );
```

### Vendors Needing Re-analysis — `get_vendors_needing_analysis`

Finds vendors whose P4 analysis is absent or stale (older than 30 days).
Only runs on vendors where P3 is complete (RelationshipType is not null).

```sql
SELECT VendorId FROM VendorIntelligence
WHERE ProgrammeId = @programme_id
  AND RelationshipType IS NOT NULL
  AND (
      LastAnalysedAt IS NULL
      OR LastAnalysedAt < DATEADD(day, -30, GETUTCDATE())
  );
```

### Vendors by Health Band — `get_vendors_by_health`

Returns all vendors in a given health band for dashboard display and escalation routing.

```sql
SELECT VendorId, VendorName, CriScore, HealthBand, VendorState, LastAnalysedAt
FROM VendorIntelligence
WHERE ProgrammeId = @programme_id
  AND HealthBand = @health_band
ORDER BY CriScore ASC;   -- Worst performers first within band
```

### Overdue Triage SLA Items — `get_overdue_triage`

Returns all pending triage items past their SLA deadline for a user. Uses the filtered index on `Status = 'PENDING'`.

```sql
SELECT TriageId, VendorId, TriageType, SlaDeadline
FROM TriageItem
WHERE Status = 'PENDING'
  AND SlaDeadline < GETUTCDATE()
  AND UserId = @user_id
ORDER BY SlaDeadline ASC;
```

---

## Table Creation Order (SSMS)

No foreign key constraints are enforced. The database is a projection layer — enforcing FK constraints would cause failures when workspace files are written in unexpected orders. All references are soft (by convention only).

```
1. UserAccount           — no dependencies
2. ProgrammeRun          — soft ref to UserAccount.UserId
3. VendorIntelligence    — soft ref to ProgrammeRun.ProgrammeId, UserAccount.UserId
4. VendorCheckin         — soft ref to VendorIntelligence.VendorId
5. TriageItem            — soft ref to VendorIntelligence.VendorId
6. WorkflowIndex         — V2 ONLY: create after V2 migration session
```

---

## Migration Sections

### V1 Baseline — Create Tables 1–5

Run in order: UserAccount → ProgrammeRun → VendorIntelligence → VendorCheckin → TriageItem.

The `VendorIntelligence` V1 baseline includes only the columns that exist in the current `models.py` plus the new `UserId` column. P2, P3, and P4 column groups are added in subsequent migrations.

### V2 Enrichment Migration — Add P2 Columns

After P2 enrichment tooling is deployed:

```sql
ALTER TABLE VendorIntelligence
ADD ProfileStatus    NVARCHAR(50)  NULL,
    LastEnrichedAt   DATETIME2(7)  NULL,
    Subcategory      NVARCHAR(100) NULL,
    VendorType       NVARCHAR(50)  NULL,
    HqCountry        NVARCHAR(10)  NULL,
    CompanySizeBand  NVARCHAR(20)  NULL;
```

Then create `WorkflowIndex`:

```sql
-- Run the CREATE TABLE WorkflowIndex statement from Table 6 above
```

### P3 Migration — Add Relationship & Spend Columns

After P3 tooling is deployed:

```sql
ALTER TABLE VendorIntelligence
ADD RsLastUpdated    DATETIME2(7)  NULL,
    SpendTotalUsd    DECIMAL(18,2) NULL,
    DependencyTier   NVARCHAR(20)  NULL,
    RelationshipType NVARCHAR(50)  NULL;
```

**Note:** The `sync_to_db()` handler for `relationship_spend_profile.md` must be deployed before running this migration, or the columns will remain null after P3 runs.

### P4 Migration — Add Analysis & Intelligence Columns

After P4 tooling is deployed:

```sql
ALTER TABLE VendorIntelligence
ADD CriScore        INT           NULL,
    HealthBand      NVARCHAR(20)  NULL,
    VendorState     NVARCHAR(20)  NULL,
    LastAnalysedAt  DATETIME2(7)  NULL;

CREATE INDEX IX_VI_HealthBand ON VendorIntelligence (HealthBand)
    WHERE HealthBand IS NOT NULL;
```

**Note:** The `sync_to_db()` handler for `analysis_result.md` must be deployed before running this migration, or the columns will remain null after P4 runs. This follows the same pattern as the P3 migration.
