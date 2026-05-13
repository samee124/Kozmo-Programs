# Intake Pipeline Specification

## Purpose
Convert raw input sources into confirmed vendor workspaces.
Runs before any VW Agent. Before any workspace exists.
Follows APE at every level.

## The Five Tools

Tool 1: source_processor
  Purpose: Ingest all sources. Extract candidates. Link documents.
  Called by: Orchestrator
  
Tool 2: candidate_screening
  Purpose: Clean, normalize, cross-source dedup.
  Called by: Orchestrator

Tool 3: entity_resolver
  Purpose: Brain lookup, dedup, entity type, signal collection.
  Called by: Orchestrator

Tool 4: external_validator
  Purpose: Web research for unresolved candidates only.
  Called by: Orchestrator (triggers Research Agent + Analysis Agent)

Tool 5: workspace_builder
  Purpose: Write all initial workspace files for confirmed vendors.
  Called by: Orchestrator

## The Routing Decision (after entity_resolver)

AUTO CONFIRM — no investigation plan written:
  brain_hit.matched=True AND confidence >= 0.90
  → Call Planning Agent only if documents linked
  → workspace_builder immediately

AUTO DISCARD — no investigation plan written:
  entity_type=PERSON or INTERNAL
  → log in deduplication_report

AUTO MERGE — no investigation plan written:
  dedup_result.status=AUTO_MERGE (>= 0.90)
  → merge_canonical, workspace_builder for canonical

INVESTIGATION NEEDED — planning agent writes plan:
  brain_hit=False AND entity_type=COMPANY or AMBIGUOUS
  → Call Planning Agent with SignalProfile
  → Planning Agent writes IP-{key}.md
  → Execute plan (web research, fraud check, etc.)
  → workspace_builder if CONFIRMED

DOCUMENT BACKED CONFIRMED:
  brain_hit=True AND documents linked
  → Call Analysis Agent: extract_contract_terms per doc
  → workspace_builder seeds contract.md + evidence files

## The Plan File for Each Investigation
Written by Planning Agent to:
  workspace/{programme_id}/programme_run/intake_plans/IP-{key}.md

Contains:
  - SignalProfile summary (what was known)
  - Selected path and reason
  - Steps to execute
  - Step results (updated as execution proceeds)
  - Final outcome (CONFIRMED / TRIAGE / BLOCKED / DISCARDED)

Written BEFORE execution starts.
Finalised AFTER execution completes.

## IntakeResult Schema
@dataclass
class IntakeResult:
    raw_input:           str
    canonical_name:      str | None
    vendor_id:           str | None
    status:              IntakeStatus
    confidence:          float
    resolution_method:   str
    country_code:        str | None
    erp_spend:           Decimal | None
    data_class:          str
    entity_type:         str
    triage_question:     str | None
    fraud_signals:       list[str]
    fraud_risk:          str
    block_reason:        str | None
    aliases:             list[str]
    linked_doc_ids:      list[str]
    extracted_terms:     dict | None    ← from document extraction
    investigation_plan:  InvestigationPlan

## Data Class at Intake
CLASS_D: name only (no ERP, no document)
CLASS_C: ERP spend confirmed
CLASS_B: ERP spend + contract document confirmed
CLASS_A: full data (rare at intake)

## Initial PCS at Workspace Creation
Fields OBSERVED from ERP: +12 PCS
Fields OBSERVED from document:
  renewal_date OBSERVED:     +15 PCS
  auto_renewal OBSERVED:     +10 PCS
  contract_value OBSERVED:   +8 PCS
  price_escalation OBSERVED: +5 PCS
  baa_present OBSERVED:      +5 PCS
Max at intake (all above): ~55 PCS
