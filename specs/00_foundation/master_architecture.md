# Cobalt — Master Architecture

## Identity

- **Platform name:** Cobalt (never BlueSalt, never Kozmo)
- **Package:** `src/cobalt/`
- **Python:** 3.11+
- **LLM:** OpenAI gpt-4o via openai SDK
- **Storage:** Local filesystem (workspace/) + PostgreSQL (SQLAlchemy 2.0)
- **Tests:** pytest, never `bare except`, never direct file I/O

---

## The Two Processes

### Process 1 — Vendor Entity Formation & Resolution (intake)

Takes raw source material, resolves it to a canonical vendor identity, writes an entity shell to the workspace. Runs once per programme at intake. **Complete in V1.**

| Tool | Purpose |
|---|---|
| `source_intake` | Parse vendor list + Google Drive PDFs into unified candidates |
| `candidate_screening` | Clean, normalize, classify entity types |
| `entity_resolution` | Match against Brain, dedup, build SignalProfile |
| `external_validation` | Execute investigation plans via STEP_REGISTRY |
| `entity_decision_and_shell_creation` | Create vendor workspace from confirmed candidates |

### Process 2 — Vendor Profile Enrichment

Takes an entity shell from Process 1 and builds a research-backed profile. Runs periodically per vendor, triggered by Vendor Manager Agent.

| Tool | Purpose |
|---|---|
| `enrichment_readiness_check` | Gate — decides depth, reviews known facts |
| `external_source_collector` | Gathers raw evidence (web, website, registry, news) |
| `attribute_extractor` | Extracts structured attributes + classifies (Analysis Agent) |
| `relationship_and_lifecycle_mapper` | Maps parent/subsidiary, detects rebrand/acquisition |
| `enriched_profile_creator` | Reconciles, writes `vendor_profile.md` |

---

## The Six Agents

| Agent | Responsibility | Plans? | Researches? | Writes Files? |
|---|---|---|---|---|
| Program Orchestrator | Drives + sequences agents | No | No | Programme files only |
| Planning Agent | Writes every plan and workflow | Yes (always) | No | Plan files only |
| Research Agent | Raw evidence collection only | No | Yes (always) | Connector logs only |
| Analysis Agent | Extracts structure from raw evidence | No | No | Returns dicts only |
| Vendor Manager Agent | Per-vendor lifecycle, runs DE | Calls Planning Agent | No | All workspace files post-intake |
| Campaign Manager | Campaign execution | No | No | Campaign + outcome files |

---

## The Runtime Layer (new in V2)

Three persistent layers separated cleanly:

```
workspace/{programme_id}/workflows/{wf_id}/
  workflow.json   ← EXECUTABLE TRUTH (Planning Agent writes, revises)
  state.json      ← EXECUTION STATE (RuntimeEngine updates atomically)
  plan.md         ← AUDIT TRAIL (PlanRenderer derives from above two)
```

### Core invariants

- `workflow.json` is the single source of executable truth
- `state.json` is the only thing executor writes during a run
- `plan.md` is rendered from the other two — never read by execution
- In-memory objects are deserialised views — crash = reload from disk
- Every step result is persisted before any replanning decision

### Five new components

| Component | Path | Purpose |
|---|---|---|
| `WorkflowDefinition` | `src/cobalt/runtime/workflow_definition.py` | Step graph, dependencies, conditions, retry policies |
| `ExecutionState` | `src/cobalt/runtime/execution_state.py` | Step status, results, signals, timestamps |
| `RuntimeEngine` | `src/cobalt/runtime/runtime_engine.py` | Execution loop, crash recovery, replan trigger |
| `PlanRenderer` | `src/cobalt/runtime/plan_renderer.py` | Renders workflow + state → plan.md |
| Planning Agent additions | `src/cobalt/agents/planning_agent.py` | `create_workflow`, `evaluate_step`, `replan` |

---

## The Five Capabilities Enabled

1. **Crash recovery** — restart reads workflow.json + state.json, resumes from last DONE step
2. **Adaptive intelligence** — Planning Agent revises remaining workflow when step results change the picture
3. **Full auditability** — workflow.json shows every revision with rationale; state.json shows every step result with timestamps
4. **Replayability** — workflow.json v1 preserved; state can be reset; deterministic reproduction
5. **Observability** — query state.json for any vendor at any time

---

## Directory Structure (V2)

```
src/cobalt/
  core/                 exceptions, llm_call, atomic_write, file_system
  db/                   models, sync_to_db, queries
  brain/                loader (5 files: known_vendors, rebrand_map,
                                alias_map, acquisition_map, brand_map)
  models/schemas/       investigation_plan, signal_profile, intake_result,
                        campaign_plan, workflow_schema, enrichment_schema
  runtime/              workflow_definition, execution_state,
                        runtime_engine, plan_renderer            [NEW V2]
  intake/               _cleaner, _normalizer, _signal_collector,
                        steps/ (Process 1 step registry)
  tools/                Process 1 (5 tools) + Process 2 (5 tools)
  agents/               planning_agent, research_agent, analysis_agent,
                        vendor_manager_agent                     [NEW V3]
  orchestrator/         intake_orchestrator,
                        enrichment_orchestrator                  [NEW V2]
  workspace/            builder, plan_writer
  api/                  server (FastAPI surface)
```

---

## Ten Non-Negotiable Rules

1. **Vendor Manager Agent is the only writer to vendor workspace files post-intake.**
2. **Every LLM call goes through `llm_call()` in `src/cobalt/core/llm_call.py`.**
3. **Every file write goes through `atomic_write()` in `src/cobalt/core/atomic_write.py`.**
4. **Ledger append is synchronous.** `LedgerWriteError` = HALT.
5. **One LLM call per Vendor Manager Agent tick for tactical decisions.**
6. **Planning Agent writes every plan and workflow.** Rules engine lives inside Planning Agent.
7. **Research Agent = raw data only.** Analysis Agent = extracts structure.
8. **DB updated via `sync_to_db()` only** — inside `atomic_write()` automatically.
9. **`entity.md` input_name is IMMUTABLE.** `evidence/ev-{id}.md` files are IMMUTABLE.
10. **V3 features are NOT built in V2.** Add to TODO.md and stop.
