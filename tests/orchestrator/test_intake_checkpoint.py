"""Tests for IntakeCheckpoint crash-recovery integration in run_intake."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cobalt.core.exceptions import CheckpointParseError
from cobalt.models.schemas.signal_profile_schema import (
    ApSignal,
    BrainHit,
    DedupResult,
    EntityType,
    ErpSignal,
    ScriptType,
    SignalProfile,
)
from cobalt.orchestrator.intake_orchestrator import (
    IntakeCheckpoint,
    IntakeRunResult,
    run_intake,
)
from cobalt.tools.entity_decision_and_shell_creation import EntityDecision
from cobalt.tools.external_validation import ValidationResult
from cobalt.tools.source_intake import DocRecord, RawCandidate, SourceIntakeResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _no_hit() -> BrainHit:
    return BrainHit(
        matched=False, confidence=0.0, canonical=None, match_type=None,
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def _make_profile(raw: str = "Acme", comparison_key: str = "acme") -> SignalProfile:
    return SignalProfile(
        raw=raw,
        cleaned=raw,
        script_type=ScriptType.LATIN,
        country_hint=None,
        normalized=raw.lower(),
        comparison_key=comparison_key,
        brain_hit=_no_hit(),
        erp_signal=ErpSignal(exists=False, spend=None, category=None, vendor_ids=[]),
        ap_signal=ApSignal(invoice_count=0, flags=[], single_approver=False),
        linked_doc_ids=[],
        spend_hint=None,
        category_hint=None,
        entity_type=EntityType.COMPANY,
        dedup_result=DedupResult(status="UNIQUE", match_key=None, match_name=None, similarity=0.0),
    )


def _make_confirmed_decision() -> EntityDecision:
    return EntityDecision(
        vendor_id="v-test",
        status="CONFIRMED",
        workspace_path=None,
        data_class="CLASS_D",
        initial_pcs=0,
        confidence=0.8,
        fraud_risk="LOW",
        triage_question=None,
        block_reason=None,
        files_written=[],
    )


def _make_resolution(comparison_key: str, recommendation: str = "DISCARD"):
    from cobalt.tools.entity_resolution import ResolutionResult
    return ResolutionResult(
        comparison_key=comparison_key,
        signal_profile=_make_profile(comparison_key, comparison_key),
        resolution_status="MATCHED",
        matched_canonical=None,
        match_type=None,
        confidence=0.0,
        entity_type=EntityType.COMPANY,
        recommendation=recommendation,
    )


def _make_screened(raw: str, comparison_key: str):
    from cobalt.tools.candidate_screening import ScreenedCandidate
    return ScreenedCandidate(
        raw=raw,
        cleaned=raw,
        normalized=raw.lower(),
        comparison_key=comparison_key,
        country_hint=None,
        entity_type=EntityType.COMPANY,
        disposition="PASS",
        rejection_reason=None,
    )


def _patch_all(
    monkeypatch,
    *,
    screened=None,
    resolutions=None,
    decide_fn=None,
    process_call_counter: dict | None = None,
):
    """Patch all external dependencies of run_intake."""
    si = SourceIntakeResult(
        candidates=[],
        doc_registry={},
        source_summary={"candidates_total": 0, "doc_count": 0},
    )
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.source_intake_run", lambda *a, **kw: si)
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.screen_all", lambda e: screened or [])
    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.build_batch_context",
        lambda *a, **kw: _empty_batch_ctx(),
    )
    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.resolve_all",
        lambda s, ctx: resolutions or [],
    )
    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.validate",
        lambda *a, **kw: ValidationResult(
            entry="test", profile=_make_profile(), plan=_make_plan(),
            step_results=[], final_status="CONFIRMED", exit_reason=None,
        ),
    )
    if decide_fn is not None:
        monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.decide_and_create", decide_fn)
    else:
        monkeypatch.setattr(
            "cobalt.orchestrator.intake_orchestrator.decide_and_create",
            lambda *a, **kw: _make_confirmed_decision(),
        )
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.PlanningAgent", _MockPA)
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.ResearchAgent", _MockRA)
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.AnalysisAgent", _MockAA)


def _empty_batch_ctx():
    from cobalt.models.schemas.signal_profile_schema import BatchContext
    return BatchContext(known_vendors={}, rebrand_map={}, alias_map={},
                       erp_hits={}, ap_counts={}, all_keys=[], doc_registry={})


def _make_plan():
    from cobalt.models.schemas.investigation_plan_schema import (
        FraudRisk, InvestigationDepth, InvestigationPlan,
    )
    return InvestigationPlan(
        depth=InvestigationDepth.FAST, steps=[], require_human_gate=False,
        require_legal_gate=False, fraud_risk=FraudRisk.LOW,
        resolving_question=None, reason="test",
    )


class _MockPA:
    def write_programme_plan(self, prog, summary): pass
    def write_investigation_plan(self, prog, key, profile): return None, _make_plan()
    def compute_investigation_plan(self, profile): return _make_plan()


class _MockRA:
    def web_research(self, *a, **kw): return ""
    def erp_batch_scan(self, keys): return {}
    def ap_batch_scan(self, keys): return {}
    def fetch_google_drive_docs(self, path): return []


class _MockAA:
    def extract_vendor_name_from_doc(self, raw, filename): return {"vendor_name": None}
    def extract_contract_terms(self, raw, name): return {}
    def consolidate_documents(self, terms): return terms[0] if terms else {}


def _checkpoint_path(tmp_path: Path, programme_id: str = "prog-ck") -> Path:
    return tmp_path / programme_id / "programme_run" / "checkpoint.json"


# ---------------------------------------------------------------------------
# 1. Fresh run — checkpoint created, all processed, ends COMPLETED
# ---------------------------------------------------------------------------

def test_fresh_run_creates_completed_checkpoint(tmp_workspace, monkeypatch):
    keys = ["alpha", "beta"]
    screened = [_make_screened(k, k) for k in keys]
    resolutions = [_make_resolution(k, "DISCARD") for k in keys]
    _patch_all(monkeypatch, screened=screened, resolutions=resolutions)

    run_intake("prog-ck")

    ck_path = _checkpoint_path(tmp_workspace)
    assert ck_path.exists()
    data = json.loads(ck_path.read_text(encoding="utf-8"))
    assert data["status"] == "COMPLETED"
    assert data["programme_id"] == "prog-ck"
    assert sorted(data["completed_candidates"]) == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# 2. Restart with COMPLETED checkpoint → returns immediately, zero processing
# ---------------------------------------------------------------------------

def test_completed_checkpoint_returns_immediately(tmp_workspace, monkeypatch):
    call_counter = {"n": 0}

    def counting_decide(*a, **kw):
        call_counter["n"] += 1
        return _make_confirmed_decision()

    # Pre-seed a COMPLETED checkpoint
    ck_path = _checkpoint_path(tmp_workspace)
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    ck = IntakeCheckpoint(
        programme_id="prog-ck",
        started_at=_now(),
        last_updated=_now(),
        status="COMPLETED",
        completed_candidates=["alpha", "beta"],
        failed_candidates=[],
        total_candidates=2,
    )
    ck.save(ck_path)

    _patch_all(monkeypatch, decide_fn=counting_decide)
    result = run_intake("prog-ck")

    # Must return without calling decide_and_create even once
    assert call_counter["n"] == 0, (
        f"Expected 0 calls to decide_and_create, got {call_counter['n']}"
    )
    assert isinstance(result, IntakeRunResult)


# ---------------------------------------------------------------------------
# 3. Partial checkpoint — only 2 remaining candidates processed
# ---------------------------------------------------------------------------

def test_partial_checkpoint_skips_completed(tmp_workspace, monkeypatch):
    """3 of 5 candidates already in checkpoint → only delta and epsilon processed."""
    all_keys = ["alpha", "beta", "gamma", "delta", "epsilon"]
    already_done = ["alpha", "beta", "gamma"]
    remaining = ["delta", "epsilon"]

    # Pre-seed an IN_PROGRESS checkpoint with 3 done
    ck_path = _checkpoint_path(tmp_workspace)
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    ck = IntakeCheckpoint(
        programme_id="prog-ck",
        started_at=_now(),
        last_updated=_now(),
        status="IN_PROGRESS",
        completed_candidates=list(already_done),
        failed_candidates=[],
        total_candidates=5,
    )
    ck.save(ck_path)

    screened = [_make_screened(k, k) for k in all_keys]
    resolutions = [_make_resolution(k, "DISCARD") for k in all_keys]
    _patch_all(monkeypatch, screened=screened, resolutions=resolutions)
    run_intake("prog-ck")

    # After run: only delta and epsilon should have been added to completed_candidates
    # (alpha/beta/gamma were already there and are NOT re-appended).
    data = json.loads(ck_path.read_text(encoding="utf-8"))
    newly_added = [k for k in data["completed_candidates"] if k not in already_done]
    assert sorted(newly_added) == sorted(remaining), (
        f"Expected newly added: {sorted(remaining)}, got: {sorted(newly_added)}"
    )
    # And the pre-existing ones are still there
    for k in already_done:
        assert k in data["completed_candidates"]


# ---------------------------------------------------------------------------
# 4. Checkpoint updated after each candidate
# ---------------------------------------------------------------------------

def test_checkpoint_updated_after_each_candidate(tmp_workspace, monkeypatch):
    keys = ["alpha", "beta", "gamma"]
    screened = [_make_screened(k, k) for k in keys]
    resolutions = [_make_resolution(k, "DISCARD") for k in keys]
    _patch_all(monkeypatch, screened=screened, resolutions=resolutions)

    run_intake("prog-ck")

    ck_path = _checkpoint_path(tmp_workspace)
    data = json.loads(ck_path.read_text(encoding="utf-8"))
    # After full run, all 3 should be in completed_candidates
    assert sorted(data["completed_candidates"]) == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# 5. Failed candidate lands in failed_candidates, not completed
# ---------------------------------------------------------------------------

def test_failed_candidate_in_failed_candidates(tmp_workspace, monkeypatch):
    # alpha → DISCARD (no decide_and_create call)
    # bad-one → AUTO_CONFIRM → decide_and_create raises → lands in failed_candidates
    screened = [_make_screened("alpha", "alpha"), _make_screened("bad-one", "bad-one")]
    resolutions = [
        _make_resolution("alpha", "DISCARD"),
        _make_resolution("bad-one", "AUTO_CONFIRM"),
    ]

    def failing_decide(entry, profile, **kw):
        if profile.comparison_key == "bad-one":
            raise RuntimeError("Simulated failure")
        return _make_confirmed_decision()

    _patch_all(monkeypatch, screened=screened, resolutions=resolutions, decide_fn=failing_decide)
    run_intake("prog-ck")

    ck_path = _checkpoint_path(tmp_workspace)
    data = json.loads(ck_path.read_text(encoding="utf-8"))
    assert "alpha" in data["completed_candidates"]
    assert "bad-one" in data["failed_candidates"]
    assert "bad-one" not in data["completed_candidates"]


# ---------------------------------------------------------------------------
# 6. Final checkpoint status=COMPLETED, total = len(completed) + len(failed)
# ---------------------------------------------------------------------------

def test_final_checkpoint_total_matches(tmp_workspace, monkeypatch):
    keys = ["alpha", "beta", "bad"]
    screened = [_make_screened(k, k) for k in keys]
    resolutions = [_make_resolution(k, "DISCARD") for k in keys]

    def mixed_decide(entry, profile, **kw):
        if profile.comparison_key == "bad":
            raise RuntimeError("forced failure")
        return _make_confirmed_decision()

    _patch_all(monkeypatch, screened=screened, resolutions=resolutions, decide_fn=mixed_decide)
    run_intake("prog-ck")

    ck_path = _checkpoint_path(tmp_workspace)
    data = json.loads(ck_path.read_text(encoding="utf-8"))
    assert data["status"] == "COMPLETED"
    total = len(data["completed_candidates"]) + len(data["failed_candidates"])
    assert total == data["total_candidates"]


# ---------------------------------------------------------------------------
# 7. Malformed checkpoint JSON → raises CheckpointParseError
# ---------------------------------------------------------------------------

def test_malformed_checkpoint_raises(tmp_path):
    ck_path = tmp_path / "prog-ck" / "programme_run" / "checkpoint.json"
    ck_path.parent.mkdir(parents=True)
    ck_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(CheckpointParseError):
        IntakeCheckpoint.load(ck_path)


# ---------------------------------------------------------------------------
# 8. COMPLETED checkpoint trusted even if candidate list differs
# ---------------------------------------------------------------------------

def test_completed_checkpoint_trusted_regardless_of_candidates(tmp_workspace, monkeypatch):
    # Pre-seed COMPLETED checkpoint with different candidate list
    ck_path = _checkpoint_path(tmp_workspace)
    ck_path.parent.mkdir(parents=True, exist_ok=True)
    ck = IntakeCheckpoint(
        programme_id="prog-ck",
        started_at=_now(),
        last_updated=_now(),
        status="COMPLETED",
        completed_candidates=["old-vendor-1", "old-vendor-2"],
        failed_candidates=[],
        total_candidates=2,
    )
    ck.save(ck_path)

    call_counter = {"n": 0}

    def counting_decide(*a, **kw):
        call_counter["n"] += 1
        return _make_confirmed_decision()

    # Mock now returns 3 NEW candidates — checkpoint says 2 old ones were done
    _patch_all(
        monkeypatch,
        screened=[_make_screened(k, k) for k in ["new-a", "new-b", "new-c"]],
        resolutions=[_make_resolution(k, "DISCARD") for k in ["new-a", "new-b", "new-c"]],
        decide_fn=counting_decide,
    )

    result = run_intake("prog-ck")

    # Checkpoint status=COMPLETED → return immediately, trust checkpoint
    assert call_counter["n"] == 0
    assert isinstance(result, IntakeRunResult)


# ---------------------------------------------------------------------------
# 9. IntakeCheckpoint save → load round trip
# ---------------------------------------------------------------------------

def test_checkpoint_save_load_round_trip(tmp_path):
    ck_path = tmp_path / "checkpoint.json"
    ck = IntakeCheckpoint(
        programme_id="prog-rt",
        started_at="2026-05-12T10:00:00Z",
        last_updated="2026-05-12T10:05:00Z",
        status="IN_PROGRESS",
        completed_candidates=["alpha", "beta"],
        failed_candidates=["gamma"],
        total_candidates=10,
    )
    ck.save(ck_path)

    loaded = IntakeCheckpoint.load(ck_path)
    assert loaded is not None
    assert loaded.programme_id == "prog-rt"
    assert loaded.status == "IN_PROGRESS"
    assert loaded.completed_candidates == ["alpha", "beta"]
    assert loaded.failed_candidates == ["gamma"]
    assert loaded.total_candidates == 10
