# enrichment_pipeline

## Overview

**File:** `src/cobalt/orchestrator/enrichment_orchestrator.py`
**Role:** Wires the 5 Process 2 tools end-to-end via the RuntimeEngine. Handles the SKIP / BLOCKED / FAILED routing decisions.

---

## Purpose

`enrichment_orchestrator.run_enrichment()` is the entry point for Process 2.

Uses the V2 runtime layer (`RuntimeEngine`, `WorkflowDefinition`, `ExecutionState`) so the enrichment process gets:
- Crash recovery
- Adaptive replanning
- Full audit trail
- Replayability
- Observability

---

## Public API

```python
def run_enrichment(
    vendor_id: str,
    programme_id: str,
    manual_override: bool = False,
) -> EnrichmentRunResult:
    """
    Drive the full Process 2 enrichment pipeline for one vendor.
    Never raises — always returns a result.
    """
```

---

## Flow

```
1. enrichment_readiness_check (Tool 1) — runs FIRST, no workflow needed
   - If skip=true → return EnrichmentRunResult(status=SKIPPED, reason=...)
   - If proceed=false → return EnrichmentRunResult(status=BLOCKED, reason=...)
   - Otherwise: have readiness_result, proceed to step 2

2. Planning Agent creates WorkflowDefinition
   - workflow_type = "ENRICHMENT"
   - vendor_id = vendor_id
   - workflow_id = f"wf-enrich-{vendor_id}-{int(time.time())}"
   - context = readiness_result (depth_tier, source_list, known_facts)
   - steps = [
       {step_id: "s1", step_type: "COLLECT_SOURCES",   depends_on: []},
       {step_id: "s2", step_type: "EXTRACT_ATTRIBUTES", depends_on: ["s1"]},
       {step_id: "s3", step_type: "MAP_RELATIONSHIPS",  depends_on: ["s1"]},
       {step_id: "s4", step_type: "CREATE_PROFILE",     depends_on: ["s2", "s3"]},
     ]
   - Save workflow.json

3. RuntimeEngine.execute_workflow(workflow_id, programme_id)
   - Reads workflow.json + state.json
   - Executes steps in dependency order
   - Each step calls its tool:
     - COLLECT_SOURCES   → external_source_collector.collect(vendor_id, readiness_result)
     - EXTRACT_ATTRIBUTES → attribute_extractor.extract(source_bundle, known_facts)
     - MAP_RELATIONSHIPS  → relationship_and_lifecycle_mapper.map(source_bundle)
     - CREATE_PROFILE     → enriched_profile_creator.create(extracted, map, signals,
                                                              brain_suggestions, readiness)
   - Persists state.json after each step
   - Calls Planning Agent for replanning evaluation

4. Final result:
   - state.outcome contains the vendor profile + flags + triage tasks
   - Brain update suggestions forwarded to Program Orchestration Agent

5. Return EnrichmentRunResult
```

---

## EnrichmentRunResult

```python
@dataclass
class EnrichmentRunResult:
    vendor_id:           str
    workflow_id:         str
    status:              str       # COMPLETED / SKIPPED / BLOCKED / FAILED / PARTIAL
    profile_status:      str | None   # ENRICHED / PARTIALLY_ENRICHED / PROVISIONAL / FAILED_ENRICHMENT
    overall_confidence:  str | None
    flags:               list[str]
    triage_tasks:        list[dict]
    brain_update_suggestions: list[dict]
    pcs_before:          float | None
    pcs_after:           float | None
    error:               str | None
```

---

## Step adapter registry

The runtime engine's STEP_REGISTRY needs entries for each enrichment step:

```python
ENRICHMENT_STEP_REGISTRY = {
    "COLLECT_SOURCES":   _collect_sources_step,
    "EXTRACT_ATTRIBUTES": _extract_attributes_step,
    "MAP_RELATIONSHIPS":  _map_relationships_step,
    "CREATE_PROFILE":     _create_profile_step,
}

def _collect_sources_step(workflow, state, step) -> dict:
    readiness = workflow.context["readiness_result"]
    bundle = external_source_collector.collect(
        vendor_id=workflow.vendor_id,
        readiness=readiness,
    )
    return {
        "source_bundle": bundle.to_dict(),
        "confidence": 0.85 if bundle.sources else 0.20,
        "collection_flags": bundle.collection_flags,
    }
```

Step functions:
- Read context from `workflow.context`
- Read prior step results from `state.completed_steps`
- Return a dict (becomes `result` in StepRunRecord)
- Raise `StepRetryable` for transient failures
- Raise `StepFatal` for permanent failures

---

## Crash recovery for enrichment

If `run_enrichment()` is called and there is already a workflow.json for this vendor:

1. Load existing workflow + state
2. If state.status == COMPLETED → return existing outcome (no rerun)
3. If state.status == IN_PROGRESS or FAILED with retries → resume
4. If state.status == BLOCKED → return blocked result

Same as Process 1 intake — crash recovery is implicit by reading state.json.

---

## Routing decisions

```python
if readiness.skip:
    return EnrichmentRunResult(status="SKIPPED", reason=readiness.skip_reason)

if not readiness.proceed:
    return EnrichmentRunResult(status="BLOCKED", reason="enrichment_blocked")

outcome = engine.execute_workflow(workflow_id, programme_id)

if outcome.status == "COMPLETED":
    profile_result = outcome.state.outcome["profile"]
    return EnrichmentRunResult(
        status="COMPLETED",
        profile_status=profile_result["profile_status"],
        ...
    )

if outcome.status == "FAILED":
    return EnrichmentRunResult(status="FAILED", error=outcome.state.outcome.get("error"))

if outcome.status == "BLOCKED":
    return EnrichmentRunResult(
        status="BLOCKED",
        triage_tasks=outcome.state.outcome["triage_tasks"],
    )
```

---

## Programme-level outputs

After `run_enrichment()` completes for a vendor, update programme-level files:

- Append to `programme_run/enrichment_log.md` (one row per vendor enriched)
- If `BRAIN_UPDATE_PENDING` flag → append to `programme_run/brain_update_queue.md`
- If triage tasks → append to `programme_run/triage_queue.md`

---

## Tests required (integration)

`tests/integration/test_full_enrichment.py`:

- Vendor with all 4 sources available → status=COMPLETED, profile_status=ENRICHED
- Vendor with no digital presence → status=COMPLETED, profile_status=PROVISIONAL, NO_DIGITAL_PRESENCE flag
- WRONG_ENTITY_RISK detected → profile_status=PROVISIONAL, ENTITY_DISAMBIGUATION triage task
- Rebranded vendor (Brain rebrand_map hit) → LIFECYCLE_EVENT_DETECTED flag
- Rebranded vendor (web evidence, not in Brain) → BRAIN_UPDATE_PENDING + REBRAND_MAP suggestion
- Acquisition discovered from news → parent_company updated + ACQUISITION_MAP suggestion
- POSSIBLY_DEFUNCT signals → LIFECYCLE_CONFIRMATION triage task
- Last enriched 30 days ago → status=SKIPPED
- Entity status TRIAGE_REQUIRED → status=BLOCKED
- Crash mid-enrichment (after Tool 2 completes) → resume from Tool 3 with bundle preserved
- Conflicting sources (LinkedIn says 500, registry says 5000) → SIZE_SIGNALS_CONFLICT, conflict in profile
- Marketing language in vendor description → stripped before extraction
- Hybrid vendor (Amazon: retail + cloud) → primary + additional_categories
- Brand confusion (Instagram → Meta) → resolved via Brain brand_map
- Multiple entities with same name → DISAMBIGUATION_REQUIRED → triage task
