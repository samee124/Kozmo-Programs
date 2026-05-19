# Database Schema

> **IMPORTANT — PostgreSQL Reference Only**
> This file documents the V1 development schema using PostgreSQL. It contains known gaps and is **not the production target**.
> **Production target is SQL Server (MSSQL).** For the authoritative production schema with full DDL, correct types, all tables, column purpose documentation, and migration scripts, see:
> `specs/02_database/mssql_schema_spec.md`

## Overview

PostgreSQL via SQLAlchemy 2.0. Schema is scheduling-focused — the source of truth lives in workspace .md files. The DB caches structured columns from .md files via `sync_to_db()` to enable fast queries.

---

## Tables

### vendor_intelligence

Primary table — one row per confirmed vendor.

```sql
CREATE TABLE vendor_intelligence (
  vendor_id           VARCHAR(50) PRIMARY KEY,
  programme_id        VARCHAR(50) NOT NULL,
  comparison_key      VARCHAR(255) NOT NULL,
  canonical_name      VARCHAR(255) NOT NULL,
  input_name          VARCHAR(255) NOT NULL,

  status              VARCHAR(50) NOT NULL,          -- NEEDS_ACTION / IN_PROGRESS / etc.
  data_class          VARCHAR(20) NOT NULL,          -- CLASS_A / CLASS_B / CLASS_C / CLASS_D
  next_action_due     TIMESTAMP,

  pcs_score           INTEGER NOT NULL DEFAULT 0,
  identity_confidence FLOAT NOT NULL,

  -- V2 additions
  profile_status      VARCHAR(50),                   -- ENRICHED / PARTIALLY_ENRICHED / PROVISIONAL / FAILED_ENRICHMENT
  last_enriched_at    TIMESTAMP,
  category            VARCHAR(100),
  subcategory         VARCHAR(100),
  vendor_type         VARCHAR(50),
  hq_country          VARCHAR(10),
  company_size_band   VARCHAR(20),

  created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMP NOT NULL DEFAULT NOW(),

  UNIQUE (programme_id, comparison_key)
);

CREATE INDEX idx_vendor_status ON vendor_intelligence (status);
CREATE INDEX idx_vendor_next_action ON vendor_intelligence (next_action_due);
CREATE INDEX idx_vendor_programme ON vendor_intelligence (programme_id);
CREATE INDEX idx_vendor_profile_status ON vendor_intelligence (profile_status);
CREATE INDEX idx_vendor_last_enriched ON vendor_intelligence (last_enriched_at);
```

### workflow_index (V2)

Cache of active workflows for fast lookup. Source of truth is still workflow.json + state.json.

```sql
CREATE TABLE workflow_index (
  workflow_id         VARCHAR(100) PRIMARY KEY,
  programme_id        VARCHAR(50) NOT NULL,
  vendor_id           VARCHAR(50),
  workflow_type       VARCHAR(50) NOT NULL,          -- INTAKE_INVESTIGATION / ENRICHMENT / etc.
  status              VARCHAR(20) NOT NULL,          -- NOT_STARTED / IN_PROGRESS / COMPLETED / FAILED / BLOCKED
  current_step_id     VARCHAR(20),
  version             INTEGER NOT NULL DEFAULT 1,
  replanning_count    INTEGER NOT NULL DEFAULT 0,
  created_at          TIMESTAMP NOT NULL,
  last_updated        TIMESTAMP NOT NULL,
  workflow_path       VARCHAR(500) NOT NULL          -- path to workflow.json on disk
);

CREATE INDEX idx_workflow_status ON workflow_index (status);
CREATE INDEX idx_workflow_vendor ON workflow_index (vendor_id);
CREATE INDEX idx_workflow_programme ON workflow_index (programme_id);
```

---

## sync_to_db Mapping

| Markdown File | DB Update |
|---|---|
| `entity.md` (on create) | INSERT INTO vendor_intelligence |
| `entity.md` (on update) | UPDATE canonical_name, identity_confidence, data_class |
| `coverage.md` (intake) | UPDATE pcs_score |
| `coverage.md` (after enrichment) | UPDATE pcs_score, profile_status, last_enriched_at, category |
| `vendor_profile.md` (V2) | UPDATE category, subcategory, vendor_type, hq_country, company_size_band, profile_status, last_enriched_at |
| `workflow.json` (V2) | UPSERT workflow_index (workflow_id, status, version, replanning_count, last_updated) |
| `state.json` (V2) | UPDATE workflow_index SET status, current_step_id, last_updated |

---

## Queries (V2 additions)

```python
def get_vendors_needing_enrichment(programme_id: str) -> list[VendorRow]:
    """
    Returns vendors where:
      - profile_status IS NULL  (never enriched)
      - OR last_enriched_at IS NULL
      - OR last_enriched_at < NOW() - INTERVAL '90 days'  (stale)
    """

def get_active_workflows(programme_id: str) -> list[WorkflowIndexRow]:
    """Returns workflows with status IN ('NOT_STARTED', 'IN_PROGRESS')."""

def get_blocked_workflows() -> list[WorkflowIndexRow]:
    """Returns workflows where status = 'BLOCKED' — need human attention."""
```

---

## Migration

V1 → V2 requires adding columns to `vendor_intelligence`:

```sql
ALTER TABLE vendor_intelligence
  ADD COLUMN profile_status VARCHAR(50),
  ADD COLUMN last_enriched_at TIMESTAMP,
  ADD COLUMN category VARCHAR(100),
  ADD COLUMN subcategory VARCHAR(100),
  ADD COLUMN vendor_type VARCHAR(50),
  ADD COLUMN hq_country VARCHAR(10),
  ADD COLUMN company_size_band VARCHAR(20);

CREATE INDEX idx_vendor_profile_status ON vendor_intelligence (profile_status);
CREATE INDEX idx_vendor_last_enriched ON vendor_intelligence (last_enriched_at);
```

And creating `workflow_index`:

```sql
CREATE TABLE workflow_index (
  -- as defined above
);
```

Alembic migration script: `db/migrations/v2_runtime_enrichment.py`.
