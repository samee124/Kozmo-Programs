# Workspace Builder Specification

## Purpose
Creates all initial workspace files for confirmed vendors.
Called by Orchestrator after intake confirms a vendor.
The more documents available, the richer the initial workspace.

## Location
src/Cobalt/workspace/builder.py

## Function Signature
def build_workspace(
    result: IntakeResult,
    programme_id: str,
    extracted_terms: dict | None,     ← from Analysis Agent
    erp_data: ErpSignal | None,
) -> WorkspaceBuildResult:

## Only Called When
result.status == CONFIRMED
Raises ValueError for any other status.

## Files Written (always)
workspace/{programme_id}/{vendor_id}/
  identity/entity.md
  intake/gate_results.md
  execution/ledger.md    ← INTAKE_COMPLETED row
  cost_file/spend.md     ← OBSERVED if ERP, INFERRED if not
  cost_file/contract.md  ← OBSERVED if doc extracted, NOT_FOUND if not
  cost_file/coverage.md  ← initial PCS seeded

## Files Written (only if documents linked)
  evidence/ev-contract-{doc_id}.md   ← one per linked document, IMMUTABLE

## entity.md at Creation
  vendor_id:            generated hash
  input_name:           result.raw_input    ← IMMUTABLE forever
  vendor_name:          result.canonical_name
  legal_entity:         null               ← VW Agent fills
  category:             result.erp_category or null
  hq_country:           result.country_code
  identity_confidence:  result.confidence
  resolution_method:    result.resolution_method
  data_class:           result.data_class
  version:              1

## contract.md at Creation (Scenario 2 — doc available)
  status: OBSERVED
  primary_contract:
    renewal_date: extracted_terms.renewal_date
    auto_renewal: extracted_terms.auto_renewal
    contract_value: extracted_terms.contract_value
    ...
  effective_terms: consolidated effective terms
  overall_confidence: extracted_terms.confidence

## contract.md at Creation (Scenario 1 — no doc)
  status: NOT_FOUND
  primary_contract: null
  effective_terms: all fields null, all INFERRED
  overall_confidence: 0.10

## spend.md at Creation
  annual_spend: erp_data.spend if OBSERVED else null
  status: OBSERVED if ERP hit else INFERRED
  confidence: 0.92 if OBSERVED else 0.25

## Initial PCS Seeding
  Compute based on OBSERVED fields:
    annual_spend OBSERVED:     +12
    renewal_date OBSERVED:     +15
    auto_renewal OBSERVED:     +10
    contract_value OBSERVED:   +8
    price_escalation OBSERVED: +5
    baa_present OBSERVED:      +5
  Write to cost_file/coverage.md

## data_class at Creation
  CLASS_D: no ERP, no document
  CLASS_C: ERP spend confirmed, no document
  CLASS_B: ERP spend + contract document confirmed
  CLASS_A: full data (very rare at intake)

## DB Row Inserted (via sync_to_db)
  status:           NEEDS_ACTION
  next_action_due:  NOW()
  pcs_score:        computed above
  data_class:       computed above
  identity_confidence: result.confidence
