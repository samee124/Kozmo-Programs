# rs_pipeline (Process 3 Orchestrator)

## Overview

**File:** `src/cobalt/orchestrator/rs_orchestrator.py`
**Role:** Wires the 5 Process 3 tools end-to-end via the RuntimeEngine. Handles gate checks, data arrival mode routing, crash recovery, and the SKIP / BLOCKED / FAILED routing decisions.

---

## Purpose

`rs_orchestrator.run_rs()` is the entry point for Process 3 — Relationship & Spend Data Gathering.

Uses the same V2 runtime layer (`RuntimeEngine`, `WorkflowDefinition`, `ExecutionState`) as the enrichment orchestrator, providing:
- Crash recovery (restart resumes from last completed step)
- Full audit trail via `state.json`
- Replayability
- Step-level observability

The orchestrator is the only component that knows the full sequence. Individual tools know nothing about each other — they accept inputs and return outputs.

---

## Public API

```python
def run_rs(
    vendor_id: str,
    programme_id: str,
    arrival_modes: list[str] | None = None,
    uploaded_files: list[dict] | None = None,
    checkin_data: dict | None = None,
    connector_config: dict | None = None,
) -> RSRunResult:
    # analysis_agent is NOT a parameter — document_intelligence calls llm_call() directly.
    """
    Drive the full Process 3 pipeline for one vendor.
    Never raises — always returns an RSRunResult.
    """

def run_rs_all_confirmed(
    programme_id: str,
    **kwargs,
) -> list[RSRunResult]:
    """
    Run Process 3 for every vendor with CONFIRMED status in the programme.
    kwargs forwarded to run_rs() for each vendor.
    """
```

---

## Gate Checks (run before workflow creation)

Four checks are evaluated in order before creating any workflow. Any failing gate returns `RSRunResult(status="SKIPPED")` or `RSRunResult(status="BLOCKED")` immediately.

| Gate | Condition | Result |
|---|---|---|
| Entity confirmed | `entity.md` exists AND `status = CONFIRMED` | BLOCKED if not met |
| P2 profile exists | `vendor_profile.md` exists | Warn only — do not block; log `P2_PROFILE_MISSING` |
| Data available | At least one arrival mode has data (files non-empty, checkin_data non-null, connector dir exists) | SKIPPED if nothing to collect |
| Profile freshness | `relationship_spend_profile.md` exists AND `last_updated` < 30 days ago | SKIPPED if fresh |

**P2 profile missing warning:** Process 3 can run without P2 enrichment — it just means `known_facts` will be empty dict, reducing classification signal quality. This is noted in the profile but is not a blocker.

---

## Flow

```
1. Run gate checks
   - BLOCKED → return RSRunResult(status="BLOCKED", reason="entity_not_confirmed")
   - SKIPPED (no data) → return RSRunResult(status="SKIPPED", reason="no_data_available")
   - SKIPPED (fresh) → return RSRunResult(status="SKIPPED", reason="profile_fresh")
   - Otherwise: proceed

2. Load entity.md and vendor_profile.md (if exists) to extract:
   - entity_profile dict
   - known_facts dict
   - current_pcs value

3. Extract document_paths from uploaded_files before creating workflow:
   document_paths = [f["path"] for f in (uploaded_files or []) if f.get("path")]
   This is the only place that knows both uploaded_files and the workflow context.
   document_paths is passed in workflow.context so the s2_documents step can access it.

4. Planning Agent creates WorkflowDefinition
   - workflow_type = "RS_DATA_GATHERING"
   - vendor_id = vendor_id
   - workflow_id = f"wf-rs-{vendor_id}-{int(time.time())}"
   - context = {arrival_modes, uploaded_files, checkin_data, connector_config,
                document_paths, entity_profile, known_facts, current_pcs}
   - steps = [see workflow steps below]
   - Save workflow.json

5. RuntimeEngine.execute_workflow(workflow_id, programme_id)
   - Reads workflow.json + state.json
   - Executes steps in dependency order
   - Persists state.json after each step
   - Calls Planning Agent for replanning evaluation after each step

6. Final result:
   - RSRunResult assembled from state.json outcome — include programme_id in RSRunResult
   - Programme-level files updated

7. Return RSRunResult
```

---

## Workflow Definition (5 steps)

| Step ID | Step Type | Tool Called | Depends On |
|---------|-----------|-------------|------------|
| `s1_collect` | `COLLECT_RS_DATA` | `structured_data_collector.collect_structured_data()` | — |
| `s2_documents` | `PROCESS_DOCUMENTS` | `document_intelligence.process_documents()` | s1_collect |
| `s3_aggregate` | `AGGREGATE_SPEND` | `spend_aggregator.aggregate_spend()` | s1_collect, s2_documents |
| `s4_classify` | `CLASSIFY_RELATIONSHIP` | `relationship_classifier.classify_relationship()` | s3_aggregate |
| `s5_assemble` | `ASSEMBLE_RS_PROFILE` | `rs_profile_assembler.assemble_rs_profile()` | s1_collect, s2_documents, s3_aggregate, s4_classify |

**Dependency graph:**
```
s1_collect
    ├──→ s2_documents
    │        └──→ s3_aggregate ──→ s4_classify ──→ s5_assemble
    └──→ s3_aggregate (also depends on s2_documents)
```

s3_aggregate depends on both s1 and s2 because it receives both `raw_records` (from Tool 1) and `contract_terms` (from Tool 2) for the deviation check.

---

## Step Adapter Registry

```python
RS_STEP_REGISTRY = {
    "COLLECT_RS_DATA":       _collect_rs_data_step,
    "PROCESS_DOCUMENTS":     _process_documents_step,
    "AGGREGATE_SPEND":       _aggregate_spend_step,
    "CLASSIFY_RELATIONSHIP": _classify_relationship_step,
    "ASSEMBLE_RS_PROFILE":   _assemble_rs_profile_step,
}
```

Each adapter function:
- Reads context from `workflow.context`
- Reads prior step results from `state.completed_steps`
- Returns a dict that becomes the `result` field in `StepRunRecord`
- Raises `StepFatal` for any unrecoverable failure

**`StepRetryable` note:** Do not use `StepRetryable` unless you have first confirmed that `RuntimeEngine.execute_workflow()` implements retry logic for it. As of the current implementation, the enrichment orchestrator uses only `StepFatal`. Follow the same pattern — use `StepFatal` for all failures. If retry support is added to RuntimeEngine in future, the adapters can be updated then.

**Step adapter patterns:**

```python
def _collect_rs_data_step(workflow, state, step) -> dict:
    ctx = workflow.context
    bundle = structured_data_collector.collect_structured_data(
        vendor_id=workflow.vendor_id,
        programme_id=workflow.programme_id,
        arrival_modes=ctx.get("arrival_modes"),
        connector_config=ctx.get("connector_config"),
        uploaded_files=ctx.get("uploaded_files"),
        checkin_data=ctx.get("checkin_data"),
    )
    return {"structured_bundle": bundle.to_dict()}

def _process_documents_step(workflow, state, step) -> dict:
    # document_paths was extracted from uploaded_files before workflow creation
    # and stored in workflow.context["document_paths"]
    document_paths = workflow.context.get("document_paths", [])
    result = document_intelligence.process_documents(
        vendor_id=workflow.vendor_id,
        programme_id=workflow.programme_id,
        document_paths=document_paths,
        # No analysis_agent parameter — llm_call() imported directly inside the tool
    )
    return {"doc_intelligence": result.to_dict()}
```

---

## RSRunResult

`programme_id` is included to make the result self-describing. It is required for programme-level log writes and is always set by the orchestrator before returning.

```python
@dataclass
class RSRunResult:
    vendor_id:          str
    programme_id:       str          # Always set — never None
    status:             str          # COMPLETED / SKIPPED / BLOCKED / FAILED
    pcs_before:         float | None
    pcs_after:          float | None
    tools_run:          list[str]    # step IDs that executed
    flags_raised:       list[str]    # flags from assembler
    profile_status:     str | None   # COMPLETE / PARTIAL / MINIMAL / FAILED
    skip_reason:        str | None   # if status=SKIPPED
    error:              str | None   # if status=FAILED
```

Defined in `rs_schema.py`. The orchestrator populates all fields before returning — never returns a partial result object.

---

## Crash Recovery

Same pattern as `enrichment_orchestrator`.

If `run_rs()` is called and a `workflow.json` already exists for this vendor:
1. Load existing `workflow.json` + `state.json`
2. If `state.status = COMPLETED` → return existing outcome (no re-run)
3. If `state.status = IN_PROGRESS` → resume from first non-DONE step
4. If `state.status = FAILED` with retries remaining → resume

**Step resume example:** If crash occurred during s3_aggregate:
- s1_collect (DONE) → skip, read result from state
- s2_documents (DONE) → skip, read result from state
- s3_aggregate (PENDING) → re-run with s1 and s2 results from state
- s4_classify (PENDING) → run after s3
- s5_assemble (PENDING) → run after s4

---

## Programme-Level Outputs

After `run_rs()` completes for a vendor, update programme-level files:

- Append to `programme_run/rs_log.md` (one row per vendor P3 run: vendor_id, status, pcs_before, pcs_after, flags)
- If `CLASSIFICATION_INCOMPLETE` flag → append to `programme_run/triage_queue.md`
- If `PROFILE_ASSEMBLY_FAILED` flag → append to `programme_run/triage_queue.md`

---

## `run_rs_all_confirmed` Behaviour

```python
def run_rs_all_confirmed(programme_id: str, **kwargs) -> list[RSRunResult]:
    confirmed_vendors = db.query_confirmed_vendors(programme_id)
    results = []
    for vendor_id in confirmed_vendors:
        result = run_rs(vendor_id, programme_id, **kwargs)
        results.append(result)
    return results
```

Sequential by default in V1. Does not parallelise. Failures in one vendor do not affect others (each `run_rs()` never raises).

---

## Tests required (orchestrator unit)

- Gate: `entity.md` absent → `status = BLOCKED`, no workflow created
- Gate: `entity.md` exists but `status = TRIAGE_REQUIRED` → `status = BLOCKED`
- Gate: `relationship_spend_profile.md` updated 10 days ago → `status = SKIPPED`, `skip_reason = profile_fresh`
- Gate: `relationship_spend_profile.md` updated 31 days ago → proceeds (not fresh)
- Gate: no uploaded files + no checkin_data + no connector dir → `status = SKIPPED`, `skip_reason = no_data_available`
- Gate: P2 profile missing → warning logged, execution continues
- Happy path: all 5 steps complete → `status = COMPLETED`, `tools_run` has 5 entries
- `pcs_after > pcs_before` on COMPLETED run with real data
- Step s3 failure (StepFatal) → `status = FAILED`, `error` populated, s4 and s5 not run
- Step s5 failure (StepFatal) → `status = FAILED`, `error` populated
- Crash recovery: state.json has s1+s2 DONE → re-run starts from s3
- `run_rs_all_confirmed`: 3 confirmed vendors → returns list of 3 results, one per vendor
- `run_rs_all_confirmed`: one vendor fails → remaining vendors still processed

## Tests required (integration)

`tests/integration/test_full_rs_pipeline.py`:

- FILE_UPLOAD CSV with spend data → profile written, `pcs_after > pcs_before`
- CHECK_IN data only → `relationship_type` determined, profile written with PARTIAL completeness
- No data from any mode → `status = SKIPPED`, no profile written
- Malformed CSV → `FILE_PARSE_ERROR` warning, partial result, no crash, profile still written
- PDF document with contract → `ContractTerms` extracted, `contract_count = 1` in profile
- LLM extraction failure on all documents → profile written with zero contracts, `UNCOVERED_SPEND` flag if spend present
- Score in ambiguous band → LLM called for classifier
- Ambiguous band + LLM failure → rule-based fallback used, profile still written
- All tools stubbed at import boundary — same pattern as `test_full_enrichment.py`
- `CONTRACT_RENEWAL_URGENT` flag → present in profile YAML front-matter flags list
