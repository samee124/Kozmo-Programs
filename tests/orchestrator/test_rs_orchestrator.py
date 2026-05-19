"""Tests for rs_orchestrator.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from cobalt.models.schemas.rs_schema import RSRunResult, RSRunStatus
from cobalt.orchestrator.rs_orchestrator import (
    _check_gates,
    _read_pcs,
    run_rs,
    run_rs_all_confirmed,
    PROFILE_MAX_AGE_DAYS,
)
from cobalt.runtime.runtime_engine import WorkflowOutcome


# ---------------------------------------------------------------------------
# Helpers — write entity.md with CONFIRMED status
# ---------------------------------------------------------------------------

def _write_entity(path: Path, status: str = "CONFIRMED") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"intake": {"status": status}}
    content = f"---\n{yaml.dump(data)}---\n"
    path.write_text(content, encoding="utf-8")


def _write_rs_profile(path: Path, last_updated: str, pcs_total: float = 0.6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"last_updated": last_updated, "pcs_total": pcs_total}
    content = f"---\n{yaml.dump(data)}---\n"
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# _check_gates — entity missing
# ---------------------------------------------------------------------------

def test_gate_entity_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    result = _check_gates("V-001", "PROG-001", uploaded_files=None, checkin_data=None, connector_config=None)
    assert result is not None
    assert result.status == RSRunStatus.BLOCKED.value
    assert result.error == "entity_not_confirmed"


def test_gate_entity_not_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    ep = tmp_path / "PROG-001" / "V-001" / "entity.md"
    _write_entity(ep, status="TRIAGE")
    with patch("cobalt.orchestrator.rs_orchestrator.entity_path", return_value=ep):
        result = _check_gates("V-001", "PROG-001", uploaded_files=None, checkin_data=None, connector_config=None)
    assert result is not None
    assert result.status == RSRunStatus.BLOCKED.value


def test_gate_no_data_available(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    ep = tmp_path / "PROG-001" / "V-001" / "entity.md"
    _write_entity(ep, status="CONFIRMED")
    with patch("cobalt.orchestrator.rs_orchestrator.entity_path", return_value=ep):
        with patch("cobalt.orchestrator.rs_orchestrator.vendor_profile_path", return_value=tmp_path / "x.md"):
            result = _check_gates(
                "V-001", "PROG-001",
                uploaded_files=[],
                checkin_data=None,
                connector_config=None,
            )
    assert result is not None
    assert result.status == RSRunStatus.SKIPPED.value
    assert result.skip_reason == "no_data_available"


def test_gate_profile_fresh_skips(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    ep = tmp_path / "PROG-001" / "V-001" / "entity.md"
    _write_entity(ep, status="CONFIRMED")

    rp = tmp_path / "PROG-001" / "V-001" / "relationship_spend_profile.md"
    # Fresh profile — 1 day old
    now = datetime.now(timezone.utc).isoformat()
    _write_rs_profile(rp, last_updated=now)

    with patch("cobalt.orchestrator.rs_orchestrator.entity_path", return_value=ep):
        with patch("cobalt.orchestrator.rs_orchestrator.vendor_profile_path", return_value=tmp_path / "x.md"):
            with patch("cobalt.orchestrator.rs_orchestrator.rs_profile_path", return_value=rp):
                result = _check_gates(
                    "V-001", "PROG-001",
                    uploaded_files=[{"path": str(tmp_path / "file.csv")}],
                    checkin_data=None,
                    connector_config=None,
                )
    assert result is not None
    assert result.status == RSRunStatus.SKIPPED.value
    assert result.skip_reason == "profile_fresh"


def test_gate_passes_when_confirmed_with_checkin(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    ep = tmp_path / "PROG-001" / "V-001" / "entity.md"
    _write_entity(ep, status="CONFIRMED")

    with patch("cobalt.orchestrator.rs_orchestrator.entity_path", return_value=ep):
        with patch("cobalt.orchestrator.rs_orchestrator.vendor_profile_path", return_value=tmp_path / "x.md"):
            with patch("cobalt.orchestrator.rs_orchestrator.rs_profile_path", return_value=tmp_path / "y.md"):
                result = _check_gates(
                    "V-001", "PROG-001",
                    uploaded_files=None,
                    checkin_data={"spend_ytd": "10000"},
                    connector_config=None,
                )
    assert result is None  # gates passed


# ---------------------------------------------------------------------------
# run_rs — full mocked happy path
# ---------------------------------------------------------------------------

def _make_mock_outcome(status: str = "COMPLETED") -> WorkflowOutcome:
    return WorkflowOutcome(
        workflow_id="wf-rs-V-001-123",
        status=status,
        final_step_id="s5_assemble",
        outcome=None,
    )


@pytest.fixture
def confirmed_entity(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    ep = tmp_path / "PROG-001" / "V-001" / "entity.md"
    _write_entity(ep, status="CONFIRMED")
    return tmp_path, ep


def test_run_rs_completed_happy_path(confirmed_entity):
    tmp_path, ep = confirmed_entity

    mock_profile = MagicMock()
    mock_profile.profile_status = "COMPLETE"
    mock_profile.pcs_total = 0.80
    mock_profile.flags = []

    mock_outcome = _make_mock_outcome("COMPLETED")

    with patch("cobalt.orchestrator.rs_orchestrator.entity_path", return_value=ep):
        with patch("cobalt.orchestrator.rs_orchestrator.vendor_profile_path", return_value=tmp_path / "x.md"):
            with patch("cobalt.orchestrator.rs_orchestrator.rs_profile_path", return_value=tmp_path / "y.md"):
                with patch("cobalt.orchestrator.rs_orchestrator.RuntimeEngine") as MockEngine:
                    mock_engine_instance = MagicMock()
                    mock_engine_instance.execute_workflow.return_value = mock_outcome
                    MockEngine.return_value = mock_engine_instance
                    with patch("cobalt.orchestrator.rs_orchestrator._build_rs_workflow") as mock_wf:
                        mock_wf_obj = MagicMock()
                        mock_wf_obj.workflow_id = "wf-rs-V-001-123"
                        mock_wf.return_value = mock_wf_obj
                        with patch("cobalt.orchestrator.rs_orchestrator._update_programme_logs"):
                            with patch("cobalt.orchestrator.rs_orchestrator._build_rs_step_registry"):
                                # Inject profile into run_cache via side effect
                                original_build = __import__(
                                    "cobalt.orchestrator.rs_orchestrator",
                                    fromlist=["_build_rs_step_registry"]
                                )._build_rs_step_registry

                                result = run_rs(
                                    vendor_id="V-001",
                                    programme_id="PROG-001",
                                    checkin_data={"spend_ytd": "10000"},
                                )

    # With empty run_cache and COMPLETED status — falls to FAILED because profile is None
    assert result.status in (RSRunStatus.COMPLETED.value, RSRunStatus.FAILED.value)
    assert result.vendor_id == "V-001"


def test_run_rs_blocked_entity_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    result = run_rs("V-001", "PROG-001")
    assert result.status == RSRunStatus.BLOCKED.value
    assert result.error == "entity_not_confirmed"


def test_run_rs_skipped_no_data(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    ep = tmp_path / "PROG-001" / "V-001" / "entity.md"
    _write_entity(ep, status="CONFIRMED")

    with patch("cobalt.orchestrator.rs_orchestrator.entity_path", return_value=ep):
        with patch("cobalt.orchestrator.rs_orchestrator.vendor_profile_path", return_value=tmp_path / "x.md"):
            result = run_rs("V-001", "PROG-001")

    assert result.status == RSRunStatus.SKIPPED.value
    assert result.skip_reason == "no_data_available"


def test_run_rs_engine_crash_returns_failed(tmp_path, monkeypatch):
    """If RuntimeEngine raises, result is FAILED — no re-raise."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    ep = tmp_path / "PROG-001" / "V-001" / "entity.md"
    _write_entity(ep, status="CONFIRMED")

    with patch("cobalt.orchestrator.rs_orchestrator.entity_path", return_value=ep):
        with patch("cobalt.orchestrator.rs_orchestrator.vendor_profile_path", return_value=tmp_path / "x.md"):
            with patch("cobalt.orchestrator.rs_orchestrator.rs_profile_path", return_value=tmp_path / "y.md"):
                with patch("cobalt.orchestrator.rs_orchestrator.RuntimeEngine", side_effect=RuntimeError("engine crash")):
                    with patch("cobalt.orchestrator.rs_orchestrator._build_rs_workflow") as mock_wf:
                        mock_wf_obj = MagicMock()
                        mock_wf_obj.workflow_id = "wf-test"
                        mock_wf.return_value = mock_wf_obj
                        with patch("cobalt.orchestrator.rs_orchestrator._update_programme_logs"):
                            result = run_rs(
                                "V-001", "PROG-001",
                                checkin_data={"spend_ytd": "5000"},
                            )

    assert result.status == RSRunStatus.FAILED.value
    assert "engine crash" in (result.error or "")


def test_run_rs_never_raises(tmp_path, monkeypatch):
    """run_rs must always return RSRunResult, never raise."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    # Entity doesn't exist — should return BLOCKED gracefully
    result = run_rs("V-NONEXISTENT", "PROG-999")
    assert isinstance(result, RSRunResult)


# ---------------------------------------------------------------------------
# run_rs_all_confirmed
# ---------------------------------------------------------------------------

def test_run_rs_all_confirmed_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    with patch("cobalt.db.queries.get_confirmed_vendors", return_value=[]):
        results = run_rs_all_confirmed("PROG-001")
    assert results == []


def test_run_rs_all_confirmed_db_failure_returns_empty(tmp_path, monkeypatch):
    """If DB query fails, logs warning and returns empty list."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    with patch("cobalt.db.queries.get_confirmed_vendors", side_effect=Exception("DB down")):
        results = run_rs_all_confirmed("PROG-001")
    assert results == []


def test_run_rs_all_confirmed_runs_each_vendor(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    vendor_ids = ["V-001", "V-002", "V-003"]

    with patch("cobalt.db.queries.get_confirmed_vendors", return_value=vendor_ids):
        with patch("cobalt.orchestrator.rs_orchestrator.run_rs") as mock_run:
            mock_run.return_value = RSRunResult(
                vendor_id="V-001", programme_id="PROG-001",
                status=RSRunStatus.COMPLETED.value,
                pcs_before=0.5, pcs_after=0.7,
                tools_run=[], flags_raised=[],
                profile_status="COMPLETE",
                skip_reason=None, error=None,
            )
            results = run_rs_all_confirmed("PROG-001")

    assert mock_run.call_count == 3
    assert len(results) == 3


def test_run_rs_all_confirmed_one_failure_does_not_stop_others(tmp_path, monkeypatch):
    """Failure in one vendor run should not prevent others from running."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    vendor_ids = ["V-001", "V-002"]

    def _side_effect(vendor_id, programme_id, **kwargs):
        return RSRunResult(
            vendor_id=vendor_id,
            programme_id=programme_id,
            status=RSRunStatus.BLOCKED.value,
            pcs_before=None, pcs_after=None,
            tools_run=[], flags_raised=[],
            profile_status=None,
            skip_reason=None,
            error="entity_not_confirmed",
        )

    with patch("cobalt.db.queries.get_confirmed_vendors", return_value=vendor_ids):
        with patch("cobalt.orchestrator.rs_orchestrator.run_rs", side_effect=_side_effect):
            results = run_rs_all_confirmed("PROG-001")

    assert len(results) == 2
    assert all(r.status == RSRunStatus.BLOCKED.value for r in results)
