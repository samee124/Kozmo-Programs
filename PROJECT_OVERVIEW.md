# Cobalt — Project Overview

## What Is Cobalt?

Cobalt is a Python-based AI platform (V1) for **vendor intelligence and management**. It ingests raw vendor data from spreadsheets and contracts, resolves vendors to canonical identities, enriches them with external research, and manages the ongoing vendor lifecycle through AI agents. The platform is built entirely without LangChain/LangGraph/CrewAI — all orchestration is hand-coded.

- **Package:** `src/cobalt/`
- **Language:** Python 3.11+
- **LLM:** OpenAI `gpt-4o` via the `openai` SDK (all calls through `llm_call()`)
- **Storage (V1 dev):** Local filesystem (`workspace/`) for all vendor files; PostgreSQL (SQLAlchemy 2.0) for scheduling/projection only
- **Storage (Production target):** Azure Blob Storage for workspace files (all paths prefixed by `{UserId}/`); SQL Server (SSMS) as the relational database
- **File writes:** All writes go through `atomic_write()` — tmp-file replace pattern in V1; `blob_client.upload_blob(overwrite=True)` in V2

---

## Three Core Processes

### Process 1 — Vendor Intake (Entity Formation & Resolution)

Runs once per programme. Takes raw input files and resolves them into a canonical vendor workspace.

| Step | Tool | What It Does |
|------|------|-------------|
| 1 | `source_intake` | Reads vendor lists (CSV/Excel) and Google Drive PDFs; extracts vendor names; deduplicates |
| 2 | `candidate_screening` | Cleans, normalises, classifies entity types (company / person / internal / etc.) |
| 3 | `entity_resolution` | Matches against the Brain (known vendors, aliases, rebrands, acquisitions); builds a SignalProfile |
| 4 | `external_validation` | Executes an investigation plan — runs registry lookups, web research, fraud checks, sanctions checks |
| 5 | `entity_decision_and_shell_creation` | Creates the vendor workspace shell from confirmed candidates |

**Output per vendor:** CONFIRMED / TRIAGE_REQUIRED / DISCARDED / BLOCKED

**Programme-level files written:** `vendor_register.md`, `deduplication_report.md`, `triage_queue.md`, `run_log.md`

### Process 2 — Vendor Profile Enrichment

Runs periodically per vendor, triggered by the Vendor Manager Agent. Builds a research-backed profile on top of the entity shell from Process 1.

| Step | Tool | What It Does |
|------|------|-------------|
| s0 | `enrichment_readiness_check` | Gate — decides depth tier, reviews known facts, skips if not due |
| s1 | `external_source_collector` | Gathers raw evidence: web search, website crawl, Companies House, SEC EDGAR, GLEIF, Wikidata, OpenCorporates |
| s2 | `attribute_extractor` | Extracts structured attributes (category, size, HQ, financials, key people) via the Analysis Agent |
| s3 | `relationship_and_lifecycle_mapper` | Maps parent/subsidiary relationships; detects rebrands and acquisitions |
| s4 | `enriched_profile_creator` | Reconciles all evidence; writes `vendor_profile.md`; updates PCS score |

### Process 3 — Relationship & Spend Data Gathering

Runs per vendor after Process 1 confirms the entity. Answers the commercial exposure question: how much do we spend, what do our contracts say, and how dependent are we on this vendor?

| Step | Tool | What It Does |
|------|------|-------------|
| s1 | `structured_data_collector` | Collects raw spend records from three arrival modes: CONNECTOR (ERP stub), FILE_UPLOAD (CSV/Excel), CHECK_IN (vendor self-report) |
| s2 | `document_intelligence` | Extracts structured `ContractTerms` from PDFs and text documents via one LLM call per document |
| s3 | `spend_aggregator` | Aggregates raw records into TTM/YTD/all-time totals, period breakdowns, anomaly signals, and data quality flags — no LLM |
| s4 | `relationship_classifier` | Scores dependency (0.0–1.0) across six signals; classifies as STRATEGIC/PREFERRED/TRANSACTIONAL/INCIDENTAL; LLM only in ambiguous 0.35–0.65 band |
| s5 | `rs_profile_assembler` | Reconciles all outputs; writes `relationship_spend_profile.md`; computes P3 PCS contribution (max 0.20) |

**Output per vendor:** `relationship_spend_profile.md` with spend summary, contract terms, dependency classification, and gap report.

**Data arrival modes:**
- `CONNECTOR` — V1 stub reads JSON from `workspace/{programme}/{vendor}/connectors/`; V2 will integrate real ERP/AP adapters
- `FILE_UPLOAD` — CSV or Excel AP extract; fuzzy vendor name matching with Jaro-Winkler similarity
- `CHECK_IN` — Structured dict from vendor check-in response via VW Agent

**PCS contribution:** P3 adds up to 0.20 to the vendor's Profile Completeness Score. P1 contributes up to 0.53, P2 up to 0.47; PCS is clamped at 1.0 at display.

---

## Architecture: The Six Agents

| Agent | Role | Plans? | Researches? | Writes Files? |
|-------|------|--------|-------------|---------------|
| **Program Orchestrator** | Drives and sequences all agents | No | No | Programme-level files only |
| **Planning Agent** | Writes every plan at every level (programme, investigation, campaign, negotiation) | Always | No | Plan files only |
| **Research Agent** | Collects raw evidence (web, ERP connector, PDF fetch) | No | Always | Connector logs only |
| **Analysis Agent** | Extracts structure from raw evidence; document extraction; entity structuring | No | No | Returns dicts only |
| **Vendor Manager Agent (VW Agent)** | Per-vendor lifecycle after workspace exists; one LLM call per tick for tactical decisions | Calls Planning Agent for strategic | No | All vendor workspace files post-intake |
| **Campaign Manager** | Campaign lifecycle, stage tracking, outcome recording | No | No | Campaign + outcome files |

**APE Mental Model:** STATE → ANALYZE → PLAN → EXECUTE → NEW STATE → REPEAT

---

## Runtime Layer

Three persistent layers per workflow:

```
workspace/{programme_id}/workflows/{wf_id}/
  workflow.json   -- executable truth (Planning Agent writes/revises)
  state.json      -- execution state (RuntimeEngine updates atomically)
  plan.md         -- human-readable audit trail (derived, never read by execution)
```

### Components

| Component | File | Purpose |
|-----------|------|---------|
| `WorkflowDefinition` | `src/cobalt/runtime/workflow_definition.py` | Step graph, dependencies, conditions, retry policies |
| `ExecutionState` | `src/cobalt/runtime/execution_state.py` | Step status, results, signals, timestamps |
| `RuntimeEngine` | `src/cobalt/runtime/runtime_engine.py` | Execution loop, crash recovery, replan trigger |
| `PlanRenderer` | `src/cobalt/runtime/plan_renderer.py` | Renders workflow + state to plan.md |

### RuntimeEngine Behaviour
- Finds next runnable step (all dependencies DONE)
- Evaluates conditions (skips if not met)
- Executes step with retry (`StepRetryable`) or aborts on fatal (`StepFatal`)
- After each step, asks Planning Agent: CONTINUE / REPLAN / ESCALATE_HUMAN / TERMINATE
- Replanning capped at 3 revisions per workflow
- Crashes are recoverable — reload from workflow.json + state.json

---

## The Brain

Pre-loaded knowledge used during entity resolution:

| File | Purpose |
|------|---------|
| `Brain/known_vendors.json` | Canonical vendor names with confidence scores |
| `Brain/rebrand_map.json` | Old name → new canonical name mappings |
| `Brain/alias_map.json` | Common aliases/abbreviations → canonical |
| `Brain/acquisition_map.json` | Acquired-by relationships |
| `Brain/brand_map.json` | Brand names → parent company |

All loaded once at startup by `cobalt.brain.loader.load_brain()` and cached in-process.

---

## Database (PostgreSQL via SQLAlchemy 2.0)

The DB is a **scheduling and projection layer only** — the workspace filesystem is the source of truth.

| Table | Purpose |
|-------|---------|
| `vendor_intelligence` | One row per vendor; tracks status, PCS score, tier, data class, enrichment timestamps, next action due; P3 columns: `rs_last_updated`, `spend_total_usd`, `dependency_tier`, `relationship_type` |
| `programme_runs` | One row per intake run; tracks counts (confirmed/triage/discarded/blocked) |
| `vendor_checkins` | Outbound check-in tracking (sent, deadline, response) |
| `triage_items` | Items requiring human review with SLA deadlines |

All DB writes go through `sync_to_db()` which is called automatically inside `atomic_write()`.

---

## External Data Sources

| Source | Purpose | Auth |
|--------|---------|------|
| Brave Search API | Web search for vendor intelligence | `BRAVE_API_KEY` |
| Companies House (UK) | UK company registry | `COMPANIES_HOUSE_API_KEY` |
| SEC EDGAR (US) | US public financial filings | `SEC_USER_AGENT` (no key) |
| GLEIF | Global LEI registry; authoritative parent/subsidiary for ~2.5M entities | No auth |
| Wikidata | Structured facts for ~100M global entities | No auth |
| OpenCorporates | Global registry aggregator; ~140 jurisdictions | `OPENCORPORATES_API_TOKEN` |

---

## Key Infrastructure

### `atomic_write()` — `src/cobalt/core/atomic_write.py`
1. Serialises dict to YAML front-matter + markdown table (or writes string as-is)
2. Checks immutability — `entity.md` `input_name` field can never change
3. Writes to `<path>.tmp`
4. Runs schema validator (deletes .tmp on failure)
5. `tmp.replace(path)` — Windows-safe atomic swap
6. Calls `sync_to_db()`

### `llm_call()` — `src/cobalt/core/llm_call.py`
- Single entry point for all LLM calls — direct OpenAI usage forbidden elsewhere
- Model locked to `gpt-4o`, temperature 0, max_tokens 2000
- 3 retries with exponential backoff on `openai.APIError`
- Returns parsed dict (JSON) or raw string
- `llm_tool_call()` variant supports OpenAI function-calling

### `append_md()` — `src/cobalt/core/atomic_write.py`
- Append-only ledger writes; any `OSError` raises `LedgerWriteError` (caller must HALT)

---

## Intake Orchestrator — Full Flow

```
run_intake(programme_id, vendor_list_path, documents_path)
  1. Load/create checkpoint.json (crash recovery — skip if COMPLETED)
  2. Boot agents (PlanningAgent, ResearchAgent, AnalysisAgent)
  3. source_intake   → raw candidates from CSV/Excel + Google Drive PDFs
  4. PlanningAgent.write_programme_plan()
  5. candidate_screening  → PASS / REJECT
  6. build_batch_context  → ERP/AP scan + Brain load
  7. entity_resolution    → MATCHED / UNMATCHED_VIABLE / UNMATCHED_AMBIGUOUS / DISCARD
  8. Per-candidate routing:
       DISCARD       → discarded list
       AUTO_CONFIRM  → decide_and_create directly
       INVESTIGATE   → write investigation plan → external_validation → decide_and_create
  9. Write programme report files (always, even after failures)
 10. Mark checkpoint COMPLETED
```

---

## Enrichment Orchestrator — Full Flow

```
run_enrichment(vendor_id, programme_id, declared_depth)
  1. Read entity.md for vendor data and PCS score
  2. enrichment_readiness_check → SKIP / proceed with depth_tier
  3. PlanningAgent.create_workflow(ENRICHMENT)
  4. RuntimeEngine.execute_workflow():
       s1: collect_sources    (COLLECT_SOURCES step type)
       s2: extract_attributes (EXTRACT_ATTRIBUTES step type)
       s3: map_relationships  (MAP_RELATIONSHIPS step type)
       s4: create_profile     (CREATE_PROFILE step type)
  5. Append to enrichment_log.md, brain_update_queue.md, triage_queue.md
```

Crash recovery: each step snapshot is stored in `state.json` so a restart re-uses completed step results.

---

## RS Orchestrator — Full Flow (Process 3)

```
run_rs(vendor_id, programme_id, arrival_modes, uploaded_files, checkin_data, ...)
  1. Gate checks:
       entity.md exists and status=CONFIRMED → proceed (else BLOCKED)
       relationship_spend_profile.md < 30 days old → SKIPPED (fresh)
       No data in any arrival mode → SKIPPED (no_data_available)
       vendor_profile.md missing → warn only (P2_PROFILE_MISSING), do not block
  2. Load entity.md + vendor_profile.md → entity_profile dict, known_facts dict, current_pcs
  3. PlanningAgent.create_workflow(RS_DATA_GATHERING)
  4. RuntimeEngine.execute_workflow():
       s1: structured_data_collector  (COLLECT_RS_DATA step type)
       s2: document_intelligence      (PROCESS_DOCUMENTS step type)      depends: s1
       s3: spend_aggregator           (AGGREGATE_SPEND step type)         depends: s1, s2
       s4: relationship_classifier    (CLASSIFY_RELATIONSHIP step type)   depends: s3
       s5: rs_profile_assembler       (ASSEMBLE_RS_PROFILE step type)     depends: s1–s4
  5. Append to rs_log.md, triage_queue.md
```

Crash recovery: same pattern as enrichment — state.json stores each step's result; restart resumes from first non-DONE step.

---

## Directory Structure

```
src/cobalt/
  core/           llm_call, atomic_write, file_system, exceptions
                  companies_house, sec_edgar, gleif, wikidata, opencorporates, search
                  name_matching, confidence_scorer, gap_analyzer, staleness  [P3 utilities]
  db/             models (SQLAlchemy ORM), queries, sync_to_db
  brain/          loader (5 JSON knowledge files)
  models/schemas/ signal_profile, investigation_plan, intake_result,
                  campaign_plan, enrichment_schema,
                  rs_schema  [P3 dataclasses]
  runtime/        workflow_definition, execution_state, runtime_engine, plan_renderer
  intake/         _cleaner, _normalizer, _signal_collector
                  steps/ (registry lookup, fraud check, web research, sanctions,
                          document extraction, HR overlap, rebrand confirm,
                          merge canonical, route to human)
  tools/          Process 1: source_intake, candidate_screening, entity_resolution,
                             external_validation, entity_decision_and_shell_creation
                  Process 2: enrichment_readiness_check, external_source_collector,
                             attribute_extractor, relationship_and_lifecycle_mapper,
                             enriched_profile_creator
                  Process 3: structured_data_collector, document_intelligence,
                             spend_aggregator, relationship_classifier,
                             rs_profile_assembler
  agents/         planning_agent, research_agent, analysis_agent
  orchestrator/   intake_orchestrator, enrichment_orchestrator, batch_context_builder,
                  rs_orchestrator  [P3 orchestrator]
  api/            server (FastAPI)

Brain/            known_vendors.json, rebrand_map.json, alias_map.json,
                  acquisition_map.json, brand_map.json

specs/            Full design specs for every component
                  07_rs_tools/  [Process 3 specs]
tests/            pytest test suite (agents, brain, models, orchestrator,
                  runtime, tools, integration)
```

---

## Non-Negotiable Rules (enforced in code)

1. VW Agent is the only writer to vendor workspace files after intake
2. Every LLM call goes through `llm_call()` — no direct OpenAI calls anywhere
3. Every file write goes through `atomic_write()` — no direct `open().write()`
4. Ledger append (`append_md`) failures = `LedgerWriteError` = HALT
5. One LLM call per VW Agent tick for tactical decisions
6. Planning Agent writes every plan — Orchestrator never plans
7. Research Agent = raw data only; Analysis Agent = extracts structure
8. DB updated via `sync_to_db()` only, called inside `atomic_write()`
9. `entity.md` `input_name` field is IMMUTABLE after creation
10. V2/V3 features are not built in V1 — deferred to TODO.md

---

## V2 / V3 Roadmap (not built)

**V2:** Real sanctions API (OFAC/EU/UN), real registry connectors, Azure Functions deployment, Google Drive real API, real ERP connector, multilingual search, transliteration, Planning Agent simulation

**V3:** Slow loop (quarterly insight surfacing), cross-domain learning, simulation calibration store, inferential analysis

---

## Entry Points

| Script | Purpose |
|--------|---------|
| `run.py` | Main programme runner |
| `run_intake.py` | Standalone intake pipeline runner |
| `serve_ui.py` | FastAPI UI server |
