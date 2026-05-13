# File Ownership

## The Single-Writer Rule

Each file has exactly one writer. Violation raises `FileOwnershipViolation`. HALT.

---

## Process 1 (intake) Writers

| File | Writer |
|---|---|
| `programme_plan.md` | Planning Agent (via `plan_writer.py`) |
| `IP-*.md` | Planning Agent (via `plan_writer.py`) |
| `entity.md` | `workspace_builder` (only at intake) |
| `gate_results.md` | `workspace_builder` |
| `spend.md` | `workspace_builder` at intake; Vendor Manager Agent thereafter |
| `contract.md` | `workspace_builder` at intake; Vendor Manager Agent thereafter |
| `coverage.md` | `workspace_builder` at intake; Vendor Manager Agent / `enriched_profile_creator` thereafter |
| `evidence/ev-*.md` | `workspace_builder` (IMMUTABLE after creation) |
| `ledger.md` | Append-only via `append_md()` — any agent can append, never rewrite |
| `vendor_register.md` | `intake_orchestrator` |
| `triage_queue.md` | `intake_orchestrator` |
| `deduplication_report.md` | `intake_orchestrator` |
| `run_log.md` | `intake_orchestrator` |

---

## V2 Runtime Writers

| File | Writer |
|---|---|
| `workflows/{wf_id}/workflow.json` | Planning Agent (create + apply_revision only) |
| `workflows/{wf_id}/state.json` | RuntimeEngine (atomic writes after every step) |
| `workflows/{wf_id}/plan.md` | PlanRenderer (derived from workflow + state) |

**No other component writes to workflow.json or state.json.**

---

## Process 2 (enrichment) Writers — V2

| File | Writer |
|---|---|
| `profile/vendor_profile.md` | `enriched_profile_creator` (Tool 5) ONLY |
| `coverage.md` (enrichment ledger entry) | `enriched_profile_creator` via append |
| `enrichment_log.md` | `enrichment_orchestrator` |
| `brain_update_queue.md` | `enrichment_orchestrator` |
| Triage queue updates | `enrichment_orchestrator` (append) |

---

## Vendor Manager Agent (V3)

Once V3 is built, Vendor Manager Agent becomes the sole post-intake writer of vendor workspace files. It orchestrates:

- `spend.md` updates from new ERP signals
- `contract.md` updates from new contract documents
- `coverage.md` updates after every workspace change
- New evidence file creation
- `ledger.md` appends

---

## What Each Agent Cannot Do

### Planning Agent
- Cannot write workspace files (entity, spend, contract, coverage, evidence)
- Cannot execute steps
- Can only write plan files (programme_plan, IP-*, workflow.json revisions)

### Research Agent
- Cannot write any file
- Cannot interpret results
- Returns raw evidence only

### Analysis Agent
- Cannot write any file
- Cannot fetch new evidence
- Returns structured dicts only

### Program Orchestrator
- Cannot plan
- Cannot research
- Can only write programme-level files (run_log, triage_queue, enrichment_log)

### Vendor Manager Agent
- Cannot run during intake
- Cannot bypass Planning Agent for strategic decisions
- Sole writer of vendor workspace files post-intake

---

## Brain Files (Special)

| File | Writer |
|---|---|
| `Brain/known_vendors.json` | Manual curation only (V1) / Program Orchestration Agent review (V2+) |
| `Brain/rebrand_map.json` | Same |
| `Brain/alias_map.json` | Same |
| `Brain/acquisition_map.json` | Same |
| `Brain/brand_map.json` | Same |

**No tool writes directly to Brain.** Tools suggest updates via `BrainUpdateSuggestion`. Program Orchestration Agent reviews and commits.

---

## Atomic Write Contract

Every write goes through `atomic_write()` from `src/cobalt/core/atomic_write.py`.

The function:
1. Writes to `{path}.tmp`
2. Calls `tmp.replace(path)` (Windows-safe atomic rename)
3. Triggers `sync_to_db()` if the file maps to a DB column

Violation:
- Direct `open().write()` → `FileOwnershipViolation`
- Direct file I/O outside `atomic_write()` → forbidden
