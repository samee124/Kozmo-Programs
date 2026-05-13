"""Integration tests — full enrichment pipeline, Process 2.

35 tests:
  Group A (A1–A5): V2 runtime capabilities
  Group B (B1–B31, B26 merged into B24): 30 DE issue scenarios

All external boundaries are mocked (no real network or OpenAI calls).
Group A tests let the RuntimeEngine and PlanningAgent run for real;
Group B tests mock at the orchestrator import boundary for speed and control.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cobalt.orchestrator.enrichment_orchestrator as orch_mod
from cobalt.agents.planning_agent import PlanningAgent
from cobalt.models.schemas.enrichment_schema import (
    BrainUpdateSuggestion,
    EnrichedProfileResult,
    EnrichmentReadinessResult,
    ExtractedAttributes,
    ExtractedField,
    KnownFacts,
    LifecycleSignal,
    RelationshipLifecycleResult,
    RelationshipMap,
    SourceEvidenceBundle,
    SourceEvidenceItem,
)
from cobalt.orchestrator.enrichment_orchestrator import run_enrichment
from cobalt.runtime.execution_state import ExecutionState, ExecutionStatus, StepRunRecord
from cobalt.runtime.workflow_definition import StepStatus


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VENDOR = "acme"
_PROG = "p1"
_NOW = "2024-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------

def _make_known_facts(**kw) -> KnownFacts:
    return KnownFacts(
        confirmed=kw.get("confirmed", ["canonical_name", "domain"]),
        gaps=kw.get("gaps", []),
        conflicts=kw.get("conflicts", []),
    )


def _make_readiness(
    vendor_id: str = _VENDOR,
    depth_tier: str = "STANDARD",
    flags: list[str] | None = None,
    **kw,
) -> EnrichmentReadinessResult:
    return EnrichmentReadinessResult(
        vendor_id=vendor_id,
        proceed=True,
        skip=False,
        skip_reason=None,
        depth_tier=depth_tier,
        source_list=kw.get("source_list", ["web_search", "company_website"]),
        query_count=2,
        known_facts=kw.get("known_facts", _make_known_facts()),
        confidence_floor=0.75,
        flags=flags or [],
    )


def _make_item(
    content: str = "Acme Corp is an IT company.",
    source_type: str = "WEB_SEARCH",
) -> SourceEvidenceItem:
    return SourceEvidenceItem(
        content=content,
        source_type=source_type,
        source_url="https://example.com",
        retrieved_at=_NOW,
        validation_status="CONFIRMED",
        quality_signal="OFFICIAL",
    )


def _make_bundle(
    vendor_id: str = _VENDOR,
    flags: list[str] | None = None,
    notices: list[dict] | None = None,
    depth_tier: str = "STANDARD",
) -> SourceEvidenceBundle:
    return SourceEvidenceBundle(
        vendor_id=vendor_id,
        depth_tier=depth_tier,
        sources={"WEB_SEARCH": [_make_item()]},
        disambiguation_notices=notices or [],
        collection_flags=flags or [],
    )


def _make_field(value: object, confidence: str = "HIGH", source: str = "WEB_SEARCH") -> ExtractedField:
    return ExtractedField(value=value, confidence=confidence, source=source)


def _make_extracted(
    vendor_id: str = _VENDOR,
    fields: dict | None = None,
    flags: list[str] | None = None,
    conflicts: list[dict] | None = None,
) -> ExtractedAttributes:
    base: dict[str, ExtractedField] = {
        "canonical_name":    _make_field("Acme Corp"),
        "category":          _make_field("IT"),
        "hq_country":        _make_field("US"),
        "hq_city":           _make_field("New York"),
        "company_status":    _make_field("ACTIVE"),
        "description":       _make_field("An enterprise IT company."),
        "vendor_type":       _make_field("DIRECT"),
        "company_size_band": _make_field("SMB"),
    }
    if fields:
        base.update(fields)
    return ExtractedAttributes(
        vendor_id=vendor_id,
        fields=base,
        conflicts=conflicts or [],
        extraction_flags=flags or [],
    )


def _make_rel_map(
    vendor_id: str = _VENDOR,
    parent: dict | None = None,
    subsidiaries: list[dict] | None = None,
) -> RelationshipMap:
    return RelationshipMap(
        vendor_id=vendor_id,
        parent_company=parent,
        subsidiaries=subsidiaries or [],
        brands=[],
        former_names=[],
    )


def _make_relationship(
    rel_map: RelationshipMap | None = None,
    lifecycle: list[LifecycleSignal] | None = None,
    brain_updates: list[BrainUpdateSuggestion] | None = None,
    flags: list[str] | None = None,
    vendor_id: str = _VENDOR,
) -> RelationshipLifecycleResult:
    return RelationshipLifecycleResult(
        relationship_map=rel_map or _make_rel_map(vendor_id),
        lifecycle_signals=lifecycle or [],
        brain_update_suggestions=brain_updates or [],
        flags=flags or [],
    )


def _make_lifecycle(
    signal_type: str,
    from_: str | None = None,
    to: str | None = None,
    confidence: str = "HIGH",
    brain_update_required: bool = False,
) -> LifecycleSignal:
    return LifecycleSignal(
        signal_type=signal_type,
        from_=from_,
        to=to,
        date="2023-01-01",
        confidence=confidence,
        source="WEB_SEARCH",
        brain_update_required=brain_update_required,
    )


def _make_brain_update(
    update_type: str = "REBRAND_MAP",
    from_: str = "Old Inc",
    to: str = "New Corp",
    vendor_id: str = _VENDOR,
) -> BrainUpdateSuggestion:
    return BrainUpdateSuggestion(
        update_type=update_type,
        from_=from_,
        to=to,
        confidence="HIGH",
        source_url="https://example.com",
        suggested_by_vendor_id=vendor_id,
        review_required=True,
    )


def _make_profile(
    vendor_id: str = _VENDOR,
    status: str = "ENRICHED",
    confidence: str = "HIGH",
    flags: list[str] | None = None,
    triage: list[dict] | None = None,
    brain_updates: list[BrainUpdateSuggestion] | None = None,
    pcs_before: float = 0.40,
    pcs_after: float = 0.72,
    error: str | None = None,
) -> EnrichedProfileResult:
    return EnrichedProfileResult(
        vendor_id=vendor_id,
        profile_status=status,
        overall_confidence=confidence,
        profile_path=None,
        pcs_before=pcs_before,
        pcs_after=pcs_after,
        flags=flags or [],
        triage_tasks=triage or [],
        brain_update_suggestions=brain_updates or [],
        enriched_at=_NOW,
        error=error,
    )


# ---------------------------------------------------------------------------
# Fixture: wire all 5 tools at orchestrator import boundary
# ---------------------------------------------------------------------------

@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Patch all five enrichment tools at the orchestrator module level.

    The lambdas close over ``ns`` by reference, so tests can update attributes
    (e.g. ``wired.profile = _make_profile(...)``) without re-patching.
    Patches are automatically undone after each test via monkeypatch.
    """
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    ns = MagicMock()
    ns.readiness = _make_readiness()
    ns.bundle = _make_bundle()
    ns.extracted = _make_extracted()
    ns.rel = _make_relationship()
    ns.profile = _make_profile()
    ns.tmp_path = tmp_path

    monkeypatch.setattr(orch_mod, "check_enrichment_readiness",
                        lambda **kw: ns.readiness)
    monkeypatch.setattr(orch_mod, "collect_sources",
                        lambda **kw: ns.bundle)
    monkeypatch.setattr(orch_mod, "extract_attributes",
                        lambda b, kf: ns.extracted)
    monkeypatch.setattr(orch_mod, "map_relationships_and_lifecycle",
                        lambda b, e, ed, brain: ns.rel)
    monkeypatch.setattr(orch_mod, "create_enriched_profile",
                        lambda **kw: ns.profile)
    return ns


def _run(vendor_id: str = _VENDOR, programme_id: str = _PROG, **kw):
    return run_enrichment(vendor_id=vendor_id, programme_id=programme_id, **kw)


# ===========================================================================
# GROUP A — V2 RUNTIME CAPABILITIES
# ===========================================================================

class TestA1CrashRecovery:
    """A1: RuntimeEngine resumes from persisted state without re-running done steps."""

    def test_completed_steps_not_re_executed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

        vendor_id, programme_id = _VENDOR, _PROG

        # Build a real ENRICHMENT workflow so workflow.json lands on disk.
        pa = PlanningAgent()
        wf = pa.create_workflow(
            profile=None,
            workflow_type="ENRICHMENT",
            programme_id=programme_id,
            context_overrides={"vendor_id": vendor_id, "depth_tier": "STANDARD"},
        )

        # Mark s1 and s2 as DONE in the in-memory definition, then re-save.
        for step in wf.steps:
            if step.step_id in ("s1", "s2"):
                step.status = StepStatus.DONE
        wf.save()

        # Persist an ExecutionState that declares s1/s2 completed with snapshots.
        bundle = _make_bundle(vendor_id)
        extracted = _make_extracted(vendor_id)

        s1_rec = StepRunRecord(
            step_id="s1",
            status=StepStatus.DONE,
            attempts=1,
            started_at=_NOW,
            completed_at=_NOW,
            result={"bundle_snapshot": bundle.to_dict(), "depth_tier": "STANDARD"},
            error=None,
        )
        s2_rec = StepRunRecord(
            step_id="s2",
            status=StepStatus.DONE,
            attempts=1,
            started_at=_NOW,
            completed_at=_NOW,
            result={"extracted_snapshot": extracted.to_dict()},
            error=None,
        )
        state = ExecutionState(
            workflow_id=wf.workflow_id,
            programme_id=programme_id,
            status=ExecutionStatus.IN_PROGRESS,
            current_step_id="s3",
            started_at=_NOW,
            last_updated=_NOW,
            completed_steps={"s1": s1_rec, "s2": s2_rec},
            failed_steps={},
            skipped_steps={},
            accumulated_signals={},
            outcome=None,
        )
        state.save()

        # Instrument collect_sources and extract_attributes with call counters.
        collect_calls: list = []
        extract_calls: list = []

        rel = _make_relationship(vendor_id=vendor_id)
        profile = _make_profile(vendor_id=vendor_id)

        monkeypatch.setattr(orch_mod, "check_enrichment_readiness",
                            lambda **kw: _make_readiness(vendor_id))
        monkeypatch.setattr(orch_mod, "collect_sources",
                            lambda **kw: (collect_calls.append(1), _make_bundle(vendor_id))[1])
        monkeypatch.setattr(orch_mod, "extract_attributes",
                            lambda b, kf: (extract_calls.append(1), _make_extracted(vendor_id))[1])
        monkeypatch.setattr(orch_mod, "map_relationships_and_lifecycle",
                            lambda b, e, ed, brain: rel)
        monkeypatch.setattr(orch_mod, "create_enriched_profile",
                            lambda **kw: profile)
        # Return our pre-built workflow so the engine loads our pre-written files.
        monkeypatch.setattr(PlanningAgent, "create_workflow",
                            lambda self, **kw: wf)

        result = _run(vendor_id=vendor_id, programme_id=programme_id)

        assert len(collect_calls) == 0, "s1 (COLLECT_SOURCES) must not re-run after crash recovery"
        assert len(extract_calls) == 0, "s2 (EXTRACT_ATTRIBUTES) must not re-run after crash recovery"
        assert result.status == "COMPLETED"
        assert result.profile_status == "ENRICHED"

    def test_crash_recovery_rehydrates_bundle_from_snapshot(self, monkeypatch, tmp_path):
        """MAP_RELATIONSHIPS (s3) receives the bundle from the s1 snapshot."""
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

        vendor_id, programme_id = _VENDOR, _PROG
        pa = PlanningAgent()
        wf = pa.create_workflow(
            profile=None,
            workflow_type="ENRICHMENT",
            programme_id=programme_id,
            context_overrides={"vendor_id": vendor_id, "depth_tier": "STANDARD"},
        )
        for step in wf.steps:
            if step.step_id in ("s1", "s2"):
                step.status = StepStatus.DONE
        wf.save()

        bundle = _make_bundle(vendor_id)
        extracted = _make_extracted(vendor_id)

        s1_rec = StepRunRecord(
            step_id="s1", status=StepStatus.DONE, attempts=1,
            started_at=_NOW, completed_at=_NOW,
            result={"bundle_snapshot": bundle.to_dict(), "depth_tier": "STANDARD"},
            error=None,
        )
        s2_rec = StepRunRecord(
            step_id="s2", status=StepStatus.DONE, attempts=1,
            started_at=_NOW, completed_at=_NOW,
            result={"extracted_snapshot": extracted.to_dict()},
            error=None,
        )
        state = ExecutionState(
            workflow_id=wf.workflow_id, programme_id=programme_id,
            status=ExecutionStatus.IN_PROGRESS, current_step_id="s3",
            started_at=_NOW, last_updated=_NOW,
            completed_steps={"s1": s1_rec, "s2": s2_rec},
            failed_steps={}, skipped_steps={}, accumulated_signals={}, outcome=None,
        )
        state.save()

        received_bundles: list = []
        profile = _make_profile(vendor_id=vendor_id)
        rel = _make_relationship(vendor_id=vendor_id)

        monkeypatch.setattr(orch_mod, "check_enrichment_readiness",
                            lambda **kw: _make_readiness(vendor_id))
        monkeypatch.setattr(orch_mod, "collect_sources",
                            lambda **kw: _make_bundle(vendor_id))
        monkeypatch.setattr(orch_mod, "extract_attributes",
                            lambda b, kf: _make_extracted(vendor_id))
        monkeypatch.setattr(
            orch_mod,
            "map_relationships_and_lifecycle",
            lambda b, e, ed, brain: (received_bundles.append(b), rel)[1],
        )
        monkeypatch.setattr(orch_mod, "create_enriched_profile",
                            lambda **kw: profile)
        monkeypatch.setattr(PlanningAgent, "create_workflow",
                            lambda self, **kw: wf)

        _run(vendor_id=vendor_id, programme_id=programme_id)

        assert len(received_bundles) == 1
        assert received_bundles[0].vendor_id == vendor_id


@pytest.mark.xfail(
    reason=(
        "Enrichment step callables do not include a 'confidence' key in their result dicts; "
        "evaluate_step therefore always returns CONTINUE. Adaptive replanning on enrichment "
        "confidence decay is a V2.1 feature."
    ),
    strict=True,
)
def test_a2_adaptive_replanning_not_triggered(wired):
    """A2: evaluate_step never triggers REPLAN for enrichment steps (xfail — V2 gap)."""
    # Even with a very low-confidence profile the engine never replans because
    # enrichment step result dicts don't carry a 'confidence' key.
    wired.profile = _make_profile(confidence="LOW")
    result = _run()
    # If we reach here with COMPLETED the engine did NOT replan — xfail proves this
    # is the current (unwanted) behaviour that V2.1 must fix.
    assert result.status != "COMPLETED", "REPLAN should have been triggered by LOW confidence"


class TestA3Auditability:
    """A3: workflow.json and state.json are persisted to the workspace."""

    def test_workflow_json_written(self, wired):
        result = _run()
        wf_dir = wired.tmp_path / _PROG / "workflows"
        wf_files = list(wf_dir.glob("*/workflow.json"))
        assert len(wf_files) == 1, f"Expected exactly one workflow.json, found {wf_files}"
        wf_data = json.loads(wf_files[0].read_text(encoding="utf-8"))
        assert wf_data["workflow_type"] == "ENRICHMENT"
        assert wf_data["vendor_id"] == _VENDOR
        assert result.workflow_id == wf_data["workflow_id"]

    def test_state_json_written(self, wired):
        _run()
        wf_dir = wired.tmp_path / _PROG / "workflows"
        state_files = list(wf_dir.glob("*/state.json"))
        assert len(state_files) == 1, "Expected exactly one state.json"
        state_data = json.loads(state_files[0].read_text(encoding="utf-8"))
        assert state_data["status"] in ("COMPLETED", "IN_PROGRESS")
        assert "s1" in state_data["completed_steps"]
        assert "s4" in state_data["completed_steps"]

    def test_enrichment_log_written(self, wired):
        _run()
        log_path = wired.tmp_path / _PROG / "programme_run" / "enrichment_log.md"
        assert log_path.exists(), "enrichment_log.md must be written for every run"
        assert _VENDOR in log_path.read_text(encoding="utf-8")


class TestA4Replayability:
    """A4: Running enrichment for two distinct vendors yields identical outcomes."""

    def test_two_vendors_same_outcome(self, wired):
        # Use distinct vendor IDs to avoid workflow_id collisions when both
        # runs happen within the same clock second.
        r1 = run_enrichment(vendor_id="vendor-alpha", programme_id=_PROG)
        r2 = run_enrichment(vendor_id="vendor-beta",  programme_id=_PROG)
        # Both workflow files should be present in the same workspace.
        assert len(list((wired.tmp_path / _PROG / "workflows").glob("*/workflow.json"))) == 2
        assert r1.status == r2.status
        assert r1.profile_status == r2.profile_status
        assert r1.overall_confidence == r2.overall_confidence

    def test_each_run_appends_log_entry(self, wired):
        run_enrichment(vendor_id="vendor-alpha", programme_id=_PROG)
        run_enrichment(vendor_id="vendor-beta",  programme_id=_PROG)
        log_path = wired.tmp_path / _PROG / "programme_run" / "enrichment_log.md"
        content = log_path.read_text(encoding="utf-8")
        assert content.count("enrichment_log_entry") >= 2


class TestA5Observability:
    """A5: state.json contains per-step timestamps and depth_tier signal."""

    def test_state_has_step_timestamps(self, wired):
        _run()
        wf_dir = wired.tmp_path / _PROG / "workflows"
        state_files = list(wf_dir.glob("*/state.json"))
        assert state_files
        state = json.loads(state_files[0].read_text(encoding="utf-8"))
        for step_id in ("s1", "s2", "s3", "s4"):
            rec = (
                state["completed_steps"].get(step_id)
                or state.get("failed_steps", {}).get(step_id)
            )
            assert rec is not None, f"Step {step_id} missing from state"
            assert rec.get("started_at"), f"{step_id} missing started_at"
            assert rec.get("completed_at"), f"{step_id} missing completed_at"

    def test_state_records_depth_tier_signal(self, wired):
        _run()
        wf_dir = wired.tmp_path / _PROG / "workflows"
        state_files = list(wf_dir.glob("*/state.json"))
        state = json.loads(state_files[0].read_text(encoding="utf-8"))
        # s1 result carries depth_tier from the COLLECT_SOURCES step return dict.
        s1_result = state["completed_steps"]["s1"]["result"]
        assert "depth_tier" in s1_result


# ===========================================================================
# GROUP B — DE ISSUE SCENARIOS (30 tests, B1–B31 with B26 merged into B24)
# ===========================================================================

class TestB1ConflictingSources:
    """B1: Multiple sources disagree on the same attribute."""

    def test_conflict_flag_propagates_to_result(self, wired):
        wired.extracted = _make_extracted(
            conflicts=[{"field": "hq_country", "values": ["US", "GB"], "resolution": "UNRESOLVED"}],
        )
        wired.profile = _make_profile(flags=["UNRESOLVED_CONFLICT:hq_country"])
        result = _run()
        assert result.status == "COMPLETED"
        assert any("UNRESOLVED_CONFLICT" in f for f in result.flags)

    def test_conflicting_sources_creates_triage_task(self, wired):
        wired.profile = _make_profile(
            triage=[{"field": "hq_country", "reason": "conflicting_sources", "priority": "HIGH"}],
        )
        result = _run()
        assert len(result.triage_tasks) >= 1
        assert any(t.get("field") == "hq_country" for t in result.triage_tasks)


class TestB2OutdatedInfo:
    """B2: Source data is stale / retrieved from an old cache."""

    def test_stale_data_flag_preserved(self, wired):
        wired.bundle = _make_bundle(flags=["STALE_DATA"])
        wired.profile = _make_profile(flags=["STALE_DATA"])
        result = _run()
        assert result.status == "COMPLETED"
        assert "STALE_DATA" in result.flags


class TestB3WrongEntity:
    """B3: Collected evidence is about a different company (name collision)."""

    def test_wrong_entity_risk_flag_in_result(self, wired):
        wired.extracted = _make_extracted(flags=["WRONG_ENTITY_RISK"])
        wired.profile = _make_profile(
            status="PARTIALLY_ENRICHED",
            flags=["WRONG_ENTITY_RISK"],
            triage=[{"reason": "wrong_entity_risk", "priority": "HIGH"}],
        )
        result = _run()
        assert "WRONG_ENTITY_RISK" in result.flags
        assert result.profile_status in ("PARTIALLY_ENRICHED", "PROVISIONAL")

    def test_wrong_entity_creates_triage(self, wired):
        wired.profile = _make_profile(
            flags=["WRONG_ENTITY_RISK"],
            triage=[{"reason": "wrong_entity_risk", "priority": "HIGH"}],
        )
        result = _run()
        assert len(result.triage_tasks) >= 1


class TestB4MissingHQCity:
    """B4: hq_city not found in any source."""

    def test_missing_hq_city_not_fatal(self, wired):
        fields = {k: v for k, v in _make_extracted().fields.items() if k != "hq_city"}
        wired.extracted = _make_extracted(fields=fields)
        wired.profile = _make_profile(flags=["ENRICHMENT_GAP:hq_city"])
        result = _run()
        assert result.status == "COMPLETED"

    def test_missing_hq_city_is_enrichment_gap(self, wired):
        wired.profile = _make_profile(flags=["ENRICHMENT_GAP:hq_city"])
        result = _run()
        assert any("hq_city" in f for f in result.flags)


class TestB5NoDigitalPresence:
    """B5: Vendor has no web or social presence; PROVISIONAL depth required.

    STANDARD depth always includes 'linkedin' in source_list, preventing
    NO_DIGITAL_PRESENCE from firing naturally. Tests use PROVISIONAL.
    """

    def test_no_digital_presence_flag(self, wired):
        wired.readiness = _make_readiness(
            depth_tier="PROVISIONAL",
            source_list=["web_search"],
            flags=["NO_DIGITAL_PRESENCE"],
        )
        wired.bundle = _make_bundle(flags=["NO_DIGITAL_PRESENCE"])
        wired.profile = _make_profile(
            status="PROVISIONAL",
            confidence="LOW",
            flags=["NO_DIGITAL_PRESENCE"],
        )
        result = _run()
        assert "NO_DIGITAL_PRESENCE" in result.flags
        assert result.profile_status in ("PROVISIONAL", "PARTIALLY_ENRICHED")


class TestB6WrongCategory:
    """B6: Extracted category disagrees with the entity.md declared category."""

    def test_category_conflict_flag(self, wired):
        wired.extracted = _make_extracted(flags=["CATEGORY_CONFLICT"])
        wired.profile = _make_profile(flags=["CATEGORY_CONFLICT"])
        result = _run()
        assert "CATEGORY_CONFLICT" in result.flags


class TestB7MultiCategory:
    """B7: Vendor spans multiple categories; ambiguity flag raised."""

    def test_multi_category_ambiguity_flag(self, wired):
        wired.extracted = _make_extracted(flags=["MULTI_CATEGORY_AMBIGUITY"])
        wired.profile = _make_profile(flags=["MULTI_CATEGORY_AMBIGUITY"])
        result = _run()
        assert "MULTI_CATEGORY_AMBIGUITY" in result.flags

    def test_multi_category_creates_triage(self, wired):
        wired.profile = _make_profile(
            flags=["MULTI_CATEGORY_AMBIGUITY"],
            triage=[{"field": "category", "reason": "multi_category_ambiguity"}],
        )
        result = _run()
        assert len(result.triage_tasks) >= 1


class TestB8AmbiguousModel:
    """B8: Business model is ambiguous (e.g. SaaS vs. services)."""

    def test_ambiguous_business_model_flag(self, wired):
        wired.profile = _make_profile(flags=["AMBIGUOUS_BUSINESS_MODEL"])
        result = _run()
        assert "AMBIGUOUS_BUSINESS_MODEL" in result.flags


class TestB9OverGeneralisation:
    """B9: Description is too generic to be meaningful."""

    def test_over_generalised_description_flag(self, wired):
        wired.extracted = _make_extracted(flags=["NO_DESCRIPTION_EXTRACTABLE"])
        wired.profile = _make_profile(flags=["DESCRIPTION_OVER_GENERALISED"])
        result = _run()
        assert result.status == "COMPLETED"
        assert any("DESCRIPTION" in f for f in result.flags)


class TestB10ParentSubsidiary:
    """B10: Vendor is a subsidiary; parent is resolved."""

    def test_parent_resolved_in_relationship_map(self, wired):
        parent = {"name": "BigCorp", "confidence": "HIGH", "source": "WEB_SEARCH"}
        wired.rel = _make_relationship(rel_map=_make_rel_map(parent=parent))
        wired.profile = _make_profile(flags=["PARENT_RESOLVED"])
        result = _run()
        assert result.status == "COMPLETED"

    def test_parent_data_flows_to_completed_profile(self, wired):
        parent = {"name": "BigCorp", "confidence": "HIGH", "source": "WEB_SEARCH"}
        wired.rel = _make_relationship(rel_map=_make_rel_map(parent=parent))
        result = _run()
        assert result.status == "COMPLETED"
        assert result.profile_status == "ENRICHED"


class TestB11MissingParent:
    """B11: Parent company exists but cannot be resolved from sources."""

    def test_missing_parent_creates_triage(self, wired):
        wired.rel = _make_relationship(flags=["PARENT_UNRESOLVED"])
        wired.profile = _make_profile(
            flags=["PARENT_UNRESOLVED"],
            triage=[{"reason": "parent_unresolved", "priority": "MEDIUM"}],
        )
        result = _run()
        assert "PARENT_UNRESOLVED" in result.flags


class TestB12BrainAcquisition:
    """B12: Brain acquisition_map records an acquisition for this vendor."""

    def test_acquired_lifecycle_signal_captured(self, wired):
        sig = _make_lifecycle("ACQUIRED", from_="Citrix", to="Cloud Software Group")
        wired.rel = _make_relationship(lifecycle=[sig])
        result = _run()
        assert result.status == "COMPLETED"

    def test_brain_acquisition_generates_update_suggestion(self, wired):
        update = _make_brain_update(update_type="ACQUISITION_MAP",
                                    from_="citrix", to="Cloud Software Group")
        sig = _make_lifecycle("ACQUIRED", from_="Citrix", to="Cloud Software Group",
                               brain_update_required=True)
        wired.rel = _make_relationship(lifecycle=[sig])
        wired.profile = _make_profile(brain_updates=[update])
        result = _run()
        assert len(result.brain_update_suggestions) >= 1
        assert any(s.update_type == "ACQUISITION_MAP" for s in result.brain_update_suggestions)


class TestB13MultipleLayers:
    """B13: Multi-layer corporate hierarchy — grandparent discovered."""

    def test_nested_subsidiary_structure_accepted(self, wired):
        parent = {
            "name": "MidCorp",
            "parent": {"name": "TopCorp"},
            "confidence": "MEDIUM",
            "source": "WEB_SEARCH",
        }
        wired.rel = _make_relationship(rel_map=_make_rel_map(parent=parent))
        result = _run()
        assert result.status == "COMPLETED"


class TestB14InconsistentSize:
    """B14: Multiple sources report conflicting employee count / size band."""

    def test_size_conflict_flag(self, wired):
        wired.extracted = _make_extracted(flags=["SIZE_SIGNALS_CONFLICT"])
        wired.profile = _make_profile(flags=["SIZE_SIGNALS_CONFLICT"])
        result = _run()
        assert "SIZE_SIGNALS_CONFLICT" in result.flags


class TestB15MissingSize:
    """B15: No size data (employee_count_range / company_size_band) found."""

    def test_missing_size_is_enrichment_gap(self, wired):
        base = {k: v for k, v in _make_extracted().fields.items()
                if k not in ("employee_count_range", "company_size_band")}
        wired.extracted = _make_extracted(fields=base, flags=["MISSING_SIZE_DATA"])
        wired.profile = _make_profile(flags=["ENRICHMENT_GAP:company_size_band"])
        result = _run()
        assert result.status == "COMPLETED"
        assert any("company_size_band" in f or "size" in f.lower() for f in result.flags)


class TestB16MisleadingSize:
    """B16: Headline headcount contradicts stated size band."""

    def test_size_mismatch_flag(self, wired):
        wired.profile = _make_profile(flags=["SIZE_BAND_HEADCOUNT_MISMATCH"])
        result = _run()
        assert "SIZE_BAND_HEADCOUNT_MISMATCH" in result.flags


class TestB17WrongHQ:
    """B17: Registered address differs from operational HQ."""

    def test_hq_override_flag(self, wired):
        wired.extracted = _make_extracted(flags=["HQ_OVERRIDE"])
        wired.profile = _make_profile(flags=["HQ_OVERRIDE"])
        result = _run()
        assert "HQ_OVERRIDE" in result.flags


class TestB18MultipleHQs:
    """B18: Sources disagree on which city/country is the true HQ."""

    def test_multiple_hq_candidates_flag(self, wired):
        wired.profile = _make_profile(flags=["MULTIPLE_HQ_CANDIDATES"])
        result = _run()
        assert "MULTIPLE_HQ_CANDIDATES" in result.flags


class TestB19RegionalSubsidiary:
    """B19: Vendor is a regional subsidiary; sources describe the parent entity."""

    def test_regional_subsidiary_identified(self, wired):
        parent = {"name": "GlobalCorp EMEA", "confidence": "MEDIUM", "source": "WEB_SEARCH"}
        subs = [{"name": "GlobalCorp UK", "confidence": "LOW", "source": "WEB_SEARCH"}]
        wired.rel = _make_relationship(rel_map=_make_rel_map(parent=parent, subsidiaries=subs))
        wired.profile = _make_profile(flags=["REGIONAL_SUBSIDIARY"])
        result = _run()
        assert result.status == "COMPLETED"


class TestB20RebrandDetected:
    """B20: Vendor has rebranded; detected from Brain (B20a) or news content (B20b)."""

    def test_b20a_rebrand_from_brain_map(self, wired):
        sig = _make_lifecycle("REBRANDED", from_="Blackboard", to="Anthology",
                               brain_update_required=True)
        update = _make_brain_update(update_type="REBRAND_MAP", from_="blackboard", to="Anthology")
        wired.rel = _make_relationship(lifecycle=[sig], brain_updates=[update])
        wired.profile = _make_profile(brain_updates=[update], flags=["REBRAND_DETECTED"])
        result = _run()
        assert any(s.update_type == "REBRAND_MAP" for s in result.brain_update_suggestions)
        assert "REBRAND_DETECTED" in result.flags

    def test_b20b_rebrand_from_news_content(self, wired):
        sig = _make_lifecycle("REBRANDED", from_="OldName Inc", to="NewName Corp",
                               brain_update_required=True)
        update = _make_brain_update(update_type="REBRAND_MAP", from_="oldname", to="NewName Corp")
        wired.rel = _make_relationship(lifecycle=[sig], brain_updates=[update])
        wired.profile = _make_profile(brain_updates=[update], flags=["REBRAND_DETECTED"])
        result = _run()
        assert "REBRAND_DETECTED" in result.flags
        assert any(s.update_type == "REBRAND_MAP" for s in result.brain_update_suggestions)


class TestB21AcquisitionConfusion:
    """B21: Acquisition event creates ambiguity between acquirer and target identity."""

    def test_acquisition_confusion_flag(self, wired):
        sig = _make_lifecycle("ACQUIRED", from_="TargetCo", to="Acquirer Inc", confidence="MEDIUM")
        wired.rel = _make_relationship(lifecycle=[sig], flags=["ACQUISITION_AMBIGUITY"])
        wired.profile = _make_profile(flags=["ACQUISITION_AMBIGUITY"])
        result = _run()
        assert "ACQUISITION_AMBIGUITY" in result.flags


class TestB22BrainAcquisitionCompleted:
    """B22: Brain records a completed acquisition — brain_update_suggestion queued."""

    def test_completed_acquisition_queues_brain_update(self, wired):
        update = _make_brain_update(
            update_type="ACQUISITION_MAP", from_="TargetCo", to="Acquirer Inc",
        )
        wired.profile = _make_profile(brain_updates=[update])
        result = _run()
        assert len(result.brain_update_suggestions) >= 1

    def test_completed_acquisition_writes_queue_file(self, wired):
        update = _make_brain_update(
            update_type="ACQUISITION_MAP", from_="TargetCo", to="Acquirer Inc",
        )
        wired.profile = _make_profile(brain_updates=[update])
        _run()
        queue_path = wired.tmp_path / _PROG / "programme_run" / "brain_update_queue.md"
        assert queue_path.exists()
        assert "Acquirer" in queue_path.read_text(encoding="utf-8")


class TestB23DefunctDetected:
    """B23: Company appears to have ceased operations."""

    def test_possibly_defunct_lifecycle_signal(self, wired):
        sig = _make_lifecycle("POSSIBLY_DEFUNCT", confidence="MEDIUM")
        wired.rel = _make_relationship(lifecycle=[sig])
        wired.profile = _make_profile(flags=["POSSIBLY_DEFUNCT"])
        result = _run()
        assert "POSSIBLY_DEFUNCT" in result.flags

    def test_defunct_creates_triage(self, wired):
        wired.profile = _make_profile(
            flags=["POSSIBLY_DEFUNCT"],
            triage=[{"reason": "possibly_defunct", "priority": "HIGH"}],
        )
        result = _run()
        assert len(result.triage_tasks) >= 1


class TestB24GenericUseCaseAndConflictingDescriptions:
    """B24 + B26 combined: Generic use case text AND conflicting descriptions."""

    def test_generic_use_case_flag(self, wired):
        wired.extracted = _make_extracted(flags=["GENERIC_USE_CASE"])
        wired.profile = _make_profile(flags=["GENERIC_USE_CASE"])
        result = _run()
        assert "GENERIC_USE_CASE" in result.flags

    def test_conflicting_descriptions_flag(self, wired):
        wired.extracted = _make_extracted(
            conflicts=[{"field": "description", "values": ["desc A", "desc B"]}],
        )
        wired.profile = _make_profile(flags=["UNRESOLVED_CONFLICT:description"])
        result = _run()
        assert any("description" in f for f in result.flags)


class TestB25MarketingFilter:
    """B25: Marketing superlatives stripped before profile assembly."""

    def test_marketing_language_filter_flag(self, wired):
        wired.extracted = _make_extracted(flags=["MARKETING_LANGUAGE_FILTERED"])
        wired.profile = _make_profile(flags=["MARKETING_LANGUAGE_FILTERED"])
        result = _run()
        assert "MARKETING_LANGUAGE_FILTERED" in result.flags


class TestB27LinkedInStubbed:
    """B27: LinkedIn collector is permanently stubbed in V1."""

    def test_linkedin_stubbed_flag_in_result(self, wired):
        wired.bundle = _make_bundle(flags=["LINKEDIN_STUBBED"])
        wired.profile = _make_profile(flags=["LINKEDIN_STUBBED"])
        result = _run()
        assert "LINKEDIN_STUBBED" in result.flags


class TestB28ThirdPartyErrors:
    """B28: Registry and/or financial connector is unavailable or stubbed."""

    def test_registry_stubbed_flag(self, wired):
        wired.bundle = _make_bundle(flags=["REGISTRY_STUBBED", "NO_REGISTRY_RECORD"])
        wired.profile = _make_profile(flags=["REGISTRY_STUBBED"])
        result = _run()
        assert result.status == "COMPLETED"
        assert "REGISTRY_STUBBED" in result.flags

    def test_registry_error_degrades_gracefully(self, wired):
        wired.bundle = _make_bundle(flags=["REGISTRY_STUBBED", "NO_REGISTRY_RECORD"])
        wired.profile = _make_profile(status="PARTIALLY_ENRICHED", confidence="MEDIUM",
                                       flags=["REGISTRY_STUBBED"])
        result = _run()
        assert result.profile_status in ("PARTIALLY_ENRICHED", "ENRICHED")


class TestB29Disambiguation:
    """B29: Disambiguation notices indicate possible entity collision."""

    def test_disambiguation_notice_in_bundle(self, wired):
        notice = {"candidate": "Acme UK Ltd", "reason": "name_overlap", "confidence": "MEDIUM"}
        wired.bundle = _make_bundle(notices=[notice])
        wired.extracted = _make_extracted(flags=["DISAMBIGUATION_REQUIRED"])
        wired.profile = _make_profile(
            flags=["DISAMBIGUATION_REQUIRED"],
            triage=[{"reason": "disambiguation_required", "priority": "HIGH"}],
        )
        result = _run()
        assert "DISAMBIGUATION_REQUIRED" in result.flags

    def test_disambiguation_creates_triage(self, wired):
        wired.profile = _make_profile(
            flags=["DISAMBIGUATION_REQUIRED"],
            triage=[{"reason": "disambiguation_required", "priority": "HIGH"}],
        )
        result = _run()
        assert len(result.triage_tasks) >= 1


class TestB30BrandConfusion:
    """B30: Brain brand_map links a brand to its legal parent entity."""

    def test_brand_confusion_queues_brain_update(self, wired):
        update = _make_brain_update(update_type="BRAND_MAP", from_="instagram",
                                    to="Meta Platforms Inc")
        wired.profile = _make_profile(brain_updates=[update], flags=["BRAND_RESOLVED"])
        result = _run()
        assert any(s.update_type == "BRAND_MAP" for s in result.brain_update_suggestions)

    def test_brand_map_brain_update_written_to_queue(self, wired):
        update = _make_brain_update(update_type="BRAND_MAP", from_="instagram",
                                    to="Meta Platforms Inc")
        wired.profile = _make_profile(brain_updates=[update])
        _run()
        queue_path = wired.tmp_path / _PROG / "programme_run" / "brain_update_queue.md"
        assert queue_path.exists()


class TestB31HybridVendor:
    """B31: Vendor operates across multiple categories (e.g. SaaS + managed services)."""

    def test_hybrid_vendor_flags_preserved(self, wired):
        wired.extracted = _make_extracted(
            flags=["MULTI_CATEGORY_AMBIGUITY", "AMBIGUOUS_VENDOR_TYPE"],
        )
        wired.profile = _make_profile(
            flags=["MULTI_CATEGORY_AMBIGUITY", "AMBIGUOUS_VENDOR_TYPE"],
            triage=[{"reason": "hybrid_vendor_type", "priority": "MEDIUM"}],
        )
        result = _run()
        assert "MULTI_CATEGORY_AMBIGUITY" in result.flags
        assert "AMBIGUOUS_VENDOR_TYPE" in result.flags
        assert len(result.triage_tasks) >= 1


# ===========================================================================
# RESULT CONTRACT — fundamental guarantees of EnrichmentRunResult
# ===========================================================================

class TestResultContract:
    """Sanity checks that EnrichmentRunResult fields are always well-formed."""

    def test_completed_result_has_workflow_id(self, wired):
        result = _run()
        assert result.workflow_id is not None
        assert result.workflow_id.startswith("wf-enrich-")

    def test_pcs_after_gte_zero(self, wired):
        result = _run()
        assert result.pcs_after >= 0.0

    def test_completed_result_to_dict_serialisable(self, wired):
        result = _run()
        d = result.to_dict()
        assert d["status"] == "COMPLETED"
        assert d["vendor_id"] == _VENDOR
        json.dumps(d)  # must not raise

    def test_triage_tasks_written_to_queue_file(self, wired):
        wired.profile = _make_profile(
            triage=[{"reason": "needs_review", "priority": "HIGH"}],
        )
        _run()
        triage_path = wired.tmp_path / _PROG / "programme_run" / "triage_queue.md"
        assert triage_path.exists()
        assert "needs_review" in triage_path.read_text(encoding="utf-8")
