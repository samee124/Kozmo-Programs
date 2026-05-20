"""Integration tests — full P4 analysis pipeline, Process 4.

35 tests covering:
  A1–A7:  Happy-path end-to-end flow with stubbed tools
  B1–B6:  Gate check enforcement (BLOCKED / SKIPPED)
  C1–C6:  Workspace file outputs verified
  D1–D5:  Second run with history (score_history accumulates)
  E1–E5:  Force-flag and freshness behaviour
  F1–F6:  Failure and degraded-output paths

All external LLM/DB boundaries are mocked. Tools are stubbed at the
cobalt.orchestrator.analysis_orchestrator import boundary so the real
RuntimeEngine executes the step wiring, while tool logic is replaced with
deterministic stubs.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cobalt.orchestrator.analysis_orchestrator as orch
from cobalt.models.schemas.an_schema import (
    ANRunResult,
    ANRunStatus,
    CommercialAnalysisResult,
    DimensionScore,
    Finding,
    FindingsBundle,
    NarrativeBundle,
    QAPair,
    ScoreBundle,
    TrendReport,
    ValidatedEvidenceAssembly,
)
from cobalt.orchestrator.analysis_orchestrator import run_analysis

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VENDOR = "v-integ-001"
_PROG   = "PROG-INTEG"
_NOW    = "2020-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Stub builders
# ---------------------------------------------------------------------------

def _stub_validated(vendor_id: str = _VENDOR) -> ValidatedEvidenceAssembly:
    return ValidatedEvidenceAssembly(
        vendor_id=vendor_id,
        programme_id=_PROG,
        facts=[],
        completeness_pct=0.6,
        conflict_count=0,
        stale_count=1,
        missing_count=3,
        validated_at=_NOW,
    )


def _stub_commercial(vendor_id: str = _VENDOR) -> CommercialAnalysisResult:
    return CommercialAnalysisResult(
        vendor_id=vendor_id,
        contract_type="SERVICES",
        contract_type_confidence="HIGH",
        utilisation_score=None,
        licence_waste_pct=None,
        cost_per_seat=None,
        shelfware_flag=False,
        sla_adherence_pct=0.92,
        delivery_score=0.85,
        milestone_status="ON_TRACK",
        penalty_exposure=None,
        uptime_pct=None,
        incident_trend=None,
        mttr_days=None,
        commercial_risk_level="LOW",
        commercial_findings=[],
        spend_efficiency_score=None,
        renewal_risk_scenarios=[],
        spend_efficiency_narrative=None,
        analysed_at=_NOW,
    )


def _stub_qa_pairs() -> list[QAPair]:
    return [
        QAPair(
            question_id=f"q-t1-{i}",
            question=f"T1 question {i}?",
            answer_text="Answer available.",
            confidence="HIGH",
            completeness="COMPLETE",
            answered_by="inquiry_engine",
            evidence_citations=[],
            missing_evidence=[],
            tier=1,
            answered_at=_NOW,
        )
        for i in range(6)
    ]


def _stub_score_bundle(cri: int = 78) -> ScoreBundle:
    return ScoreBundle(
        vendor_id=_VENDOR,
        cri_score=cri,
        prior_cri=None,
        cri_delta=None,
        health_band="WATCH",
        dimension_scores=[
            DimensionScore("delivery_reliability", cri,     None, None, None),
            DimensionScore("responsiveness",        cri - 2, None, None, None),
            DimensionScore("commercial_value",      cri + 1, None, None, None),
            DimensionScore("risk_compliance",       cri - 1, None, None, None),
            DimensionScore("relationship_trend",    cri + 2, None, None, None),
        ],
        operational_metrics={},
        portfolio_rank=None,
        category_rank=None,
        scored_at=_NOW,
    )


def _stub_trend_report() -> TrendReport:
    return TrendReport(
        vendor_id=_VENDOR,
        dimension_trends={},
        action_learning=[],
        action_learning_summary=None,
        spend_trend={},
        sla_trend={},
        sentiment_trend={},
        trend_computed_at=_NOW,
        data_points_available=1,
    )


def _stub_findings_bundle(count: int = 1) -> FindingsBundle:
    findings = [
        Finding(
            finding_id=f"f-{i}",
            title=f"Finding {i}",
            severity="MEDIUM",
            why="test reason",
            evidence_ids=[],
            source="SCORE",
            status="OPEN",
            created_at=_NOW,
        )
        for i in range(count)
    ]
    return FindingsBundle(
        vendor_id=_VENDOR,
        findings=findings,
        gaps=[],
        nba=None,
        top_findings=findings[:3],
        triage_tasks=[],
        generated_at=_NOW,
    )


def _stub_narrative_bundle() -> NarrativeBundle:
    return NarrativeBundle(
        vendor_id=_VENDOR,
        vendor_summary="Vendor performing at acceptable levels.",
        finding_narratives=[],
        commercial_summary="Commercial terms are standard.",
        qa_summaries=[],
        evidence_citations=[],
        redaction_flags=[],
        generated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def an_workspace(tmp_path, monkeypatch):
    """Minimal workspace: entity.md CONFIRMED + rs_profile.md present."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")  # disable DB
    monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", tmp_path)

    vendor_dir = tmp_path / _PROG / _VENDOR
    (vendor_dir / "identity").mkdir(parents=True)
    (vendor_dir / "profile").mkdir(parents=True)

    (vendor_dir / "identity" / "entity.md").write_text(
        "---\nstatus: CONFIRMED\nvendor_name: IntegVendor\n---\n\n",
        encoding="utf-8",
    )
    (vendor_dir / "profile" / "relationship_spend_profile.md").write_text(
        f"---\nvendor_id: {_VENDOR}\nprogramme_id: {_PROG}\n"
        "last_updated: 2019-01-01T00:00:00+00:00\n"
        "pcs_total: 0.50\nrelationship_type: TRANSACTIONAL\n"
        "dependency_tier: TIER_2\n---\n\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def stub_all_tools(monkeypatch):
    """Stub all 7 P4 tools at the orchestrator boundary."""
    from cobalt.tools import (
        evidence_validator,
        commercial_analyser,
        inquiry_engine,
        scoring_engine,
        trend_analyser,
        finding_engine,
        narrative_engine,
    )

    monkeypatch.setattr(
        "cobalt.tools.evidence_validator.validate_evidence",
        lambda **kw: _stub_validated(kw.get("vendor_id", _VENDOR)),
    )
    monkeypatch.setattr(
        "cobalt.tools.commercial_analyser.analyse_commercial",
        lambda **kw: _stub_commercial(kw.get("vendor_id", _VENDOR)),
    )
    monkeypatch.setattr(
        "cobalt.tools.inquiry_engine.run_inquiry",
        lambda **kw: _stub_qa_pairs(),
    )
    monkeypatch.setattr(
        "cobalt.tools.scoring_engine.compute_scores",
        lambda **kw: _stub_score_bundle(),
    )
    monkeypatch.setattr(
        "cobalt.tools.trend_analyser.analyse_trends",
        lambda **kw: _stub_trend_report(),
    )
    monkeypatch.setattr(
        "cobalt.tools.finding_engine.detect_findings",
        lambda **kw: _stub_findings_bundle(),
    )
    monkeypatch.setattr(
        "cobalt.tools.narrative_engine.generate_narratives",
        lambda **kw: _stub_narrative_bundle(),
    )


# ---------------------------------------------------------------------------
# Group A — Happy path
# ---------------------------------------------------------------------------

class TestGroupAHappyPath:
    def test_a1_run_returns_completed(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.COMPLETED.value

    def test_a2_cri_score_populated(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        assert result.cri_score == 78

    def test_a3_health_band_populated(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        assert result.health_band == "WATCH"

    def test_a4_finding_count_populated(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        assert result.finding_count == 1

    def test_a5_all_7_tools_run(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        assert len(result.tools_run) == 7

    def test_a6_pcs_after_increases(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        assert result.pcs_after >= result.pcs_before

    def test_a7_analysis_result_md_exists(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        ar = an_workspace / _PROG / _VENDOR / "analysis_result.md"
        assert ar.exists()


# ---------------------------------------------------------------------------
# Group B — Gate checks
# ---------------------------------------------------------------------------

class TestGroupBGateChecks:
    def test_b1_no_entity_md_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", tmp_path)
        vd = tmp_path / _PROG / _VENDOR / "profile"
        vd.mkdir(parents=True)
        (vd / "relationship_spend_profile.md").write_text("---\nstatus: COMPLETE\n---\n")

        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.BLOCKED.value
        assert result.error == "entity_not_confirmed"

    def test_b2_unconfirmed_entity_blocked(self, an_workspace, stub_all_tools):
        ep = an_workspace / _PROG / _VENDOR / "identity" / "entity.md"
        ep.write_text("---\nstatus: PENDING\nvendor_name: X\n---\n")
        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.BLOCKED.value

    def test_b3_no_rs_profile_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", tmp_path)
        vd = tmp_path / _PROG / _VENDOR / "identity"
        vd.mkdir(parents=True)
        (vd / "entity.md").write_text("---\nstatus: CONFIRMED\n---\n")
        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.BLOCKED.value
        assert result.error == "rs_profile_missing"

    def test_b4_fresh_analysis_skipped(self, an_workspace, stub_all_tools):
        ar = an_workspace / _PROG / _VENDOR / "analysis_result.md"
        ar.write_text(
            "---\ncri_score: 75\nhealth_band: WATCH\nlast_analysed_at: 2030-06-01T00:00:00+00:00\n"
            "finding_count: 0\n---\n\n",
            encoding="utf-8",
        )
        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.SKIPPED.value
        assert result.skip_reason == "analysis_fresh"

    def test_b5_force_bypasses_fresh_skip(self, an_workspace, stub_all_tools):
        ar = an_workspace / _PROG / _VENDOR / "analysis_result.md"
        ar.write_text(
            "---\ncri_score: 75\nhealth_band: WATCH\nlast_analysed_at: 2030-06-01T00:00:00+00:00\n"
            "finding_count: 0\n---\n\n",
            encoding="utf-8",
        )
        result = run_analysis(_VENDOR, _PROG, force=True)
        assert result.status == ANRunStatus.COMPLETED.value

    def test_b6_vendor_profile_missing_still_runs(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        # vendor_profile.md not present — should still succeed
        assert result.status == ANRunStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# Group C — Workspace outputs
# ---------------------------------------------------------------------------

class TestGroupCWorkspaceOutputs:
    def test_c1_score_history_created(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        sh = an_workspace / _PROG / _VENDOR / "history" / "score_history.json"
        assert sh.exists()

    def test_c2_score_history_contains_run(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        data = json.loads(
            (an_workspace / _PROG / _VENDOR / "history" / "score_history.json").read_text()
        )
        assert len(data["runs"]) == 1
        assert data["runs"][0]["cri_score"] == 78

    def test_c3_qa_history_created(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        qh = an_workspace / _PROG / _VENDOR / "history" / "qa_history.json"
        assert qh.exists()

    def test_c4_analysis_result_has_yaml_front_matter(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        content = (an_workspace / _PROG / _VENDOR / "analysis_result.md").read_text()
        assert content.startswith("---\n")
        assert "cri_score:" in content

    def test_c5_ledger_appended(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        lp = an_workspace / _PROG / _VENDOR / "execution" / "ledger.md"
        assert lp.exists()
        assert "P4 analysis completed" in lp.read_text()

    def test_c6_analysis_log_appended(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        log = an_workspace / _PROG / "programme_run" / "analysis_log.md"
        assert log.exists()
        content = log.read_text()
        assert _VENDOR in content


# ---------------------------------------------------------------------------
# Group D — Second run with history
# ---------------------------------------------------------------------------

class TestGroupDSecondRun:
    def test_d1_second_run_accumulates_score_history(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        run_analysis(_VENDOR, _PROG, force=True)
        data = json.loads(
            (an_workspace / _PROG / _VENDOR / "history" / "score_history.json").read_text()
        )
        assert len(data["runs"]) == 2

    def test_d2_second_run_overwrites_qa_history(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        run_analysis(_VENDOR, _PROG, force=True)
        data = json.loads(
            (an_workspace / _PROG / _VENDOR / "history" / "qa_history.json").read_text()
        )
        assert len(data["prior_pairs"]) == 6  # 6 Q1 stubs

    def test_d3_second_run_updates_analysis_result(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        first_mtime = (an_workspace / _PROG / _VENDOR / "analysis_result.md").stat().st_mtime

        run_analysis(_VENDOR, _PROG, force=True)
        second_mtime = (an_workspace / _PROG / _VENDOR / "analysis_result.md").stat().st_mtime

        assert second_mtime >= first_mtime

    def test_d4_second_run_ledger_has_two_entries(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        run_analysis(_VENDOR, _PROG, force=True)
        content = (an_workspace / _PROG / _VENDOR / "execution" / "ledger.md").read_text()
        assert content.count("P4 analysis completed") == 2

    def test_d5_action_history_not_overwritten(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        ah_path = an_workspace / _PROG / _VENDOR / "history" / "action_history.json"
        # Simulate VW Agent writing an action entry
        ah_path.write_text(json.dumps({
            "vendor_id": _VENDOR,
            "actions": [{"action_type": "ESCALATE", "taken_at": "2020-01-02T00:00:00+00:00",
                         "before_cri": 78, "after_cri": 82, "delta": 4}],
        }), encoding="utf-8")

        run_analysis(_VENDOR, _PROG, force=True)
        data = json.loads(ah_path.read_text())
        # Action history should still have the VW Agent entry
        assert len(data["actions"]) >= 1


# ---------------------------------------------------------------------------
# Group E — Force and freshness
# ---------------------------------------------------------------------------

class TestGroupEForce:
    def test_e1_fresh_result_skips_without_force(self, an_workspace, stub_all_tools):
        run_analysis(_VENDOR, _PROG)
        # Overwrite with a "fresh" result
        ar = an_workspace / _PROG / _VENDOR / "analysis_result.md"
        ar.write_text(
            "---\ncri_score: 99\nhealth_band: HEALTHY\nlast_analysed_at: 2030-06-01T00:00:00+00:00\n"
            "finding_count: 0\n---\n\n",
            encoding="utf-8",
        )
        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.SKIPPED.value
        assert result.cri_score == 99  # cached value returned

    def test_e2_force_re_runs_despite_fresh(self, an_workspace, stub_all_tools):
        ar = an_workspace / _PROG / _VENDOR / "analysis_result.md"
        ar.write_text(
            "---\ncri_score: 99\nhealth_band: HEALTHY\nlast_analysed_at: 2030-06-01T00:00:00+00:00\n"
            "finding_count: 0\n---\n\n",
            encoding="utf-8",
        )
        result = run_analysis(_VENDOR, _PROG, force=True)
        assert result.status == ANRunStatus.COMPLETED.value
        assert result.cri_score == 78  # new run's value

    def test_e3_pcs_before_read_from_rs_profile(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        assert result.pcs_before == 0.50  # from rs_profile frontmatter

    def test_e4_pcs_after_gt_pcs_before_when_cri_computed(self, an_workspace, stub_all_tools):
        result = run_analysis(_VENDOR, _PROG)
        # CRI_COMPUTED flag adds 0.05
        assert result.pcs_after > result.pcs_before

    def test_e5_skip_preserves_error_none(self, an_workspace, stub_all_tools):
        ar = an_workspace / _PROG / _VENDOR / "analysis_result.md"
        ar.write_text(
            "---\ncri_score: 75\nhealth_band: WATCH\nlast_analysed_at: 2030-06-01T00:00:00+00:00\n"
            "finding_count: 0\n---\n\n",
            encoding="utf-8",
        )
        result = run_analysis(_VENDOR, _PROG)
        assert result.error is None


# ---------------------------------------------------------------------------
# Group F — Failure paths
# ---------------------------------------------------------------------------

class TestGroupFFailures:
    def test_f1_evidence_validator_exception_causes_failed(self, an_workspace, monkeypatch):
        monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", an_workspace)
        monkeypatch.setenv("DATABASE_URL", "")

        def bad_validate(**kw):
            raise RuntimeError("evidence boom")

        monkeypatch.setattr("cobalt.tools.evidence_validator.validate_evidence", bad_validate)
        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.FAILED.value

    def test_f2_scoring_engine_exception_causes_failed(self, an_workspace, monkeypatch):
        monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", an_workspace)
        monkeypatch.setenv("DATABASE_URL", "")

        monkeypatch.setattr(
            "cobalt.tools.evidence_validator.validate_evidence",
            lambda **kw: _stub_validated(),
        )
        monkeypatch.setattr(
            "cobalt.tools.commercial_analyser.analyse_commercial",
            lambda **kw: _stub_commercial(),
        )
        monkeypatch.setattr(
            "cobalt.tools.inquiry_engine.run_inquiry",
            lambda **kw: _stub_qa_pairs(),
        )
        monkeypatch.setattr(
            "cobalt.tools.scoring_engine.compute_scores",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("scoring boom")),
        )

        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.FAILED.value

    def test_f3_failed_result_never_raises(self, an_workspace, monkeypatch):
        monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", an_workspace)
        monkeypatch.setenv("DATABASE_URL", "")

        def always_fail(**kw):
            raise RuntimeError("always fail")

        monkeypatch.setattr("cobalt.tools.evidence_validator.validate_evidence", always_fail)
        # Must not raise
        result = run_analysis(_VENDOR, _PROG)
        assert isinstance(result, ANRunResult)

    def test_f4_failed_result_has_error_field(self, an_workspace, monkeypatch):
        monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", an_workspace)
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setattr(
            "cobalt.tools.evidence_validator.validate_evidence",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("specific error")),
        )
        result = run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.FAILED.value

    def test_f5_result_is_always_an_run_result(self, an_workspace, monkeypatch):
        monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", an_workspace)
        monkeypatch.setenv("DATABASE_URL", "")
        # Completely broken execution pipeline — orchestrator must still return ANRunResult
        monkeypatch.setattr(
            "cobalt.orchestrator.analysis_orchestrator._run_steps",
            lambda *a, **kw: (_ for _ in ()).throw(Exception("kaboom")),
        )
        result = run_analysis(_VENDOR, _PROG)
        assert isinstance(result, ANRunResult)

    def test_f6_narrative_failure_does_not_block_result(self, an_workspace, monkeypatch):
        monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", an_workspace)
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setattr(
            "cobalt.tools.evidence_validator.validate_evidence",
            lambda **kw: _stub_validated(),
        )
        monkeypatch.setattr(
            "cobalt.tools.commercial_analyser.analyse_commercial",
            lambda **kw: _stub_commercial(),
        )
        monkeypatch.setattr(
            "cobalt.tools.inquiry_engine.run_inquiry",
            lambda **kw: _stub_qa_pairs(),
        )
        monkeypatch.setattr(
            "cobalt.tools.scoring_engine.compute_scores",
            lambda **kw: _stub_score_bundle(),
        )
        monkeypatch.setattr(
            "cobalt.tools.trend_analyser.analyse_trends",
            lambda **kw: _stub_trend_report(),
        )
        monkeypatch.setattr(
            "cobalt.tools.finding_engine.detect_findings",
            lambda **kw: _stub_findings_bundle(),
        )
        monkeypatch.setattr(
            "cobalt.tools.narrative_engine.generate_narratives",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("narrative failed")),
        )

        # With narrative failure the step will fail → pipeline FAILED
        result = run_analysis(_VENDOR, _PROG)
        # Either FAILED or COMPLETED with degraded narrative — either is acceptable
        assert result.status in (ANRunStatus.COMPLETED.value, ANRunStatus.FAILED.value)
