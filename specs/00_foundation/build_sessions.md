# Cobalt — Build Sessions

## Status

| Session | Component | Tests | Status |
|---|---|---|---|
| 1 | `src/cobalt/core/` | 50 | DONE |
| 2 | `src/cobalt/db/` | 74 | DONE |
| 3 | `src/cobalt/brain/` + schemas | 101 | DONE |
| 4 | Tool 2: `candidate_screening` | 164 | DONE |
| 5 | Tool 3: `entity_resolution` | 215 | DONE |
| 6 | Planning Agent (Process 1) | 258 | DONE |
| 7 | Research Agent | 240 | DONE |
| 8 | Analysis Agent | 274 | DONE |
| 9 | Tool 4: `external_validation` | 309 | DONE |
| 10 | Tool 1: `source_intake` | 360 | DONE |
| 11 | Tool 5: `entity_decision_and_shell_creation` | 431 | DONE |
| 12 | Intake Orchestrator | 456 | DONE |
| 13 | Integration test (Process 1) | 473 | DONE |

**Process 1 — Complete. Working.**

---

## V2 Sessions (Runtime + Process 2)

### Block A — Runtime Foundation (4 sessions)

| Session | Component | Spec |
|---|---|---|
| 14 | `WorkflowDefinition` + `ExecutionState` + `checkpoint_store` | `04_runtime/workflow_definition_spec.md` + `execution_state_spec.md` |
| 15 | `RuntimeEngine` | `04_runtime/runtime_engine_spec.md` |
| 16 | Planning Agent additions (`create_workflow`, `evaluate_step`, `replan`) | `03_agents/agent_planning.md` (updated) |
| 17 | `PlanRenderer` + intake checkpoint integration | `04_runtime/plan_renderer_spec.md` |

### Block B — Process 2 Data Enrichment (8 sessions)

| Session | Component | Spec |
|---|---|---|
| 18 | `enrichment_schema.py` + Brain updates (acquisition_map, brand_map) | `05_data_enrichment/enrichment_schemas_spec.md` |
| 19 | Tool 1: `enrichment_readiness_check` | `05_data_enrichment/enrichment_readiness_check_spec.md` |
| 20 | Tool 2: `external_source_collector` | `05_data_enrichment/source_collector_spec.md` |
| 21 | Tool 3: `attribute_extractor` | `05_data_enrichment/attribute_extractor_spec.md` |
| 22 | Tool 4: `relationship_and_lifecycle_mapper` | `05_data_enrichment/relationship_lifecycle_mapper_spec.md` |
| 23 | Tool 5: `enriched_profile_creator` | `05_data_enrichment/enriched_profile_creator_spec.md` |
| 24 | `enrichment_orchestrator` | `05_data_enrichment/enrichment_pipeline_spec.md` |
| 25 | Integration test (Process 2) | `05_data_enrichment/enrichment_pipeline_spec.md` |

---

## Session Protocol

Every session follows this pattern:

1. **Read CLAUDE.md fully**
2. **Read the spec(s) referenced in the session row**
3. **Run `pytest tests/ -x`** — confirm all prior tests pass
4. **Build the named component only** — do not exceed scope
5. **Write tests immediately**
6. **Run `pytest tests/ -x`** — all must pass before ending
7. **Report:** total test count, files created, any deviations from spec
