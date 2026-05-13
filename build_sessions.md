# Cobalt — Build Sessions

## Status
Session 1: DONE (50 tests)  — src/cobalt/core/
Session 2: DONE (74 tests)  — src/cobalt/db/
Session 3: DONE (101 tests) — src/cobalt/brain/ + src/cobalt/models/schemas/
Session 4: NEXT             — Tool 2: candidate_screening

---

## SESSION 4 — Tool 2: candidate_screening

Read CLAUDE.md.
Read specs/06_sparse_data/normalization_spec.md.

Build these three files:

src/cobalt/intake/_cleaner.py          (private utility)
src/cobalt/intake/_normalizer.py       (private utility)
src/cobalt/tools/candidate_screening.py  (Tool 2 — public)

_cleaner.py:
  def structural_clean(raw: str) -> str
  Rules in order:
    1. Strip whitespace
    2. Strip surrounding quotes
    3. Document title extraction (trigger keywords + last separator)
    4. Strip leading reference numbers (digit-containing tokens)
    5. Strip trailing , . ; :
    6. Strip whitespace
    7. Return original if result empty

_normalizer.py:
  def detect_country_hint(cleaned: str) -> str | None
  def normalize(cleaned, country_code=None) -> tuple[str, str]
  Two-tier suffix rule. All country tables. Steps 1-7.

candidate_screening.py (Tool 2):
  ScreenedCandidate dataclass
  def screen(raw, ...) -> ScreenedCandidate
  def screen_all(raw_candidates) -> list[ScreenedCandidate]
  def entity_type_detect(cleaned) -> EntityType
  Imports from _cleaner and _normalizer.

Tests:
  tests/unit/test_cleaner.py
  tests/unit/test_normalizer.py
  tests/unit/test_candidate_screening.py

---

## SESSION 5 — Tool 3: entity_resolution

Read CLAUDE.md.
Read specs/06_sparse_data/signal_profile_spec.md.

Build:
  src/cobalt/intake/_signal_collector.py  (private utility)
  src/cobalt/tools/entity_resolution.py   (Tool 3 — public)

_signal_collector.py:
  def detect_script(text) -> ScriptType
  def brain_lookup(key, brain_data) -> BrainHit
  def dedup_check(key, all_keys) -> DedupResult
    Levenshtein: >= 0.90 AUTO_MERGE, 0.80-0.89 CANDIDATE, < 0.80 UNIQUE
  def collect_signals(raw, ctx) -> SignalProfile
    Runs all sub-functions in order.
    Includes linked_doc_ids, spend_hint, category_hint from BatchContext.

entity_resolution.py (Tool 3):
  ResolutionResult dataclass
  def resolve(candidate: ScreenedCandidate,
              ctx: BatchContext) -> ResolutionResult
  def resolve_all(candidates, ctx) -> list[ResolutionResult]
  Imports from _signal_collector and brain/loader.
  Returns: MATCHED / UNMATCHED_VIABLE / UNMATCHED_AMBIGUOUS per candidate.

Tests cover all four sparse data dimensions:
  Brain match, rebrand, alias, dedup, entity type,
  ERP signal, document links, fraud flags.

---

## SESSION 6 — Planning Agent

Read CLAUDE.md.
Read specs/03_agents/agent_planning.md FULLY.

Build:
  src/cobalt/agents/planning_agent.py

  PlanningAgent class:
    compute_investigation_plan(profile) -> InvestigationPlan
      14 rules R01-R14. Mode 1 (rules). Mode 2 (LLM) for complex cases.
    compute_programme_plan(source_summary) -> ProgrammePlan
    compute_campaign_plan(input: CampaignPlanInput) -> CampaignPlan
      Gap-to-campaign mapping. Connector-first priority.
    compute_mid_campaign_update(state, event) -> CampaignPlan
    compute_negotiation_plan(input) -> NegotiationPlan (V1 shell)
    _rules_engine(profile) -> InvestigationPlan | None
    _llm_plan(prompt, system) -> dict

  Rules engine lives INSIDE Planning Agent. Not standalone.
  Plans A-F from spreadsheet map to rules R01-R14.

Tests: every rule fires correctly, campaign plan maps gaps correctly.

---

## SESSION 7 — Research Agent

Read CLAUDE.md. Read specs/03_agents/agent_research.md.

Build:
  src/cobalt/agents/research_agent.py

  ResearchAgent:
    web_research(name, tier, country_hint) -> str  (raw text)
    erp_batch_scan(all_keys) -> dict               (raw ErpSignal dict)
    fetch_pdf_text(file_path) -> str               (raw text only)
    fetch_google_drive_docs(folder_path) -> list
    ap_batch_scan(all_keys) -> dict

  Returns RAW data only. Never structures or interprets.
  All connectors return empty if not configured.

---

## SESSION 8 — Analysis Agent

Read CLAUDE.md.
Read specs/03_agents/agent_analysis.md.
Read specs/06_sparse_data/document_extractor_spec.md.

Build:
  src/cobalt/agents/analysis_agent.py

  AnalysisAgent:
    extract_vendor_name_from_doc(raw_text, filename) -> dict
    extract_contract_terms(raw_text, vendor_name) -> dict
    consolidate_documents(terms_list) -> dict
    structure_entity(vendor_name, raw_text) -> dict
    score_confidence(evidence_sources, method) -> float
    detect_contradictions(evidence_list) -> list

  Document extraction lives HERE.
  Research Agent fetches raw text. Analysis Agent extracts structure.

---

## SESSION 9 — Tool 4: external_validation

Read CLAUDE.md. Read specs/06_sparse_data/fraud_detection_spec.md.

Build:
  src/cobalt/intake/_executor.py         (private utility)
  src/cobalt/intake/steps/               (private step functions)
    __init__.py — StepResult dataclass
    _web_research_step.py
    _fraud_check_step.py
    _document_extraction_step.py
    _merge_canonical_step.py
    _confirm_rebrand_step.py
    _route_to_human_step.py
    _sanctions_check_step.py  (V1 stub)
    _registry_lookup_step.py  (V1 stub)
    _hr_overlap_step.py       (V1 stub)

  src/cobalt/tools/external_validation.py  (Tool 4 — public)
    ValidationResult dataclass
    def validate(candidate, profile, plan, ctx) -> ValidationResult
    def validate_all(candidates, profiles, plans, ctx)
    STEP_REGISTRY inside this file.
    execute_plan() inside this file.

---

## SESSION 10 — Tool 1: source_intake

Read CLAUDE.md. Read specs/06_sparse_data/source_processor_spec.md.

Build:
  src/cobalt/tools/source_intake.py  (Tool 1 — public)

  RawCandidate, DocRecord, SourceIntakeResult dataclasses.
  def run(vendor_list_path, google_drive_path,
          research_agent, analysis_agent) -> SourceIntakeResult

  Scenario 1: Excel/CSV only.
  Scenario 2: Excel + Google Drive PDFs.
  Document-to-candidate linking by comparison_key.
  Vendors discovered via document (not in list) also captured.

---

## SESSION 11 — Tool 5: entity_decision_and_shell_creation

Read CLAUDE.md.
Read specs/09_invocation/workspace_creation_spec.md.
Read specs/09_invocation/plan_writer_spec.md.

Build:
  src/cobalt/workspace/plan_writer.py
    write_programme_plan(), write_investigation_plan()
    update_plan_step(), finalise_plan()

  src/cobalt/tools/entity_decision_and_shell_creation.py (Tool 5 — public)
    EntityDecision dataclass
    def decide_and_create(result: IntakeResult,
                          programme_id: str,
                          extracted_terms: dict | None,
                          erp_data: ErpSignal | None) -> EntityDecision

    Internally calls workspace/builder.py.
    Writes entity.md, gate_results.md, ledger.md.
    Writes spend.md, contract.md, coverage.md.
    Writes evidence files (IMMUTABLE).
    Sets data_class. Seeds initial PCS. Inserts DB row.

---

## SESSION 12 — Intake Orchestrator

Read CLAUDE.md. Read specs/03_agents/agent_orchestrator.md.

Build:
  src/cobalt/orchestrator/batch_context_builder.py
  src/cobalt/orchestrator/intake_orchestrator.py

  intake_orchestrator.run_intake():
    1. Tool 1 source_intake → unified candidates
    2. Tool 2 candidate_screening → screened candidates
    3. Tool 3 entity_resolution → resolution results
    4. Route: AUTO_CONFIRM / AUTO_DISCARD / INVESTIGATE
    5. Planning Agent → programme_plan.md + IP-{key}.md per candidate
    6. Tool 4 external_validation for investigation candidates
    7. Analysis Agent extract_contract_terms for confirmed with docs
    8. Tool 5 entity_decision_and_shell_creation for confirmed
    9. Write programme files

---

## SESSION 13 — Integration Test

Write tests/integration/test_full_intake.py

Scenario 1 (Excel only):
  20 vendors. Verify routing. contract.md NOT_FOUND. PCS <= 15.
  Planning Agent wrote IP files for complex candidates.

Scenario 2 (Excel + mock Google Drive):
  Same + 8 mock PDFs. contract.md OBSERVED for doc-backed vendors.
  PCS > 30 for doc-backed. Evidence files IMMUTABLE.
  All five tool names used in the flow.

pytest tests/ -x — all must pass.
