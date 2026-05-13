"""Tests for enrich_all_confirmed batch wrapper — 8 tests."""

from __future__ import annotations

import pytest

import cobalt.orchestrator.enrichment_orchestrator as mod
from cobalt.orchestrator.enrichment_orchestrator import (
    EnrichmentRunResult,
    enrich_all_confirmed,
)
from cobalt.core.atomic_write import atomic_write


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_register(tmp_path, programme_id: str, vendors: list[dict]) -> None:
    """Write a vendor_register.md matching intake_orchestrator's format."""
    reg_dir = tmp_path / programme_id / "programme_run"
    reg_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        reg_dir / "vendor_register.md",
        {
            "programme_id": programme_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "vendors": vendors,
        },
        programme_id=programme_id,
    )


def _vendor(vid: str) -> dict:
    return {"vendor_id": vid, "data_class": "CLASS_A", "initial_pcs": 0.5, "confidence": 0.9}


def _default_result(vid: str) -> EnrichmentRunResult:
    return EnrichmentRunResult(
        vendor_id=vid,
        workflow_id=f"wf-{vid}-123",
        status="COMPLETED",
        profile_status="PARTIALLY_ENRICHED",
        overall_confidence="MEDIUM",
        flags=[],
        triage_tasks=[],
        brain_update_suggestions=[],
        pcs_before=0.2,
        pcs_after=0.4,
        error=None,
        reason=None,
    )


@pytest.fixture
def mock_run_enrichment(monkeypatch):
    """Replace run_enrichment at module level with a controllable fake.

    responses dict: vendor_id → EnrichmentRunResult (normal case)
                                or Exception subclass instance (raises path)
    """
    calls: list[dict] = []
    responses: dict = {}

    def fake_run_enrichment(**kwargs):
        vid = kwargs.get("vendor_id")
        calls.append(kwargs)
        val = responses.get(vid)
        if isinstance(val, BaseException):
            raise val
        return val if val is not None else _default_result(vid)

    monkeypatch.setattr(mod, "run_enrichment", fake_run_enrichment)
    return {"calls": calls, "responses": responses}


# ---------------------------------------------------------------------------
# Test 1: one call per CONFIRMED vendor
# ---------------------------------------------------------------------------

def test_enrich_all_confirmed_runs_one_per_vendor(tmp_path, mock_run_enrichment):
    """Three vendors in register → run_enrichment called three times in order."""
    _make_register(tmp_path, "nova-2026", [
        _vendor("V-AAA-001"), _vendor("V-BBB-001"), _vendor("V-CCC-001"),
    ])
    results = enrich_all_confirmed("nova-2026", workspace_root=tmp_path)
    assert len(results) == 3
    called_vids = [c["vendor_id"] for c in mock_run_enrichment["calls"]]
    assert called_vids == ["V-AAA-001", "V-BBB-001", "V-CCC-001"]


# ---------------------------------------------------------------------------
# Test 2: empty register returns []
# ---------------------------------------------------------------------------

def test_enrich_all_confirmed_empty_register_returns_empty(tmp_path, mock_run_enrichment):
    """vendor_register.md with no vendors → returns [] without calling run_enrichment."""
    _make_register(tmp_path, "nova-2026", [])
    results = enrich_all_confirmed("nova-2026", workspace_root=tmp_path)
    assert results == []
    assert mock_run_enrichment["calls"] == []


# ---------------------------------------------------------------------------
# Test 3: max_vendors cap
# ---------------------------------------------------------------------------

def test_enrich_all_confirmed_max_vendors_cap(tmp_path, mock_run_enrichment):
    """max_vendors=2 with 5 in register → only 2 calls made."""
    _make_register(tmp_path, "nova-2026", [_vendor(f"V-{i:03d}-001") for i in range(5)])
    results = enrich_all_confirmed("nova-2026", workspace_root=tmp_path, max_vendors=2)
    assert len(results) == 2
    assert len(mock_run_enrichment["calls"]) == 2


# ---------------------------------------------------------------------------
# Test 4: skip_already_enriched=True → manual_override=False
# ---------------------------------------------------------------------------

def test_enrich_all_confirmed_skip_already_enriched_passes_override_false(
    tmp_path, mock_run_enrichment
):
    """skip_already_enriched=True (default) → manual_override=False in run_enrichment call."""
    _make_register(tmp_path, "nova-2026", [_vendor("V-AAA-001")])
    enrich_all_confirmed("nova-2026", workspace_root=tmp_path, skip_already_enriched=True)
    assert mock_run_enrichment["calls"][0]["manual_override"] is False


# ---------------------------------------------------------------------------
# Test 5: skip_already_enriched=False → manual_override=True
# ---------------------------------------------------------------------------

def test_enrich_all_confirmed_force_rerun(tmp_path, mock_run_enrichment):
    """skip_already_enriched=False → manual_override=True forces re-enrichment."""
    _make_register(tmp_path, "nova-2026", [_vendor("V-AAA-001")])
    enrich_all_confirmed("nova-2026", workspace_root=tmp_path, skip_already_enriched=False)
    assert mock_run_enrichment["calls"][0]["manual_override"] is True


# ---------------------------------------------------------------------------
# Test 6: individual failure does not abort batch
# ---------------------------------------------------------------------------

def test_enrich_all_confirmed_continues_on_individual_failure(tmp_path, mock_run_enrichment):
    """run_enrichment raises for vendor #2 → #1 and #3 still processed; #2 gets FAILED result."""
    _make_register(tmp_path, "nova-2026", [
        _vendor("V-AAA-001"), _vendor("V-BBB-001"), _vendor("V-CCC-001"),
    ])
    mock_run_enrichment["responses"]["V-BBB-001"] = RuntimeError("network failure")
    results = enrich_all_confirmed("nova-2026", workspace_root=tmp_path)
    assert len(results) == 3
    assert results[0].status == "COMPLETED"
    assert results[1].status == "FAILED"
    assert "BATCH_WRAPPER_ERROR" in results[1].flags
    assert results[1].reason == "batch_wrapper_unexpected_failure"
    assert results[2].status == "COMPLETED"


# ---------------------------------------------------------------------------
# Test 7: missing register returns []
# ---------------------------------------------------------------------------

def test_enrich_all_confirmed_no_register_returns_empty(tmp_path, mock_run_enrichment):
    """vendor_register.md does not exist → returns [], no crash, no run_enrichment calls."""
    # Do NOT create register file
    results = enrich_all_confirmed("nova-2026", workspace_root=tmp_path)
    assert results == []
    assert mock_run_enrichment["calls"] == []


# ---------------------------------------------------------------------------
# Test 8: on_progress callback invoked once per vendor
# ---------------------------------------------------------------------------

def test_enrich_all_confirmed_on_progress_callback(tmp_path, mock_run_enrichment):
    """on_progress called once per vendor with correct vendor_id and status."""
    _make_register(tmp_path, "nova-2026", [_vendor("V-AAA-001"), _vendor("V-BBB-001")])
    progress_calls: list[tuple[str, str]] = []

    def on_progress(vendor_id: str, status: str, result: EnrichmentRunResult) -> None:
        progress_calls.append((vendor_id, status))

    enrich_all_confirmed("nova-2026", workspace_root=tmp_path, on_progress=on_progress)
    assert len(progress_calls) == 2
    assert progress_calls[0] == ("V-AAA-001", "COMPLETED")
    assert progress_calls[1] == ("V-BBB-001", "COMPLETED")
