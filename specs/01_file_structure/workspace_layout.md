# Workspace Layout

## Top-Level Structure

```
workspace/
  {programme_id}/
    programme_run/             — Programme-wide artifacts
    workflows/                 — V2: all workflows (intake + enrichment)
    v-{vendor_id}/             — Per-vendor workspaces
```

---

## Programme Run

```
workspace/{programme_id}/programme_run/
  programme_plan.md              — Planning Agent strategy
  vendor_register.md             — All confirmed vendors
  deduplication_report.md        — Summary of dedup decisions
  triage_queue.md                — Human review items
  run_log.md                     — Run summary
  enrichment_log.md              — V2: per-vendor enrichment outcomes
  brain_update_queue.md          — V2: pending Brain update suggestions
  intake_plans/
    IP-{candidate_key}-{n:03d}.md   — Per-candidate investigation plan (Process 1, .md only)
```

---

## Workflows (V2)

```
workspace/{programme_id}/workflows/
  {workflow_id}/
    workflow.json    ← EXECUTABLE TRUTH (Planning Agent writes)
    state.json       ← EXECUTION STATE (RuntimeEngine updates)
    plan.md          ← AUDIT TRAIL (PlanRenderer derives)
```

**Workflow ID conventions:**
- Intake investigation: `wf-intake-{candidate_key}-{timestamp}`
- Enrichment: `wf-enrich-{vendor_id}-{timestamp}`

---

## Per-Vendor Workspace

```
workspace/{programme_id}/v-{vendor_id}/
  identity/
    entity.md                    — Canonical identity (IMMUTABLE input_name)
    gate_results.md              — Investigation gate results

  cost_file/
    spend.md                     — ERP spend signal
    contract.md                  — Contract terms (OBSERVED or NOT_FOUND)
    coverage.md                  — PCS, gaps, enrichment ledger

  profile/                       [NEW V2]
    vendor_profile.md            — Enriched profile from Process 2

  evidence/
    ev-{type}-{id}.md            — Evidence files (IMMUTABLE)

  execution/
    ledger.md                    — Action ledger (append-only)
```

---

## File Status Conventions

Every workspace .md file has a `status` field:

| Status | Meaning |
|---|---|
| `OBSERVED` | Confirmed from a reliable source |
| `INFERRED` | Derived from another field via documented rule |
| `NOT_FOUND` | No evidence found |
| `PROVISIONAL` | Confidence below threshold |
| `CONFLICT` | Multiple sources disagree |

---

## What Gets Written When

### Process 1 (intake)

| File | Written By | When |
|---|---|---|
| `programme_plan.md` | Planning Agent | Start of intake |
| `IP-*.md` | Planning Agent | Per INVESTIGATE candidate |
| `entity.md` | workspace_builder | At entity_decision_and_shell_creation |
| `spend.md` | workspace_builder | Same time as entity.md |
| `contract.md` | workspace_builder | Same time as entity.md |
| `coverage.md` | workspace_builder | Same time as entity.md |
| `ev-*.md` | workspace_builder | Per linked document |
| `ledger.md` | workspace_builder | INTAKE_COMPLETED action |
| `vendor_register.md` | intake_orchestrator | End of intake |
| `triage_queue.md` | intake_orchestrator | End of intake |

### Process 2 (enrichment) — V2

| File | Written By | When |
|---|---|---|
| `workflow.json` | Planning Agent | enrichment_orchestrator start |
| `state.json` | RuntimeEngine | After every step |
| `plan.md` | PlanRenderer | After every state change |
| `vendor_profile.md` | enriched_profile_creator | At Tool 5 commit |
| `coverage.md` (update) | enriched_profile_creator | After write — enrichment ledger entry |
| `enrichment_log.md` | enrichment_orchestrator | End of vendor enrichment |
| `brain_update_queue.md` | enrichment_orchestrator | When Brain suggestions exist |

---

## Immutability Rules

1. `entity.md` — `input_name` field is **IMMUTABLE** after creation. Other fields can be updated.
2. `evidence/ev-*.md` — **IMMUTABLE** after creation. Evidence is never overwritten.
3. `ledger.md` — **APPEND-ONLY**. Never rewritten.
4. `workflow.json` — **REVISABLE** via `apply_revision()`. Version increments. Completed steps preserved.
5. `state.json` — **REVISABLE** via atomic_write. Step records once added are never removed.

---

## V2 Crash Recovery Contract

For any workflow_id, the truth is in two files:
- `workflow.json` → what should happen
- `state.json` → what has happened

On restart:
1. Load both files
2. Find next runnable step (PENDING + dependencies met)
3. Resume execution
4. Steps already DONE are never re-executed
