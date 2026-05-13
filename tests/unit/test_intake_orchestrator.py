"""Tests for cobalt.orchestrator.intake_orchestrator."""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from cobalt.models.schemas.investigation_plan_schema import (
    FraudRisk,
    InvestigationDepth,
    InvestigationPlan,
)
from cobalt.models.schemas.signal_profile_schema import (
    ApSignal,
    BatchContext,
    BrainHit,
    DedupResult,
    EntityType,
    ErpSignal,
    ScriptType,
    SignalProfile,
)
from cobalt.orchestrator.intake_orchestrator import IntakeRunResult, run_intake
from cobalt.tools.entity_decision_and_shell_creation import EntityDecision
from cobalt.tools.external_validation import ValidationResult
from cobalt.tools.source_intake import DocRecord, RawCandidate, SourceIntakeResult


def _read_frontmatter(path: Path) -> dict:
    """Parse YAML front-matter from a .md plan file."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    if len(parts) >= 3:
        return yaml.safe_load(parts[1]) or {}
    return yaml.safe_load(text) or {}


# ---------------------------------------------------------------------------
# Shared mock factories
# ---------------------------------------------------------------------------

def _no_hit() -> BrainHit:
    return BrainHit(
        matched=False, confidence=0.0, canonical=None, match_type=None,
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def _make_profile(raw: str = "IBM", comparison_key: str = "ibm") -> SignalProfile:
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


def _make_plan(depth: InvestigationDepth = InvestigationDepth.FAST) -> InvestigationPlan:
    return InvestigationPlan(
        depth=depth,
        steps=[],
        require_human_gate=False,
        require_legal_gate=False,
        fraud_risk=FraudRisk.LOW,
        resolving_question=None,
        reason="test",
    )


def _make_confirmed_decision(vendor_id: str = "v-abc12345") -> EntityDecision:
    return EntityDecision(
        vendor_id=vendor_id,
        status="CONFIRMED",
        workspace_path="/workspace/prog/v-abc",
        data_class="CLASS_D",
        initial_pcs=0,
        confidence=0.55,
        fraud_risk="LOW",
        triage_question=None,
        block_reason=None,
        files_written=[],
    )


def _make_triage_decision() -> EntityDecision:
    return EntityDecision(
        vendor_id=None,
        status="TRIAGE_REQUIRED",
        workspace_path=None,
        data_class="CLASS_D",
        initial_pcs=0,
        confidence=0.55,
        fraud_risk="LOW",
        triage_question="Manual review needed",
        block_reason=None,
        files_written=[],
    )


def _make_source_result(candidates=None, doc_registry=None):
    return SourceIntakeResult(
        candidates=candidates or [],
        doc_registry=doc_registry or {},
        source_summary={"candidates_total": len(candidates or []), "doc_count": 0},
    )


def _empty_batch_context():
    return BatchContext(
        known_vendors={}, rebrand_map={}, alias_map={},
        erp_hits={}, ap_counts={}, all_keys=[],
        doc_registry={}, candidate_hints={},
    )


def _make_resolution(recommendation: str, profile: SignalProfile | None = None):
    from cobalt.tools.entity_resolution import ResolutionResult
    p = profile or _make_profile()
    return ResolutionResult(
        comparison_key=p.comparison_key,
        signal_profile=p,
        resolution_status="MATCHED" if recommendation == "AUTO_CONFIRM" else "UNMATCHED_VIABLE",
        matched_canonical=None,
        match_type=None,
        confidence=0.0,
        entity_type=EntityType.COMPANY,
        recommendation=recommendation,
    )


def _make_validation(profile: SignalProfile | None = None) -> ValidationResult:
    p = profile or _make_profile()
    return ValidationResult(
        entry=p.raw,
        profile=p,
        plan=_make_plan(),
        step_results=[],
        final_status="CONFIRMED",
        exit_reason=None,
    )


class MockPlanningAgent:
    def write_programme_plan(self, programme_id, source_summary):
        from cobalt.workspace.plan_writer import write_programme_plan
        write_programme_plan(programme_id, {"research_tier": "STANDARD"})

    def write_investigation_plan(self, programme_id, key, profile):
        from cobalt.workspace.plan_writer import write_investigation_plan
        plan = _make_plan()
        path = write_investigation_plan(programme_id, key, profile, plan)
        return path, plan

    def compute_investigation_plan(self, profile):
        return _make_plan()


class MockResearchAgent:
    def web_research(self, *a, **kw):
        return ""

    def erp_batch_scan(self, keys):
        return {}

    def ap_batch_scan(self, keys):
        return {}

    def fetch_google_drive_docs(self, path):
        return []


class MockAnalysisAgent:
    def extract_vendor_name_from_doc(self, raw_text, filename):
        return {"vendor_name": None, "doc_type_hint": "UNKNOWN"}

    def extract_contract_terms(self, raw_text, vendor_name):
        return {"confidence": 0.8}

    def consolidate_documents(self, terms_list):
        return terms_list[0] if terms_list else None


def _patch_all(monkeypatch, *, source_result=None, screened=None,
               resolutions=None, decisions=None, validate_fn=None):
    """Patch all tool and agent references in intake_orchestrator."""
    si = source_result or _make_source_result()
    screened = screened or []
    resolutions = resolutions or []

    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.source_intake_run",
        lambda *a, **kw: si,
    )
    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.screen_all",
        lambda entries: screened,
    )
    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.build_batch_context",
        lambda *a, **kw: _empty_batch_context(),
    )
    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.resolve_all",
        lambda s, ctx: resolutions,
    )

    if validate_fn is None:
        monkeypatch.setattr(
            "cobalt.orchestrator.intake_orchestrator.validate",
            lambda *a, **kw: _make_validation(),
        )
    else:
        monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.validate", validate_fn)

    if decisions is not None:
        _decision_iter = iter(decisions)
        monkeypatch.setattr(
            "cobalt.orchestrator.intake_orchestrator.decide_and_create",
            lambda *a, **kw: next(_decision_iter),
        )

    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.PlanningAgent", MockPlanningAgent)
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.ResearchAgent", MockResearchAgent)
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.AnalysisAgent", MockAnalysisAgent)


# ---------------------------------------------------------------------------
# Basic
# ---------------------------------------------------------------------------

def test_run_intake_empty_returns_intake_run_result(tmp_workspace, monkeypatch):
    _patch_all(monkeypatch)
    result = run_intake("prog-1")
    assert isinstance(result, IntakeRunResult)
    assert result.programme_id == "prog-1"


def test_run_intake_empty_never_raises(tmp_workspace, monkeypatch):
    _patch_all(monkeypatch)
    result = run_intake("prog-1")
    assert result.summary["total_input"] == 0


def test_run_intake_empty_all_lists_empty(tmp_workspace, monkeypatch):
    _patch_all(monkeypatch)
    result = run_intake("prog-1")
    assert result.confirmed == []
    assert result.triage == []
    assert result.discarded == []
    assert result.blocked == []


# ---------------------------------------------------------------------------
# AUTO_CONFIRM routing
# ---------------------------------------------------------------------------

def test_auto_confirm_goes_to_confirmed(tmp_workspace, monkeypatch):
    profile = _make_profile("IBM", "ibm")
    resolution = _make_resolution("AUTO_CONFIRM", profile)
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([RawCandidate(
            raw_name="IBM", source="VENDOR_LIST", source_refs=[], linked_doc_ids=[],
            spend_hint=None, category_hint=None, department_hint=None,
        )]),
        screened=[_make_screened("IBM")],
        resolutions=[resolution],
        decisions=[_make_confirmed_decision()],
    )
    result = run_intake("prog-1")
    assert len(result.confirmed) == 1
    assert result.summary["confirmed"] == 1


def test_auto_confirm_confirmed_decision_triage_routes_to_triage(tmp_workspace, monkeypatch):
    profile = _make_profile("Unclear", "unclear")
    resolution = _make_resolution("AUTO_CONFIRM", profile)
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([_make_raw_candidate("Unclear")]),
        screened=[_make_screened("Unclear")],
        resolutions=[resolution],
        decisions=[_make_triage_decision()],
    )
    result = run_intake("prog-1")
    assert len(result.triage) == 1


# ---------------------------------------------------------------------------
# DISCARD routing
# ---------------------------------------------------------------------------

def test_discard_goes_to_discarded(tmp_workspace, monkeypatch):
    profile = _make_profile("John Smith", "john smith")
    resolution = _make_resolution("DISCARD", profile)
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([_make_raw_candidate("John Smith")]),
        screened=[_make_screened("John Smith")],
        resolutions=[resolution],
    )
    result = run_intake("prog-1")
    assert len(result.discarded) == 1
    assert result.summary["discarded"] == 1


# ---------------------------------------------------------------------------
# INVESTIGATE routing
# ---------------------------------------------------------------------------

def test_investigate_validation_called(tmp_workspace, monkeypatch):
    validate_calls = []

    def mock_validate(entry, profile, plan, ra):
        validate_calls.append(entry)
        return _make_validation(profile)

    profile = _make_profile("Aramark", "aramark")
    resolution = _make_resolution("INVESTIGATE", profile)
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([_make_raw_candidate("Aramark")]),
        screened=[_make_screened("Aramark")],
        resolutions=[resolution],
        decisions=[_make_confirmed_decision()],
        validate_fn=mock_validate,
    )
    run_intake("prog-1")
    assert len(validate_calls) == 1
    assert validate_calls[0] == "Aramark"


def test_investigate_ip_file_written(tmp_workspace, monkeypatch):
    profile = _make_profile("Aramark", "aramark")
    resolution = _make_resolution("INVESTIGATE", profile)
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([_make_raw_candidate("Aramark")]),
        screened=[_make_screened("Aramark")],
        resolutions=[resolution],
        decisions=[_make_confirmed_decision()],
    )
    run_intake("prog-1")
    intake_plans_dir = tmp_workspace / "prog-1" / "programme_run" / "intake_plans"
    ip_files = list(intake_plans_dir.glob("IP-aramark-*.md"))
    assert len(ip_files) == 1


def test_investigate_ip_file_finalised(tmp_workspace, monkeypatch):
    profile = _make_profile("Aramark", "aramark")
    resolution = _make_resolution("INVESTIGATE", profile)
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([_make_raw_candidate("Aramark")]),
        screened=[_make_screened("Aramark")],
        resolutions=[resolution],
        decisions=[_make_confirmed_decision()],
    )
    run_intake("prog-1")
    intake_plans_dir = tmp_workspace / "prog-1" / "programme_run" / "intake_plans"
    ip_file = list(intake_plans_dir.glob("IP-aramark-*.md"))[0]
    data = _read_frontmatter(ip_file)
    assert data["outcome"]["status"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# Summary counts
# ---------------------------------------------------------------------------

def test_summary_counts_correct(tmp_workspace, monkeypatch):
    profiles = [
        _make_profile("IBM", "ibm"),
        _make_profile("John Smith", "john smith"),
        _make_profile("Aramark", "aramark"),
    ]
    resolutions = [
        _make_resolution("AUTO_CONFIRM", profiles[0]),
        _make_resolution("DISCARD", profiles[1]),
        _make_resolution("INVESTIGATE", profiles[2]),
    ]
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([
            _make_raw_candidate("IBM"),
            _make_raw_candidate("John Smith"),
            _make_raw_candidate("Aramark"),
        ]),
        screened=[
            _make_screened("IBM"),
            _make_screened("John Smith"),
            _make_screened("Aramark"),
        ],
        resolutions=resolutions,
        decisions=iter([_make_confirmed_decision("v-ibm"), _make_confirmed_decision("v-aramark")]),
    )
    result = run_intake("prog-1")
    assert result.summary["total_input"] == 3
    assert result.summary["confirmed"] == 2
    assert result.summary["discarded"] == 1


# ---------------------------------------------------------------------------
# Document terms extraction
# ---------------------------------------------------------------------------

def test_doc_terms_extracted_for_linked_vendor(tmp_workspace, monkeypatch):
    extract_calls = []

    class TrackingAnalysisAgent(MockAnalysisAgent):
        def extract_contract_terms(self, raw_text, vendor_name):
            extract_calls.append(vendor_name)
            return {"confidence": 0.8}

    doc_id = "abc123"
    doc_registry = {
        doc_id: DocRecord(
            doc_id=doc_id, filename="ibm.pdf", file_path="/d/ibm.pdf",
            raw_text="Contract for IBM services", doc_type="MSA", vendor_key="ibm",
        )
    }
    profile = _make_profile("IBM", "ibm")
    profile.linked_doc_ids.append(doc_id)
    resolution = _make_resolution("AUTO_CONFIRM", profile)

    # Patch everything first, then override AnalysisAgent with the tracking version
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([_make_raw_candidate("IBM")], doc_registry),
        screened=[_make_screened("IBM")],
        resolutions=[resolution],
        decisions=[_make_confirmed_decision()],
    )
    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.AnalysisAgent",
        lambda: TrackingAnalysisAgent(),
    )

    run_intake("prog-1")
    assert len(extract_calls) >= 1


# ---------------------------------------------------------------------------
# Programme files written
# ---------------------------------------------------------------------------

def test_vendor_register_written(tmp_workspace, monkeypatch):
    _patch_all(monkeypatch)
    run_intake("prog-1")
    assert (tmp_workspace / "prog-1" / "programme_run" / "vendor_register.md").exists()


def test_triage_queue_written(tmp_workspace, monkeypatch):
    _patch_all(monkeypatch)
    run_intake("prog-1")
    assert (tmp_workspace / "prog-1" / "programme_run" / "triage_queue.md").exists()


def test_run_log_written(tmp_workspace, monkeypatch):
    _patch_all(monkeypatch)
    run_intake("prog-1")
    assert (tmp_workspace / "prog-1" / "programme_run" / "run_log.md").exists()


def test_run_log_contains_intake_completed(tmp_workspace, monkeypatch):
    _patch_all(monkeypatch)
    run_intake("prog-1")
    content = (tmp_workspace / "prog-1" / "programme_run" / "run_log.md").read_text(encoding="utf-8")
    assert "INTAKE_COMPLETED" in content


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

def test_one_candidate_exception_others_still_processed(tmp_workspace, monkeypatch):
    call_count = [0]

    def mock_decide_with_one_failure(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("first candidate explodes")
        return _make_confirmed_decision("v-second")

    monkeypatch.setattr(
        "cobalt.orchestrator.intake_orchestrator.decide_and_create",
        mock_decide_with_one_failure,
    )

    profiles = [_make_profile("IBM", "ibm"), _make_profile("Aramark", "aramark")]
    resolutions = [
        _make_resolution("AUTO_CONFIRM", profiles[0]),
        _make_resolution("AUTO_CONFIRM", profiles[1]),
    ]
    _patch_all(
        monkeypatch,
        source_result=_make_source_result([
            _make_raw_candidate("IBM"), _make_raw_candidate("Aramark"),
        ]),
        screened=[_make_screened("IBM"), _make_screened("Aramark")],
        resolutions=resolutions,
    )
    # decide_and_create already patched above — don't override via _patch_all decisions param

    result = run_intake("prog-1")
    assert len(result.confirmed) == 1
    assert result.confirmed[0].vendor_id == "v-second"


def test_run_intake_never_raises_on_full_failure(tmp_workspace, monkeypatch):
    def _crash(*a, **kw):
        raise RuntimeError("everything exploded")

    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.source_intake_run", _crash)
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.PlanningAgent", MockPlanningAgent)
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.ResearchAgent", MockResearchAgent)
    monkeypatch.setattr("cobalt.orchestrator.intake_orchestrator.AnalysisAgent", MockAnalysisAgent)
    result = run_intake("prog-1")
    assert isinstance(result, IntakeRunResult)


# ---------------------------------------------------------------------------
# Private helpers used in tests
# ---------------------------------------------------------------------------

def _make_screened(raw: str):
    from cobalt.tools.candidate_screening import ScreenedCandidate
    return ScreenedCandidate(
        raw=raw,
        cleaned=raw,
        normalized=raw.lower(),
        comparison_key=raw.lower(),
        country_hint=None,
        entity_type=EntityType.COMPANY,
        disposition="PASS",
        rejection_reason=None,
        source_refs=[],
        linked_doc_ids=[],
        spend_hint=None,
        category_hint=None,
    )


def _make_raw_candidate(name: str) -> RawCandidate:
    return RawCandidate(
        raw_name=name, source="VENDOR_LIST", source_refs=[], linked_doc_ids=[],
        spend_hint=None, category_hint=None, department_hint=None,
    )
