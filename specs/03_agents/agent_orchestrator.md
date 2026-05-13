# Program Orchestrator Specification

## Role
Drives and sequences everything.
Calls agents in the right order.
Never plans. Never researches. Never analyses.

## Two Modes

### Mode 1 — Intake Mode (Phase 0)
Runs when programme starts.
Owns entire identification process (Process 1).

APE in Intake Mode:
  ANALYZE:
    Call Research Agent: fetch all PDFs, ERP batch scan, AP scan
    Call Analysis Agent: extract vendor names from each PDF
    Build BatchContext
  PLAN:
    Call Planning Agent → programme_plan.md written
  EXECUTE:
    source_processor → unified candidates
    candidate_screening per candidate
    entity_resolution per candidate
    For each candidate needing investigation:
      Call Planning Agent → IP-{key}.md written
      Execute plan (Research Agent + Analysis Agent as needed)
    workspace_builder for confirmed vendors
    Write programme files

### Mode 2 — Ongoing Mode
Runs every hour via timer.
Queries DB for due vendors.
Sends queue messages to spawn VW Agents.

APE in Ongoing Mode:
  ANALYZE: Query DB for due vendors
  PLAN:    Assign directives per vendor
  EXECUTE: Send queue messages

## Intake Sequence — Exact

Step 1: Research Agent fetches all sources
  fetch_google_drive_docs() → list of {filename, raw_text}
  erp_batch_scan(all_keys) → ErpSignal per vendor
  ap_batch_scan(all_keys) → ApSignal per vendor

Step 2: Analysis Agent extracts from documents
  For each PDF: extract_vendor_name_from_doc(raw_text) → vendor name

Step 3: source_processor
  Unified candidate list. Document links. RawCandidate per vendor.

Step 4: Planning Agent → programme_plan.md
  Pass source summary. Receive ProgrammePlan. Write file.

Step 5: Per candidate
  collect_signals() → SignalProfile
  Route:
    AUTO CONFIRM: workspace_builder immediately
    AUTO DISCARD: log
    INVESTIGATION:
      Call Planning Agent with SignalProfile
      Planning Agent returns InvestigationPlan
      Write IP-{key}.md via plan_writer
      Execute plan steps
      If confirmed: workspace_builder

Step 6: Document extraction for confirmed vendors with docs
  Analysis Agent: extract_contract_terms(raw_text) per linked PDF
  Analysis Agent: consolidate_documents() if multiple docs
  workspace_builder seeds contract.md from extracted terms

Step 7: Programme files
  deduplication_report.md, triage_queue.md,
  vendor_register.md, run_log.md

## What Orchestrator NEVER Does
NEVER reads individual vendor workspace files in ongoing mode
NEVER makes planning decisions directly
NEVER calls search providers or web APIs directly
NEVER writes to DB directly
NEVER calls LLM directly

## DB Query for Ongoing Mode
SELECT vendor_id FROM vendor_intelligence
WHERE programme_id = :pid
  AND next_action_due <= NOW()
  AND status NOT IN ('WAITING_HUMAN_GATE','CHECKIN_SENT',
                     'SURVEY_PENDING','COMPLETE','PAUSED')
ORDER BY tier DESC NULLS LAST, pcs_score ASC
LIMIT 20
