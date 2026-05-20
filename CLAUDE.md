# Cobalt — Project Constitution
# READ THIS FIRST. EVERY SESSION. NO EXCEPTIONS.

## Identity
- Platform name: Cobalt (NEVER BlueSalt, NEVER Kozmo — anywhere, ever)
- Package: src/cobalt/
- Python 3.11+
- No LangChain, no LangGraph, no CrewAI, no AutoGen
- LLM: OpenAI gpt-4o via openai SDK
- Storage: Local filesystem (workspace/) for V1
- DB: PostgreSQL via SQLAlchemy 2.0 (scheduling only)
- File writes: always tmp.replace(path) — never tmp.rename(path)
- Search: Brave Search API (BRAVE_API_KEY). Cache: SEARCH_CACHE_DIR. Shared module: cobalt.core.search
- Companies House (UK registry): COMPANIES_HOUSE_API_KEY. Free developer tier, 600 req/5 min. UK vendors only — other jurisdictions return NO_REGISTRY_RECORD gracefully.
- SEC EDGAR (US financial): No auth. Requires User-Agent header (SEC_USER_AGENT env var or default). Module: cobalt.core.sec_edgar. Non-US primary markets return NO_PUBLIC_FINANCIAL_DATA gracefully.
- GLEIF (Legal Entity Identifier registry): No API key required. Set GLEIF_USER_AGENT (or reuse SEC_USER_AGENT). Free public API. Provides authoritative parent/subsidiary relationships for ~2.5M regulated entities globally. Module: cobalt.core.gleif.
- Wikidata (structured facts for ~100M global entities): No auth. Set WIKIDATA_USER_AGENT (or reuse SEC_USER_AGENT). Two-step: search API (name→Q-IDs) then SPARQL (Q-IDs→facts). MEDIUM confidence, DIRECTORY quality. Module: cobalt.core.wikidata.
- OpenCorporates (global registry aggregator): Set OPENCORPORATES_API_TOKEN. Free 500 calls/month. Covers ~140 jurisdictions including EU, Australia, Canada, and US states. Auth via api_token query parameter (not a header). Used as the non-UK registry source and UK fallback when Companies House finds nothing. Module: cobalt.core.opencorporates.

## The Five Process 1 Tools — Names Are Fixed
These names match the design spreadsheet exactly. Never rename them.
  Tool 1: source_intake                    → src/cobalt/tools/source_intake.py
  Tool 2: candidate_screening              → src/cobalt/tools/candidate_screening.py
  Tool 3: entity_resolution               → src/cobalt/tools/entity_resolution.py
  Tool 4: external_validation             → src/cobalt/tools/external_validation.py
  Tool 5: entity_decision_and_shell_creation → src/cobalt/tools/entity_decision_and_shell_creation.py

## The Five Process 3 Tools — Names Are Fixed
  Tool 1 P3: structured_data_collector    → src/cobalt/tools/structured_data_collector.py
  Tool 2 P3: document_intelligence        → src/cobalt/tools/document_intelligence.py
  Tool 3 P3: spend_aggregator             → src/cobalt/tools/spend_aggregator.py
  Tool 4 P3: relationship_classifier      → src/cobalt/tools/relationship_classifier.py
  Tool 5 P3: rs_profile_assembler         → src/cobalt/tools/rs_profile_assembler.py

## The Seven Process 4 Tools — Names Are Fixed
  Tool 1 P4: evidence_validator           → src/cobalt/tools/evidence_validator.py
  Tool 2 P4: scoring_engine               → src/cobalt/tools/scoring_engine.py
  Tool 3 P4: commercial_analyser          → src/cobalt/tools/commercial_analyser.py
  Tool 4 P4: trend_analyser               → src/cobalt/tools/trend_analyser.py
  Tool 5 P4: inquiry_engine               → src/cobalt/tools/inquiry_engine.py
  Tool 6 P4: finding_engine               → src/cobalt/tools/finding_engine.py
  Tool 7 P4: narrative_engine             → src/cobalt/tools/narrative_engine.py

  Execution order is STRICT and enforced by analysis_orchestrator:
    evidence_validator → commercial_analyser → inquiry_engine →
    scoring_engine → trend_analyser → finding_engine → narrative_engine

  signal_processor does not exist yet.
  All P4 tools accept signal_bundle=None. Handle gracefully — no crash.

## Directory Structure
src/cobalt/
  core/           exceptions, llm_call, atomic_write, file_system
  db/             models, sync_to_db, queries
  brain/          loader
  models/         schemas/
  tools/          THE FIVE TOOLS (public, Planning Agent selects these)
  intake/         Private utilities called by tools
    _cleaner.py
    _normalizer.py
    _signal_collector.py
    _executor.py
    steps/
  agents/         planning_agent, research_agent, analysis_agent,
                  vw_agent, campaign_manager
  orchestrator/   intake_orchestrator, batch_context_builder
  workspace/      builder, plan_writer

## The Six Agents — Exact Responsibilities

### Program Orchestrator
  DOES: Drives. Sequences. Calls agents. Routes outcomes.
  NEVER: Plans. Researches. Analyses. Writes workspace files.

### Planning Agent
  DOES: Writes every plan at every level.
        Level 1: Programme plan (Orchestrator calls at start)
        Level 2: Investigation plan (Orchestrator calls per candidate)
        Level 3: Campaign plan (VW Agent calls at Stage 4)
        Level 4: Mid-campaign update (VW Agent calls when strategy changes)
        Level 5: Negotiation plan (VW Agent calls at P3)
  NEVER: Executes. Researches. Writes workspace files directly.
  RULE:  IF a plan file needs to exist → Planning Agent created it.

### Research Agent
  DOES: Collects raw evidence only.
        Web research, ERP connector, fetch raw PDF text.
  NEVER: Interprets. Extracts structure. Writes workspace files.
  RULE:  Returns raw data only. Stops at collection.

### Analysis Agent
  DOES: Extracts meaning from raw evidence.
        Entity structuring, document extraction, confidence scoring.
  NEVER: Collects evidence. Writes workspace files.
  RULE:  Document extraction lives HERE — not in Research Agent.

### VW Agent
  DOES: Per-vendor lifecycle after workspace exists.
        ONE LLM call per tick for TACTICAL decisions.
        Calls Planning Agent for STRATEGIC decisions.
  NEVER: Runs during intake. Never before workspace exists.

  TACTICAL (VW Agent tick): next connector, dispatch check-in,
    advance campaign step, process response.
  STRATEGIC (calls Planning Agent): Stage 4 campaigns,
    Stage 5 strategy revision, P3 negotiation.

### Campaign Manager
  DOES: Campaign lifecycle. Stage tracking. Outcome recording.
  NEVER: Plans strategy. Researches. Writes identity files.

## The Mental Model — APE Everywhere
STATE → ANALYZE → PLAN → EXECUTE → NEW STATE → REPEAT

## Spec Index — Read Before Building Anything
| Building...                        | Read spec first                                      |
|------------------------------------|------------------------------------------------------|
| Core infrastructure                | specs/08_api_contracts/llm_call_spec.md              |
|                                    | specs/08_api_contracts/atomic_write_spec.md          |
| DB models (PostgreSQL dev ref)     | specs/02_database/db_schema.md                       |
| DB models (SQL Server production)  | specs/02_database/mssql_schema_spec.md               |
| Azure Blob storage layout          | specs/01_file_structure/azure_blob_spec.md           |
| Tool 1: source_intake              | specs/06_sparse_data/source_processor_spec.md        |
| Tool 2: candidate_screening        | specs/06_sparse_data/normalization_spec.md           |
| Tool 3: entity_resolution          | specs/06_sparse_data/signal_profile_spec.md          |
| Tool 4: external_validation        | specs/06_sparse_data/fraud_detection_spec.md         |
| Tool 5: entity_decision...         | specs/09_invocation/workspace_creation_spec.md       |
| Investigation plan                 | specs/06_sparse_data/investigation_plan_spec.md      |
| Planning Agent                     | specs/03_agents/agent_planning.md                    |
| Research Agent                     | specs/03_agents/agent_research.md                    |
| Analysis Agent                     | specs/03_agents/agent_analysis.md                    |
| VW Agent                           | specs/03_agents/agent_vw.md                          |
| Program Orchestrator               | specs/03_agents/agent_orchestrator.md                |
| Plan writer                        | specs/09_invocation/plan_writer_spec.md              |
| Intake pipeline                    | specs/06_sparse_data/intake_pipeline_spec.md         |
| P3 schemas                         | specs/07_rs_tools/rs_schemas_spec.md                 |
| P3 shared utilities                | specs/07_rs_tools/rs_shared_utilities_spec.md        |
| Tool 1 P3: structured_data_collector | specs/07_rs_tools/structured_data_collector_spec.md |
| Tool 2 P3: document_intelligence   | specs/07_rs_tools/document_intelligence_spec.md      |
| Tool 3 P3: spend_aggregator        | specs/07_rs_tools/spend_aggregator_spec.md           |
| Tool 4 P3: relationship_classifier | specs/07_rs_tools/relationship_classifier_spec.md    |
| Tool 5 P3: rs_profile_assembler    | specs/07_rs_tools/rs_profile_assembler_spec.md       |
| P3 pipeline orchestrator           | specs/07_rs_tools/rs_pipeline_spec.md                |
| P4 schemas                         | specs/10_analysis_intelligence/an_schema_spec.md     |
| P4 shared utilities                | specs/10_analysis_intelligence/an_shared_utilities_spec.md |
| P4 DB additions                    | specs/10_analysis_intelligence/an_db_additions_spec.md |
| Tool 1 P4: evidence_validator      | specs/10_analysis_intelligence/evidence_validator_spec.md |
| Tool 2 P4: scoring_engine          | specs/10_analysis_intelligence/scoring_engine_spec.md |
| Tool 3 P4: commercial_analyser     | specs/10_analysis_intelligence/commercial_analyser_spec.md |
| Tool 4 P4: trend_analyser          | specs/10_analysis_intelligence/trend_analyser_spec.md |
| Tool 5 P4: inquiry_engine          | specs/10_analysis_intelligence/inquiry_engine_spec.md |
| Tool 6 P4: finding_engine          | specs/10_analysis_intelligence/finding_engine_spec.md |
| Tool 7 P4: narrative_engine        | specs/10_analysis_intelligence/narrative_engine_spec.md |
| P4 pipeline orchestrator           | specs/10_analysis_intelligence/analysis_orchestrator_spec.md |
| P4 integration test                | specs/10_analysis_intelligence/an_integration_test_spec.md |

## Process 4 — Analysis & Intelligence

### P4 gate rules (checked by analysis_orchestrator before running)
  1. entity.md must exist with status = CONFIRMED
  2. relationship_spend_profile.md must exist (P3 must be complete)
  3. Skip if last_analysed_at < 30 days ago (unless force=True)
  4. vendor_profile.md missing → warn only, do not block

### P4 LLM usage per tool
  evidence_validator:  0 calls — pure quality checks, no LLM
  scoring_engine:      0 calls — arithmetic, no LLM
  commercial_analyser: 0–3 calls (contract type when ambiguous,
                         renewal scenarios, spend narrative >15% variance)
  trend_analyser:      0–1 calls (action learning insight when >= 3 actions)
  inquiry_engine:      6–12 calls (one per question, tiered Q&A)
  finding_engine:      0–1 calls (severity calibration when >= 3 HIGH findings)
  narrative_engine:    2 calls (batch 1: findings + vendor summary;
                         batch 2: commercial + Q&A summaries)
  All LLM failures: return degraded-but-non-null output. Never raise.

### P4 workspace outputs (written by analysis_orchestrator only)
  workspace/{programme_id}/{vendor_id}/analysis_result.md
  workspace/{programme_id}/{vendor_id}/history/score_history.json
  workspace/{programme_id}/{vendor_id}/history/qa_history.json
  workspace/{programme_id}/{vendor_id}/history/evidence_state.json
  workspace/{programme_id}/{vendor_id}/history/commercial_state.json
  programme_run/analysis_log.md   (one row per vendor, appended)

### P4 DB sync (analysis_orchestrator writes after each completed run)
  analysis_result.md → VendorIntelligence:
    CriScore        ← score_bundle.cri_score
    HealthBand      ← score_bundle.health_band
    VendorState     ← state_classifier.classify_vendor_state(...)
    LastAnalysedAt  ← now (UTC)

### P4 new core skills (src/cobalt/core/)
  pcs.py              — compute_pcs() — P4 PCS contribution (max 0.10)
  triage.py           — generate_triage_tasks(), build_triage_task()
  state_classifier.py — classify_vendor_state()
                        → HEALTHY / WATCH / AT_RISK / CRITICAL / UNKNOWN / ARCHIVED
  Note: confidence_scorer.py, gap_analyzer.py, staleness.py already exist from P3.
  Do not recreate them.

## Ten Non-Negotiable Rules
1. VW Agent is the ONLY writer to vendor workspace files after intake.
   During intake: Orchestrator/workspace_builder writes.
   Violation raises FileOwnershipViolation. HALT.

2. Every LLM call goes through llm_call() in src/cobalt/core/llm_call.py.
   NEVER call anthropic client directly anywhere.

3. Every file write goes through atomic_write() in src/cobalt/core/atomic_write.py.
   NEVER open().write() or direct file I/O.

4. Ledger append is synchronous. LedgerWriteError = HALT.

5. ONE LLM call per VW Agent tick for tactical decisions.
   Call Planning Agent for strategic decisions.

6. Planning Agent writes every plan. Rules engine lives INSIDE Planning Agent.
   Orchestrator never plans. VW Agent calls Planning Agent for strategy.

7. Research Agent = raw data only.
   Analysis Agent = extracts structure. Document extraction in Analysis Agent.

8. DB updated via sync_to_db() only — inside atomic_write() automatically.

9. entity.md input_name IMMUTABLE. evidence/ev-{id}.md IMMUTABLE.

10. V2/V3 features NOT built in V1. Add to TODO.md and stop.

## Build Phase Guard

V1 BUILD NOW:
  src/cobalt/core/
  src/cobalt/db/
  src/cobalt/brain/
  src/cobalt/models/schemas/
  src/cobalt/intake/          (private helpers: _cleaner, _normalizer,
                               _signal_collector, _executor, steps/)
  src/cobalt/tools/           (P1 five tools + P3 five tools + P4 seven tools)
  src/cobalt/agents/
  src/cobalt/orchestrator/
  src/cobalt/workspace/
  tests/
  specs/10_analysis_intelligence/   (P4 spec files — read before building P4)

V2 DO NOT BUILD:
  Real sanctions API, real registry connectors, Azure Functions,
  Debate mechanism, Full simulation, Pattern distillation

V3 DO NOT BUILD:
  Slow loop, Cross-domain learning, Simulation calibration

## Definition of Done Per Session
  1. pytest tests/ -x — all pass
  2. Prior tests still pass
  3. No V2/V3 features built
  4. No bare except: anywhere
  5. No direct file I/O outside atomic_write()
  6. No direct LLM calls outside llm_call()
  7. Tool files in src/cobalt/tools/ — never in src/cobalt/intake/

## Session Start Protocol
  1. Read CLAUDE.md fully
  2. Read the spec for today's component
  3. Run pytest tests/ -x
  4. Build one component only
  5. Write tests immediately
  6. Run pytest tests/ -x before ending
