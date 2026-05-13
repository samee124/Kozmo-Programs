"""Tests for src/cobalt/orchestrator/enrichment_orchestrator.py — 20 tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import cobalt.orchestrator.enrichment_orchestrator as mod
from cobalt.orchestrator.enrichment_orchestrator import (
    _assemble_result,
    _build_enrichment_step_registry,
    _read_entity_md,
    _read_pcs_from_coverage,
    _restore_bundle,
    _restore_extracted,
    _restore_relationship,
    run_enrichment,
)
from cobalt.core.exceptions import EnrichmentReadinessReadError
from cobalt.models.schemas.enrichment_schema import (
    BrainUpdateSuggestion,
    EnrichedProfileResult,
    EnrichmentReadinessResult,
    EnrichmentRunResult,
    ExtractedAttributes,
    KnownFacts,
    LifecycleSignal,
    RelationshipLifecycleResult,
    RelationshipMap,
    SourceEvidenceBundle,
    SourceEvidenceItem,
)
from cobalt.runtime.execution_state import ExecutionState, ExecutionStatus, StepRunRecord
from cobalt.runtime.runtime_engine import WorkflowOutcome
from cobalt.runtime.workflow_definition import StepStatus


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _make_readiness(
    vendor_id: str = "v-acme",
    skip: bool = False,
    skip_reason: str | None = None,
    flags: list[str] | None = None,
    depth_tier: str = "STANDARD",
    confidence_floor: float = 0.70,
) -> EnrichmentReadinessResult:
    return EnrichmentReadinessResult(
        vendor_id=vendor_id,
        proceed=not skip,
        skip=skip,
        skip_reason=skip_reason,
        depth_tier=depth_tier,
        source_list=["WEB_SEARCH", "COMPANY_WEBSITE"],
        query_count=2,
        known_facts=KnownFacts(confirmed=[], gaps=[], conflicts=[]),
        confidence_floor=confidence_floor,
        flags=flags or [],
    )


def _make_bundle(vendor_id: str = "v-acme") -> SourceEvidenceBundle:
    return SourceEvidenceBundle(
        vendor_id=vendor_id,
        depth_tier="STANDARD",
        sources={"WEB_SEARCH": []},
        disambiguation_notices=[],
        collection_flags=[],
    )


def _make_extracted(vendor_id: str = "v-acme") -> ExtractedAttributes:
    return ExtractedAttributes(
        vendor_id=vendor_id,
        fields={},
        conflicts=[],
        extraction_flags=[],
    )


def _make_relationship(vendor_id: str = "v-acme") -> RelationshipLifecycleResult:
    return RelationshipLifecycleResult(
        relationship_map=RelationshipMap(
            vendor_id=vendor_id,
            parent_company=None,
            subsidiaries=[],
            brands=[],
            former_names=[],
        ),
        lifecycle_signals=[],
        brain_update_suggestions=[],
        flags=[],
    )


def _make_profile_result(
    vendor_id: str = "v-acme",
    profile_status: str = "ENRICHED",
    pcs_before: float = 0.20,
    pcs_after: float = 0.55,
    flags: list[str] | None = None,
    triage_tasks: list[dict] | None = None,
    brain_updates: list[BrainUpdateSuggestion] | None = None,
) -> EnrichedProfileResult:
    return EnrichedProfileResult(
        vendor_id=vendor_id,
        profile_status=profile_status,
        overall_confidence="HIGH",
        profile_path=f"/fake/workspace/p1/{vendor_id}/profile/vendor_profile.md",
        pcs_before=pcs_before,
        pcs_after=pcs_after,
        flags=flags or ["SINGLE_SOURCE_ONLY"],
        triage_tasks=triage_tasks or [{"task_type": "BLOCKING_GAP_RESOLUTION"}],
        brain_update_suggestions=brain_updates or [],
        enriched_at="2024-01-01T00:00:00+00:00",
        error=None,
    )


def _make_step_record(step_id: str, result: dict) -> StepRunRecord:
    return StepRunRecord(
        step_id=step_id,
        status=StepStatus.DONE,
        attempts=1,
        started_at="2024-01-01T00:00:00+00:00",
        completed_at="2024-01-01T00:01:00+00:00",
        result=result,
        error=None,
    )


def _make_state(completed_steps: dict[str, StepRunRecord] | None = None) -> ExecutionState:
    return ExecutionState(
        workflow_id="test-wf",
        programme_id="p1",
        status=ExecutionStatus.IN_PROGRESS,
        current_step_id=None,
        started_at="2024-01-01T00:00:00+00:00",
        last_updated="2024-01-01T00:00:00+00:00",
        completed_steps=completed_steps or {},
        failed_steps={},
        skipped_steps={},
        accumulated_signals={},
        outcome=None,
    )


def _make_outcome(status: str, reason: str | None = None) -> WorkflowOutcome:
    return WorkflowOutcome(
        workflow_id="test-wf",
        status=status,
        final_step_id="s4",
        outcome={"reason": reason} if reason else None,
    )


# ---------------------------------------------------------------------------
# Fixture: mock all 5 tool calls for happy-path tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def mocked_tools(monkeypatch, tmp_path):
    """Patch all external tool calls; let PlanningAgent / RuntimeEngine run real."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    readiness = _make_readiness()
    bundle = _make_bundle()
    extracted = _make_extracted()
    relationship = _make_relationship()
    profile_result = _make_profile_result()

    monkeypatch.setattr(mod, "check_enrichment_readiness", lambda **kw: readiness)
    monkeypatch.setattr(mod, "collect_sources", lambda **kw: bundle)
    monkeypatch.setattr(mod, "extract_attributes", lambda b, kf: extracted)
    monkeypatch.setattr(mod, "map_relationships_and_lifecycle", lambda *a, **kw: relationship)
    monkeypatch.setattr(mod, "create_enriched_profile", lambda **kw: profile_result)

    return {
        "readiness": readiness,
        "bundle": bundle,
        "extracted": extracted,
        "relationship": relationship,
        "profile_result": profile_result,
        "workspace": tmp_path,
    }


# ===========================================================================
# Group 1 — Gating: skip and readiness errors
# ===========================================================================

def test_skip_returns_skipped_status(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        mod,
        "check_enrichment_readiness",
        lambda **kw: _make_readiness(skip=True, skip_reason="recently_enriched"),
    )
    result = run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    assert result.status == "SKIPPED"


def test_skip_preserves_pcs(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        mod,
        "check_enrichment_readiness",
        lambda **kw: _make_readiness(skip=True, skip_reason="pcs_sufficient"),
    )
    result = run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    assert result.pcs_before == result.pcs_after


def test_skip_carries_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        mod,
        "check_enrichment_readiness",
        lambda **kw: _make_readiness(skip=True, skip_reason="recently_enriched"),
    )
    result = run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    assert result.reason == "recently_enriched"


def test_readiness_read_error_returns_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        mod,
        "check_enrichment_readiness",
        lambda **kw: (_ for _ in ()).throw(
            EnrichmentReadinessReadError("malformed yaml")
        ),
    )
    result = run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    assert result.status == "FAILED"


def test_readiness_read_error_sets_error_field(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def _raise(**kw):
        raise EnrichmentReadinessReadError("bad yaml")

    monkeypatch.setattr(mod, "check_enrichment_readiness", _raise)
    result = run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    assert result.error is not None
    assert "bad yaml" in result.error


# ===========================================================================
# Group 2 — Happy path
# ===========================================================================

def test_successful_run_completed(mocked_tools):
    result = run_enrichment("v-acme", "p1", workspace_root=mocked_tools["workspace"])
    assert result.status == "COMPLETED"


def test_result_has_workflow_id(mocked_tools):
    result = run_enrichment("v-acme", "p1", workspace_root=mocked_tools["workspace"])
    assert result.workflow_id is not None
    assert result.workflow_id.startswith("wf-enrich-")


def test_result_profile_status_from_profile(mocked_tools):
    result = run_enrichment("v-acme", "p1", workspace_root=mocked_tools["workspace"])
    assert result.profile_status == mocked_tools["profile_result"].profile_status


def test_result_pcs_after_from_profile(mocked_tools):
    result = run_enrichment("v-acme", "p1", workspace_root=mocked_tools["workspace"])
    assert result.pcs_after == mocked_tools["profile_result"].pcs_after


def test_result_flags_propagated(mocked_tools):
    result = run_enrichment("v-acme", "p1", workspace_root=mocked_tools["workspace"])
    assert result.flags == mocked_tools["profile_result"].flags


def test_result_triage_tasks_propagated(mocked_tools):
    result = run_enrichment("v-acme", "p1", workspace_root=mocked_tools["workspace"])
    assert result.triage_tasks == mocked_tools["profile_result"].triage_tasks


def test_result_brain_update_suggestions(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    brain_update = BrainUpdateSuggestion(
        update_type="REBRAND_MAP",
        from_="OldCo",
        to="NewCo",
        confidence="HIGH",
        source_url="https://example.com",
        suggested_by_vendor_id="v-acme",
    )
    profile_result = _make_profile_result(brain_updates=[brain_update])

    monkeypatch.setattr(mod, "check_enrichment_readiness", lambda **kw: _make_readiness())
    monkeypatch.setattr(mod, "collect_sources", lambda **kw: _make_bundle())
    monkeypatch.setattr(mod, "extract_attributes", lambda b, kf: _make_extracted())
    monkeypatch.setattr(mod, "map_relationships_and_lifecycle", lambda *a, **kw: _make_relationship())
    monkeypatch.setattr(mod, "create_enriched_profile", lambda **kw: profile_result)

    result = run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    assert len(result.brain_update_suggestions) == 1
    assert result.brain_update_suggestions[0].update_type == "REBRAND_MAP"


# ===========================================================================
# Group 3 — Programme files written
# ===========================================================================

def test_enrichment_log_written_on_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        mod,
        "check_enrichment_readiness",
        lambda **kw: _make_readiness(skip=True, skip_reason="pcs_sufficient"),
    )
    run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    log_path = tmp_path / "p1" / "programme_run" / "enrichment_log.md"
    assert log_path.exists()
    assert "SKIPPED" in log_path.read_text(encoding="utf-8")


def test_enrichment_log_written_on_success(mocked_tools):
    run_enrichment("v-acme", "p1", workspace_root=mocked_tools["workspace"])
    log_path = mocked_tools["workspace"] / "p1" / "programme_run" / "enrichment_log.md"
    assert log_path.exists()
    assert "COMPLETED" in log_path.read_text(encoding="utf-8")


def test_triage_queue_written_when_tasks_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    profile_result = _make_profile_result(
        triage_tasks=[{"task_type": "BLOCKING_GAP_RESOLUTION", "field": "category"}]
    )
    monkeypatch.setattr(mod, "check_enrichment_readiness", lambda **kw: _make_readiness())
    monkeypatch.setattr(mod, "collect_sources", lambda **kw: _make_bundle())
    monkeypatch.setattr(mod, "extract_attributes", lambda b, kf: _make_extracted())
    monkeypatch.setattr(mod, "map_relationships_and_lifecycle", lambda *a, **kw: _make_relationship())
    monkeypatch.setattr(mod, "create_enriched_profile", lambda **kw: profile_result)

    run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    triage_path = tmp_path / "p1" / "programme_run" / "triage_queue.md"
    assert triage_path.exists()
    assert "BLOCKING_GAP_RESOLUTION" in triage_path.read_text(encoding="utf-8")


def test_brain_update_queue_written_when_suggestions(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    brain_update = BrainUpdateSuggestion(
        update_type="REBRAND_MAP",
        from_="OldCo",
        to="NewCo",
        confidence="HIGH",
        source_url="https://example.com",
        suggested_by_vendor_id="v-acme",
    )
    profile_result = _make_profile_result(brain_updates=[brain_update], triage_tasks=[])

    monkeypatch.setattr(mod, "check_enrichment_readiness", lambda **kw: _make_readiness())
    monkeypatch.setattr(mod, "collect_sources", lambda **kw: _make_bundle())
    monkeypatch.setattr(mod, "extract_attributes", lambda b, kf: _make_extracted())
    monkeypatch.setattr(mod, "map_relationships_and_lifecycle", lambda *a, **kw: _make_relationship())
    monkeypatch.setattr(mod, "create_enriched_profile", lambda **kw: profile_result)

    run_enrichment("v-acme", "p1", workspace_root=tmp_path)
    queue_path = tmp_path / "p1" / "programme_run" / "brain_update_queue.md"
    assert queue_path.exists()
    assert "REBRAND_MAP" in queue_path.read_text(encoding="utf-8")


# ===========================================================================
# Group 4 — Crash recovery (step function isolation)
# ===========================================================================

def test_crash_recovery_s2_restores_bundle_from_s1_snapshot(tmp_path):
    """When s1 is already done and run_cache is empty, EXTRACT_ATTRIBUTES restores bundle."""
    bundle = _make_bundle("v-crash")
    extracted_calls: list = []

    def fake_extract(b, kf):
        extracted_calls.append(b)
        return _make_extracted("v-crash")

    original_extract = mod.extract_attributes
    mod.extract_attributes = fake_extract  # type: ignore[assignment]

    try:
        run_cache: dict = {}
        readiness = _make_readiness("v-crash")
        registry = _build_enrichment_step_registry(
            readiness=readiness,
            entity_data={"vendor_id": "v-crash", "canonical_name": "CrashCo"},
            pcs_before=0.10,
            workspace_root=tmp_path,
            programme_id="p1",
            vendor_id="v-crash",
            brain=None,
            run_cache=run_cache,
        )

        state = _make_state(
            completed_steps={"s1": _make_step_record("s1", {"bundle_snapshot": bundle.to_dict()})}
        )

        result_dict = registry["EXTRACT_ATTRIBUTES"](None, state, None)

        # Bundle was restored from snapshot into run_cache
        assert "bundle" in run_cache
        assert run_cache["bundle"].vendor_id == "v-crash"
        # extract_attributes was called with the restored bundle
        assert len(extracted_calls) == 1
        assert extracted_calls[0].vendor_id == "v-crash"
        assert "extracted_snapshot" in result_dict
    finally:
        mod.extract_attributes = original_extract  # type: ignore[assignment]


def test_crash_recovery_s3_restores_bundle_and_extracted(tmp_path):
    """When s1,s2 done and run_cache empty, MAP_RELATIONSHIPS restores both."""
    bundle = _make_bundle("v-crash")
    extracted = _make_extracted("v-crash")
    map_calls: list = []

    def fake_map(b, ex, entity_data, brain):
        map_calls.append((b, ex))
        return _make_relationship("v-crash")

    original_map = mod.map_relationships_and_lifecycle
    mod.map_relationships_and_lifecycle = fake_map  # type: ignore[assignment]

    try:
        run_cache: dict = {}
        readiness = _make_readiness("v-crash")
        registry = _build_enrichment_step_registry(
            readiness=readiness,
            entity_data={"vendor_id": "v-crash"},
            pcs_before=0.10,
            workspace_root=tmp_path,
            programme_id="p1",
            vendor_id="v-crash",
            brain=None,
            run_cache=run_cache,
        )

        state = _make_state(
            completed_steps={
                "s1": _make_step_record("s1", {"bundle_snapshot": bundle.to_dict()}),
                "s2": _make_step_record("s2", {"extracted_snapshot": extracted.to_dict()}),
            }
        )

        registry["MAP_RELATIONSHIPS"](None, state, None)

        assert "bundle" in run_cache
        assert len(map_calls) == 1
        # extracted was restored from s2 snapshot
        assert map_calls[0][1].vendor_id == "v-crash"
    finally:
        mod.map_relationships_and_lifecycle = original_map  # type: ignore[assignment]


def test_crash_recovery_s4_restores_extracted_and_relationship(tmp_path):
    """When s2,s3 done and run_cache empty, CREATE_PROFILE restores both."""
    bundle = _make_bundle("v-crash")
    extracted = _make_extracted("v-crash")
    relationship = _make_relationship("v-crash")
    profile_calls: list = []

    def fake_create(**kw):
        profile_calls.append(kw)
        return _make_profile_result("v-crash")

    original_create = mod.create_enriched_profile
    mod.create_enriched_profile = fake_create  # type: ignore[assignment]

    try:
        run_cache: dict = {}
        readiness = _make_readiness("v-crash")
        registry = _build_enrichment_step_registry(
            readiness=readiness,
            entity_data={"vendor_id": "v-crash"},
            pcs_before=0.10,
            workspace_root=tmp_path,
            programme_id="p1",
            vendor_id="v-crash",
            brain=None,
            run_cache=run_cache,
        )

        state = _make_state(
            completed_steps={
                "s1": _make_step_record("s1", {"bundle_snapshot": bundle.to_dict()}),
                "s2": _make_step_record("s2", {"extracted_snapshot": extracted.to_dict()}),
                "s3": _make_step_record("s3", {"relationship_snapshot": relationship.to_dict()}),
            }
        )

        registry["CREATE_PROFILE"](None, state, None)

        assert len(profile_calls) == 1
        assert profile_calls[0]["extracted"].vendor_id == "v-crash"
        assert profile_calls[0]["relationship_result"].relationship_map.vendor_id == "v-crash"
        assert "profile_result" in run_cache
    finally:
        mod.create_enriched_profile = original_create  # type: ignore[assignment]


# ===========================================================================
# Group 5 — _assemble_result unit tests
# ===========================================================================

def test_partial_status_when_profile_not_built():
    """COMPLETED outcome with no profile_result in run_cache → PARTIAL."""
    outcome = _make_outcome("COMPLETED")
    result = _assemble_result("v-acme", "wf-1", outcome, 0.20, run_cache={})
    assert result.status == "PARTIAL"
    assert result.pcs_before == result.pcs_after == 0.20


def test_assemble_blocked_status():
    outcome = _make_outcome("BLOCKED", reason="deadlock_detected")
    result = _assemble_result("v-acme", "wf-1", outcome, 0.10, run_cache={})
    assert result.status == "BLOCKED"
    assert result.reason == "deadlock_detected"


def test_assemble_failed_status():
    outcome = _make_outcome("FAILED", reason="step_fatal_error")
    result = _assemble_result("v-acme", "wf-1", outcome, 0.10, run_cache={})
    assert result.status == "FAILED"


# ===========================================================================
# Group 6 — Registry and utility helpers
# ===========================================================================

def test_step_registry_has_four_keys(tmp_path):
    readiness = _make_readiness()
    registry = _build_enrichment_step_registry(
        readiness=readiness,
        entity_data={},
        pcs_before=0.0,
        workspace_root=tmp_path,
        programme_id="p1",
        vendor_id="v-acme",
        brain=None,
        run_cache={},
    )
    assert set(registry.keys()) == {
        "COLLECT_SOURCES",
        "EXTRACT_ATTRIBUTES",
        "MAP_RELATIONSHIPS",
        "CREATE_PROFILE",
    }


def test_read_entity_md_returns_empty_dict_when_missing(tmp_path):
    result = _read_entity_md("p-none", "v-none", tmp_path)
    assert result == {}


def test_read_pcs_from_coverage_returns_zero_when_missing(tmp_path):
    pcs = _read_pcs_from_coverage("p-none", "v-none", tmp_path)
    assert pcs == 0.0
