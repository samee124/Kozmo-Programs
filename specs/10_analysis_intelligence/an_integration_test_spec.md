# test_full_analysis_pipeline (Integration)

## Overview

**File:** `tests/integration/test_full_analysis_pipeline.py`
**Role:** End-to-end integration test for Process 4. Stubs all tools at import boundary.
**Pattern:** Same as `tests/integration/test_full_rs_pipeline.py`.

---

## Setup

Stub all 7 AN tools at the import boundary using monkeypatch.
Do NOT call real LLM. Do NOT write to real DB.
Use tmp_path for workspace. Use fixture rs_profile from fixture file.

```python
@pytest.fixture
def an_workspace(tmp_path):
    """Create minimal workspace with entity.md and relationship_spend_profile.md."""
    # Write entity.md with status=CONFIRMED
    # Write relationship_spend_profile.md with minimal valid content
    # Return (workspace_path, vendor_id, programme_id)

@pytest.fixture
def stub_evidence_validator(monkeypatch):
    """Return a ValidatedEvidenceAssembly with 5 CURRENT facts, completeness=0.45"""

@pytest.fixture
def stub_commercial_analyser(monkeypatch):
    """Return CommercialAnalysisResult with contract_type=SERVICES, sla_adherence=88.0"""

@pytest.fixture
def stub_inquiry_engine(monkeypatch):
    """Return 6 QAPairs — Q1 and Q4 PARTIAL/MEDIUM, rest COMPLETE/HIGH"""

@pytest.fixture
def stub_scoring_engine(monkeypatch):
    """Return ScoreBundle with cri_score=72, health_band=WATCH"""

@pytest.fixture
def stub_trend_analyser(monkeypatch):
    """Return TrendReport with all UNKNOWN (first run)"""

@pytest.fixture
def stub_finding_engine(monkeypatch):
    """Return FindingsBundle with 2 MEDIUM findings, NBA set"""

@pytest.fixture
def stub_narrative_engine(monkeypatch):
    """Return NarrativeBundle with vendor_summary and 2 finding_narratives"""
```

---

## Test scenarios

### Happy path
```python
def test_full_pipeline_completed(an_workspace, all_stubs):
    vendor_id, programme_id, workspace = an_workspace
    result = run_analysis(vendor_id, programme_id)

    assert result.status == "COMPLETED"
    assert result.cri_score == 72
    assert result.health_band == "WATCH"
    assert result.finding_count == 2
    assert result.nba_action is not None

    # analysis_result.md written
    analysis_path = workspace / programme_id / vendor_id / "analysis_result.md"
    assert analysis_path.exists()
    content = analysis_path.read_text()
    assert "cri_score: 72" in content

    # history/score_history.json written
    history_path = workspace / programme_id / vendor_id / "history" / "score_history.json"
    assert history_path.exists()
```

### BLOCKED — no RS profile
```python
def test_blocked_when_no_rs_profile(an_workspace):
    vendor_id, programme_id, workspace = an_workspace
    # Remove relationship_spend_profile.md
    (workspace / programme_id / vendor_id / "relationship_spend_profile.md").unlink()

    result = run_analysis(vendor_id, programme_id)
    assert result.status == "BLOCKED"
    assert "relationship_spend_profile" in result.error.lower()
```

### SKIPPED — analysis fresh
```python
def test_skipped_when_fresh(an_workspace, all_stubs):
    vendor_id, programme_id, workspace = an_workspace
    # Write analysis_result.md with last_analysed_at = 5 days ago
    _write_fresh_analysis_result(workspace, vendor_id, programme_id)

    result = run_analysis(vendor_id, programme_id, force=False)
    assert result.status == "SKIPPED"
    assert result.skip_reason == "analysis_fresh"

def test_force_overrides_fresh(an_workspace, all_stubs):
    vendor_id, programme_id, workspace = an_workspace
    _write_fresh_analysis_result(workspace, vendor_id, programme_id)

    result = run_analysis(vendor_id, programme_id, force=True)
    assert result.status == "COMPLETED"
```

### All evidence missing (poor quality run)
```python
def test_all_evidence_missing(an_workspace, monkeypatch):
    """When all evidence is MISSING, analysis completes with low CRI and findings."""
    # Stub evidence_validator to return all-MISSING assembly
    # Stub inquiry_engine to return all UNANSWERABLE QAPairs
    # Stub scoring_engine to return cri=15, CRITICAL
    # Stub finding_engine to return 5 findings

    result = run_analysis(vendor_id, programme_id)
    assert result.status == "COMPLETED"
    assert result.cri_score == 15
    assert result.finding_count >= 3
```

### LLM failures gracefully handled
```python
def test_llm_failures_in_inquiry_engine(an_workspace, monkeypatch):
    """When inquiry_engine LLM calls all fail, pipeline completes with UNANSWERABLE answers."""
    # Stub inquiry_engine to return all 6 QAPairs with completeness=UNANSWERABLE
    # All other stubs normal

    result = run_analysis(vendor_id, programme_id)
    assert result.status == "COMPLETED"   # never FAILED due to LLM
    assert result.cri_score is not None   # still scored using DEFAULT_SCORE_WHEN_NO_ANSWER

def test_llm_failure_in_narrative_engine(an_workspace, monkeypatch):
    """When narrative_engine LLM fails, fallback text used and pipeline still COMPLETED."""
    # Stub narrative_engine to return NarrativeBundle with fallback vendor_summary
    result = run_analysis(vendor_id, programme_id)
    assert result.status == "COMPLETED"
    analysis_content = (workspace / ...).read_text()
    assert "## Vendor Summary" in analysis_content
```

### Second run with historical state
```python
def test_second_run_produces_trend(an_workspace, all_stubs):
    """After first run, second run uses historical state for trend analysis."""
    # First run
    run_analysis(vendor_id, programme_id)

    # Verify score_history.json exists
    history_path = workspace / programme_id / vendor_id / "history" / "score_history.json"
    assert history_path.exists()

    # Second run — stub trend_analyser to return IMPROVING direction this time
    # Verify analysis_result.md updated with new CRI
    run_analysis(vendor_id, programme_id, force=True)
    content = (workspace / ...).read_text()
    assert "last_analysed_at" in content
```

### Triage tasks inserted to DB
```python
def test_triage_tasks_inserted(an_workspace, all_stubs, db_session):
    """BLOCKING gaps from finding_engine create TriageItem rows."""
    # Stub finding_engine to return FindingsBundle with triage_tasks=[one_task]
    run_analysis(vendor_id, programme_id)

    triage_items = db_session.query(TriageItem).filter_by(
        vendor_id=vendor_id, programme_id=programme_id
    ).all()
    assert len(triage_items) == 1
    assert triage_items[0].status == "PENDING"
```

### Batch run
```python
def test_run_analysis_all_confirmed(multiple_vendor_workspace):
    """Batch processes all confirmed vendors, failures isolated."""
    # 3 vendors: 2 confirmed, 1 triage_required
    results = run_analysis_all_confirmed(programme_id)

    assert len(results) == 2   # only CONFIRMED vendors
    statuses = [r.status for r in results]
    assert "COMPLETED" in statuses or "SKIPPED" in statuses
```
