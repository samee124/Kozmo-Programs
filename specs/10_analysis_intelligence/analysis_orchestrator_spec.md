# analysis_orchestrator (Process 4 Orchestrator)

## Overview

**File:** `src/cobalt/orchestrator/analysis_orchestrator.py`
**Role:** Wires the 7 Process 4 tools end-to-end via the RuntimeEngine. Handles gate
checks, crash recovery, workspace writes, DB sync, and historical state management.
**Mirrors:** `rs_orchestrator.py` pattern exactly.

---

## Purpose

`analysis_orchestrator.run_analysis()` is the entry point for Process 4.
Uses the same V2 runtime layer as the enrichment and RS orchestrators.
The only component that writes to the vendor workspace in P4.
Individual tools return in-memory objects — the orchestrator commits results.

---

## Public API

```python
def run_analysis(
    vendor_id: str,
    programme_id: str,
    force: bool = False,
) -> ANRunResult:
    """Drive full P4 pipeline for one vendor. Never raises."""

def run_analysis_all_confirmed(
    programme_id: str,
    **kwargs,
) -> list[ANRunResult]:
    """Run P4 for every CONFIRMED vendor in the programme."""
```

---

## Gate checks (before workflow creation)

| Gate | Condition | Result |
|---|---|---|
| Entity confirmed | `entity.md` exists AND `status = CONFIRMED` | BLOCKED if not met |
| P3 complete | `relationship_spend_profile.md` exists | BLOCKED if not met |
| Analysis fresh | `analysis_result.md` exists AND `last_analysed_at` < 30 days AND `force=False` | SKIPPED |
| P2 profile missing | `vendor_profile.md` does not exist | Warn only — continue |

---

## Historical state loading (before workflow creation)

Read these files if they exist (all optional — pass None if absent):
```python
history_dir = vendor_path / "history"

historical_scores     = _load_json(history_dir / "score_history.json",    HistoricalScoreState)
historical_qa         = _load_json(history_dir / "qa_history.json",       HistoricalQAState)
historical_evidence   = _load_json(history_dir / "evidence_state.json",   HistoricalEvidenceState)
historical_commercial = _load_json(history_dir / "commercial_state.json", HistoricalCommercialState)
action_history        = _load_json(history_dir / "action_history.json",   ActionOutcomeHistory)
```

---

## Workflow steps (7 steps, strict execution order)

| Step ID | Step Type | Tool called | Depends on |
|---|---|---|---|
| `s1_validate` | `VALIDATE_EVIDENCE` | `evidence_validator.validate_evidence()` | — |
| `s2_commercial` | `ANALYSE_COMMERCIAL` | `commercial_analyser.analyse_commercial()` | s1_validate |
| `s3_inquire` | `RUN_INQUIRY` | `inquiry_engine.run_inquiry()` | s1_validate, s2_commercial |
| `s4_score` | `COMPUTE_SCORES` | `scoring_engine.compute_scores()` | s3_inquire, s2_commercial |
| `s5_trend` | `ANALYSE_TRENDS` | `trend_analyser.analyse_trends()` | s4_score |
| `s6_findings` | `DETECT_FINDINGS` | `finding_engine.detect_findings()` | s4_score, s5_trend, s3_inquire, s2_commercial, s1_validate |
| `s7_narrative` | `GENERATE_NARRATIVES` | `narrative_engine.generate_narratives()` | s6_findings, s4_score, s3_inquire, s2_commercial, s1_validate |

**Step adapter pattern (mirrors rs_orchestrator):**
```python
AN_STEP_REGISTRY = {
    "VALIDATE_EVIDENCE":    _validate_evidence_step,
    "ANALYSE_COMMERCIAL":   _analyse_commercial_step,
    "RUN_INQUIRY":          _run_inquiry_step,
    "COMPUTE_SCORES":       _compute_scores_step,
    "ANALYSE_TRENDS":       _analyse_trends_step,
    "DETECT_FINDINGS":      _detect_findings_step,
    "GENERATE_NARRATIVES":  _generate_narratives_step,
}

def _validate_evidence_step(workflow, state, step) -> dict:
    ctx = workflow.context
    result = evidence_validator.validate_evidence(
        vendor_id=workflow.vendor_id,
        programme_id=workflow.programme_id,
        doc_intelligence=ctx.get("doc_intelligence"),
        structured_bundle=ctx.get("structured_bundle"),
        signal_bundle=ctx.get("signal_bundle"),
        vendor_file=ctx.get("vendor_file"),
        historical_state=ctx.get("historical_evidence"),
    )
    return {"validated_assembly": result.to_dict()}
```

---

## Context loading before workflow creation

Load these into `workflow.context`:
```python
# Load P3 outputs
rs_profile_path = vendor_path / "relationship_spend_profile.md"
rs_profile = _read_rs_profile(rs_profile_path)

doc_intelligence  = _load_rs_step_result(vendor_id, programme_id, "doc_intelligence")
structured_bundle = _load_rs_step_result(vendor_id, programme_id, "structured_bundle")

# Load vendor file (entity.md + rs profile merged)
vendor_file = _build_vendor_file(vendor_id, programme_id, workspace)

# Historical state (loaded above)
context = {
    "rs_profile": rs_profile.to_dict() if rs_profile else None,
    "doc_intelligence": doc_intelligence,
    "structured_bundle": structured_bundle,
    "signal_bundle": None,   # signal_processor not yet implemented
    "vendor_file": vendor_file,
    "historical_scores": historical_scores.to_dict() if historical_scores else None,
    "historical_qa": historical_qa.to_dict() if historical_qa else None,
    "historical_evidence": historical_evidence.to_dict() if historical_evidence else None,
    "historical_commercial": historical_commercial.to_dict() if historical_commercial else None,
    "action_history": action_history.to_dict() if action_history else None,
}
```

---

## Post-run writes (orchestrator only)

After all 7 steps complete:

### 1. Write analysis_result.md
Path: `workspace/{programme_id}/{vendor_id}/analysis_result.md`

YAML front-matter:
```yaml
vendor_id: {vendor_id}
programme_id: {programme_id}
cri_score: {cri_score}
health_band: {health_band}
vendor_state: {vendor_state}
finding_count: {len(findings)}
nba_action: {nba.action if nba else null}
pcs_contribution: {pcs_contribution}
pcs_total: {pcs_total}
last_analysed_at: {now_iso}
flags: [{flag1}, ...]
```

Markdown body:
```markdown
## Vendor Summary
{vendor_summary}

## Next Best Action
**Action:** {nba.action}
**Why:** {nba.why}
**Owner:** {nba.owner}
**Timing:** {nba.timing}

## Top Findings
| # | Title | Severity | Source |
|---|-------|----------|--------|
| 1 | {finding.title} | {finding.severity} | {finding.source} |
...

## All Findings
| Finding ID | Title | Severity | Source | Status |
|------------|-------|----------|--------|--------|
...

## Scores
| Dimension | Score | Prior | Delta | Trend |
|-----------|-------|-------|-------|-------|
...
CRI: {cri_score} — {health_band}

## Evidence Gaps
| Description | Severity | Suggested Action |
|-------------|----------|-----------------|
...
```

Use `atomic_write(path, content)`.

### 2. Write history files
Use `atomic_write()` for each:
```python
# Append current run to score history
if historical_scores:
    new_runs = historical_scores.runs + [current_run_entry]
else:
    new_runs = [current_run_entry]

score_history = HistoricalScoreState(vendor_id=vendor_id, runs=new_runs)
atomic_write(history_dir / "score_history.json", json.dumps(score_history.to_dict()))

# Similarly for qa_history, evidence_state, commercial_state
# action_history preserved as-is (updated by PA tools in P5, not here)
```

Where `current_run_entry`:
```python
{
    "run_at": now_iso,
    "cri_score": score_bundle.cri_score,
    "health_band": score_bundle.health_band,
    "dimension_scores": {ds.dimension: ds.score for ds in score_bundle.dimension_scores},
}
```

### 3. Append to ledger
```python
ledger_entry = (
    f"| {now_iso} | ANALYSIS_COMPLETE | {vendor_id} | "
    f"CRI={cri_score} {health_band} | findings={finding_count} | "
    f"nba={nba_action or 'none'} |"
)
append_md(vendor_path / "execution" / "ledger.md", ledger_entry)
```

### 4. DB sync
```python
vendor_state = state_classifier.classify_vendor_state(
    cri_score=score_bundle.cri_score,
    open_findings=len(findings_bundle.findings),
    trend_direction=_get_overall_trend(trend_report),
    renewal_days=_get_renewal_days(rs_profile),
    flags=[],
)

sync_to_db(vendor_id, programme_id, {
    "cri_score":       score_bundle.cri_score,
    "health_band":     score_bundle.health_band,
    "vendor_state":    vendor_state,
    "last_analysed_at": datetime.utcnow(),
})
```

### 5. Triage tasks → DB
```python
for task in findings_bundle.triage_tasks:
    if not _triage_exists(vendor_id, programme_id, task["description"]):
        db.insert(TriageItem(
            triage_id=generate_id(),
            vendor_id=vendor_id,
            programme_id=programme_id,
            triage_type=task["triage_type"],
            question=task["question"],
            status="PENDING",
            sla_deadline=parse_iso(task["due_date"]),
        ))
```

### 6. PCS update
```python
flags = []
if score_bundle.cri_score is not None:
    flags.append("CRI_COMPUTED")
if findings_bundle.findings:
    flags.append("FINDINGS_DETECTED")
if all(ds.confidence in ["HIGH", "MEDIUM"] for ds in score_bundle.dimension_scores):
    flags.append("ALL_DIMS_SCORED")

pcs_before = _read_current_pcs(vendor_id, programme_id)
pcs_contribution, pcs_total = pcs.compute_pcs(pcs_before, flags, "P4")
```

### 7. Programme-level log
```python
append_md(
    programme_path / "analysis_log.md",
    f"| {now_iso} | {vendor_id} | {cri_score} | {health_band} | "
    f"{finding_count} | {nba_action or 'none'} | COMPLETED |"
)
```

---

## `run_analysis_all_confirmed`

```python
def run_analysis_all_confirmed(programme_id: str, **kwargs) -> list[ANRunResult]:
    confirmed_vendors = db.query_confirmed_vendors(programme_id)
    results = []
    for vendor_id in confirmed_vendors:
        result = run_analysis(vendor_id, programme_id, **kwargs)
        results.append(result)
    return results
```

Sequential in V1. Failures in one vendor do not affect others.

---

## Crash recovery

Same pattern as `rs_orchestrator.py`:
- If `workflow.json` exists and `state.status = COMPLETED` → return existing result
- If `state.status = IN_PROGRESS` → resume from first non-DONE step
- Prior step results read from `state.completed_steps`

---

## ANRunResult population

```python
return ANRunResult(
    vendor_id=vendor_id,
    programme_id=programme_id,
    status="COMPLETED",
    cri_score=score_bundle.cri_score,
    health_band=score_bundle.health_band,
    finding_count=len(findings_bundle.findings),
    nba_action=findings_bundle.nba.action if findings_bundle.nba else None,
    pcs_before=pcs_before,
    pcs_after=pcs_total,
    tools_run=["s1_validate","s2_commercial","s3_inquire","s4_score","s5_trend","s6_findings","s7_narrative"],
    skip_reason=None,
    error=None,
    analysed_at=now_iso,
)
```

---

## Tests required — tests/orchestrator/test_analysis_orchestrator.py

- entity.md absent → status=BLOCKED, no workflow created
- relationship_spend_profile.md absent → status=BLOCKED
- last_analysed_at 10 days ago + force=False → status=SKIPPED, skip_reason="analysis_fresh"
- last_analysed_at 10 days ago + force=True → proceeds to run
- vendor_profile.md missing → warning logged, execution continues
- Happy path all 7 steps → status=COMPLETED, cri_score populated, analysed_at set
- Step s3 crash (StepFatal) → status=FAILED, error populated, s4-s7 not run
- Crash recovery: s1+s2+s3 DONE in state.json → run_analysis resumes from s4
- analysis_result.md written to correct workspace path after COMPLETED run
- YAML front-matter in analysis_result.md contains all required keys
- history/score_history.json written after COMPLETED run
- DB columns updated: cri_score, health_band, vendor_state, last_analysed_at
- Triage tasks from findings_bundle inserted into TriageItem table
- run_analysis_all_confirmed with 3 confirmed vendors → returns list of 3 ANRunResults
- run_analysis_all_confirmed: one vendor fails → remaining vendors still processed
