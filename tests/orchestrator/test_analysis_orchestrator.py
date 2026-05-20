"""Tests for src/cobalt/orchestrator/analysis_orchestrator.py — 48 tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cobalt.orchestrator.analysis_orchestrator as mod
from cobalt.models.schemas.an_schema import (
    ANRunResult,
    ANRunStatus,
    CommercialAnalysisResult,
    DimensionScore,
    Finding,
    FindingsBundle,
    HistoricalScoreState,
    NBA,
    NarrativeBundle,
    QAPair,
    ScoreBundle,
    ScoringConfig,
    TrendReport,
    ValidatedEvidenceAssembly,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VENDOR = "v-test-001"
_PROG   = "PROG-TEST"
_NOW    = "2020-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers: minimal valid objects
# ---------------------------------------------------------------------------

def _make_score_bundle(cri: int = 75) -> ScoreBundle:
    return ScoreBundle(
        vendor_id=_VENDOR,
        cri_score=cri,
        prior_cri=None,
        cri_delta=None,
        health_band="WATCH" if cri < 80 else "HEALTHY",
        dimension_scores=[
            DimensionScore("delivery_reliability", 70, None, None, None),
            DimensionScore("responsiveness",       72, None, None, None),
            DimensionScore("commercial_value",     75, None, None, None),
            DimensionScore("risk_compliance",      78, None, None, None),
            DimensionScore("relationship_trend",   80, None, None, None),
        ],
        operational_metrics={},
        portfolio_rank=None,
        category_rank=None,
        scored_at=_NOW,
    )


def _make_findings_bundle(finding_count: int = 0) -> FindingsBundle:
    findings = [
        Finding(
            finding_id=f"f-{i}",
            title=f"Finding {i}",
            severity="MEDIUM",
            why="reason",
            evidence_ids=[],
            source="SCORE",
            status="OPEN",
            created_at=_NOW,
        )
        for i in range(finding_count)
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


def _make_narrative_bundle() -> NarrativeBundle:
    return NarrativeBundle(
        vendor_id=_VENDOR,
        vendor_summary="Vendor is performing adequately.",
        finding_narratives=[],
        commercial_summary=None,
        qa_summaries=[],
        evidence_citations=[],
        redaction_flags=[],
        generated_at=_NOW,
    )


def _make_validated_assembly() -> ValidatedEvidenceAssembly:
    return ValidatedEvidenceAssembly(
        vendor_id=_VENDOR,
        programme_id=_PROG,
        facts=[],
        completeness_pct=0.8,
        conflict_count=0,
        stale_count=0,
        missing_count=2,
        validated_at=_NOW,
    )


def _make_commercial() -> CommercialAnalysisResult:
    return CommercialAnalysisResult(
        vendor_id=_VENDOR,
        contract_type="UNKNOWN",
        contract_type_confidence="LOW",
        utilisation_score=None,
        licence_waste_pct=None,
        cost_per_seat=None,
        shelfware_flag=False,
        sla_adherence_pct=None,
        delivery_score=None,
        milestone_status=None,
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


def _make_trend_report() -> TrendReport:
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


def _make_qa_pairs() -> list[QAPair]:
    return [
        QAPair(
            question_id=f"q-{i}",
            question=f"Question {i}?",
            answer_text="Some answer.",
            confidence="MEDIUM",
            completeness="PARTIAL",
            answered_by="inquiry_engine",
            evidence_citations=[],
            missing_evidence=[],
            tier=1,
            answered_at=_NOW,
        )
        for i in range(3)
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """Set up a minimal vendor workspace in tmp_path."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")  # disable DB in tests
    monkeypatch.setattr("cobalt.core.file_system.WORKSPACE_ROOT", tmp_path)

    vendor_dir = tmp_path / _PROG / _VENDOR
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "identity").mkdir()
    (vendor_dir / "profile").mkdir()

    return tmp_path


def _write_entity(ws: Path, status: str = "CONFIRMED") -> None:
    ep = ws / _PROG / _VENDOR / "identity" / "entity.md"
    ep.write_text(
        f"---\nstatus: {status}\nvendor_name: TestVendor\n---\n\n# Entity\n",
        encoding="utf-8",
    )


def _write_rs_profile(ws: Path, last_updated: str = "2019-01-01T00:00:00+00:00") -> None:
    rp = ws / _PROG / _VENDOR / "profile" / "relationship_spend_profile.md"
    rp.write_text(
        f"---\nvendor_id: {_VENDOR}\nprogramme_id: {_PROG}\n"
        f"last_updated: {last_updated}\npcs_total: 0.5\n"
        f"relationship_type: TRANSACTIONAL\ndependency_tier: TIER_3\n---\n\n",
        encoding="utf-8",
    )


def _write_analysis_result(ws: Path, last_analysed: str) -> None:
    ar = ws / _PROG / _VENDOR / "analysis_result.md"
    ar.write_text(
        f"---\ncri_score: 72\nhealth_band: WATCH\n"
        f"vendor_state: WATCH\nfinding_count: 0\n"
        f"last_analysed_at: {last_analysed}\n---\n\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Gate checks — BLOCKED
# ---------------------------------------------------------------------------

class TestGateChecksBlocked:
    def test_no_entity_md(self, ws):
        _write_rs_profile(ws)
        result = mod.run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.BLOCKED.value
        assert result.error == "entity_not_confirmed"

    def test_entity_not_confirmed(self, ws):
        _write_entity(ws, status="TRIAGE")
        _write_rs_profile(ws)
        result = mod.run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.BLOCKED.value
        assert result.error == "entity_not_confirmed"

    def test_entity_pending(self, ws):
        _write_entity(ws, status="PENDING")
        _write_rs_profile(ws)
        result = mod.run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.BLOCKED.value

    def test_no_rs_profile(self, ws):
        _write_entity(ws)
        result = mod.run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.BLOCKED.value
        assert result.error == "rs_profile_missing"

    def test_blocked_has_no_cri_score(self, ws):
        _write_rs_profile(ws)
        result = mod.run_analysis(_VENDOR, _PROG)
        assert result.cri_score is None
        assert result.pcs_before is None

    def test_blocked_returns_an_run_result(self, ws):
        _write_rs_profile(ws)
        result = mod.run_analysis(_VENDOR, _PROG)
        assert isinstance(result, ANRunResult)
        assert result.tools_run == []


# ---------------------------------------------------------------------------
# Gate checks — SKIPPED
# ---------------------------------------------------------------------------

class TestGateChecksSkipped:
    def test_fresh_analysis_skipped(self, ws):
        _write_entity(ws)
        _write_rs_profile(ws)
        _write_analysis_result(ws, last_analysed="2030-01-01T00:00:00+00:00")
        result = mod.run_analysis(_VENDOR, _PROG)
        assert result.status == ANRunStatus.SKIPPED.value
        assert result.skip_reason == "analysis_fresh"

    def test_fresh_analysis_returns_cached_cri(self, ws):
        _write_entity(ws)
        _write_rs_profile(ws)
        _write_analysis_result(ws, last_analysed="2030-01-01T00:00:00+00:00")
        result = mod.run_analysis(_VENDOR, _PROG)
        assert result.cri_score == 72

    def test_force_bypasses_freshness(self, ws, monkeypatch):
        _write_entity(ws)
        _write_rs_profile(ws)
        _write_analysis_result(ws, last_analysed="2030-01-01T00:00:00+00:00")

        monkeypatch.setattr(
            "cobalt.orchestrator.analysis_orchestrator._run_steps",
            lambda **kw: ({}, [], "test_abort"),
        )
        result = mod.run_analysis(_VENDOR, _PROG, force=True)

        assert result.status == ANRunStatus.FAILED.value  # got past gate into pipeline

    def test_stale_analysis_not_skipped(self, ws, monkeypatch):
        _write_entity(ws)
        _write_rs_profile(ws)
        _write_analysis_result(ws, last_analysed="2019-01-01T00:00:00+00:00")

        monkeypatch.setattr(
            "cobalt.orchestrator.analysis_orchestrator._run_steps",
            lambda **kw: ({}, [], "test_abort"),
        )
        result = mod.run_analysis(_VENDOR, _PROG)

        assert result.status != ANRunStatus.SKIPPED.value


# ---------------------------------------------------------------------------
# Gate checks — vendor_profile warning
# ---------------------------------------------------------------------------

class TestVendorProfileWarning:
    def test_missing_vendor_profile_warns_but_runs(self, ws, monkeypatch):
        _write_entity(ws)
        _write_rs_profile(ws)

        monkeypatch.setattr(
            "cobalt.orchestrator.analysis_orchestrator._run_steps",
            lambda **kw: ({}, [], "test"),
        )
        result = mod.run_analysis(_VENDOR, _PROG)

        assert result.status == ANRunStatus.FAILED.value  # made it past gates


# ---------------------------------------------------------------------------
# Pipeline failure paths
# ---------------------------------------------------------------------------

class TestRuntimeEngineFailure:
    def test_engine_exception_returns_failed(self, ws):
        _write_entity(ws)
        _write_rs_profile(ws)

        with patch("cobalt.orchestrator.analysis_orchestrator._run_steps",
                   side_effect=Exception("boom")):
            result = mod.run_analysis(_VENDOR, _PROG)

        assert result.status == ANRunStatus.FAILED.value
        assert "boom" in result.error

    def test_failed_result_has_pcs_before(self, ws):
        _write_entity(ws)
        _write_rs_profile(ws)

        with patch("cobalt.orchestrator.analysis_orchestrator._run_steps",
                   side_effect=Exception("x")):
            result = mod.run_analysis(_VENDOR, _PROG)

        assert result.pcs_before == 0.5  # from rs_profile frontmatter

    def test_incomplete_workflow_returns_failed(self, ws):
        _write_entity(ws)
        _write_rs_profile(ws)

        with patch("cobalt.orchestrator.analysis_orchestrator._run_steps",
                   return_value=({"validated_assembly": None}, ["s1_validate"], "step_failed")):
            result = mod.run_analysis(_VENDOR, _PROG)

        assert result.status == ANRunStatus.FAILED.value


# ---------------------------------------------------------------------------
# Happy path — COMPLETED
# ---------------------------------------------------------------------------

class TestHappyPath:
    @pytest.fixture()
    def completed_run(self, ws, monkeypatch):
        """Inject all 7 tool outputs via _run_steps monkeypatch."""
        _write_entity(ws)
        _write_rs_profile(ws)

        score_bundle    = _make_score_bundle()
        findings_bundle = _make_findings_bundle(finding_count=2)
        narrative_bundle = _make_narrative_bundle()
        validated       = _make_validated_assembly()
        commercial      = _make_commercial()
        trend_report    = _make_trend_report()
        qa_pairs        = _make_qa_pairs()

        tools_run = ["s1_validate", "s2_commercial", "s3_inquire",
                     "s4_score", "s5_trend", "s6_findings", "s7_narrative"]

        monkeypatch.setattr(
            "cobalt.orchestrator.analysis_orchestrator._run_steps",
            lambda **kw: (
                {
                    "score_bundle":       score_bundle,
                    "findings_bundle":    findings_bundle,
                    "narrative_bundle":   narrative_bundle,
                    "validated_assembly": validated,
                    "commercial_result":  commercial,
                    "trend_report":       trend_report,
                    "qa_pairs":           qa_pairs,
                },
                tools_run,
                None,
            ),
        )

        result = mod.run_analysis(_VENDOR, _PROG)
        return result, ws

    def test_status_completed(self, completed_run):
        result, _ = completed_run
        assert result.status == ANRunStatus.COMPLETED.value

    def test_cri_score_populated(self, completed_run):
        result, _ = completed_run
        assert result.cri_score == 75

    def test_health_band_populated(self, completed_run):
        result, _ = completed_run
        assert result.health_band == "WATCH"

    def test_finding_count(self, completed_run):
        result, _ = completed_run
        assert result.finding_count == 2

    def test_tools_run_populated(self, completed_run):
        result, _ = completed_run
        assert len(result.tools_run) == 7

    def test_pcs_after_populated(self, completed_run):
        result, _ = completed_run
        assert result.pcs_after is not None
        assert result.pcs_after >= 0.0

    def test_analysis_result_md_written(self, completed_run):
        _, ws = completed_run
        ar = ws / _PROG / _VENDOR / "analysis_result.md"
        assert ar.exists()

    def test_analysis_result_md_has_frontmatter(self, completed_run):
        _, ws = completed_run
        content = (ws / _PROG / _VENDOR / "analysis_result.md").read_text()
        assert "cri_score:" in content
        assert "health_band:" in content

    def test_score_history_written(self, completed_run):
        _, ws = completed_run
        sh = ws / _PROG / _VENDOR / "history" / "score_history.json"
        assert sh.exists()
        data = json.loads(sh.read_text())
        assert len(data["runs"]) == 1
        assert data["runs"][0]["cri_score"] == 75

    def test_qa_history_written(self, completed_run):
        _, ws = completed_run
        qh = ws / _PROG / _VENDOR / "history" / "qa_history.json"
        assert qh.exists()
        data = json.loads(qh.read_text())
        assert len(data["prior_pairs"]) == 3

    def test_evidence_state_written(self, completed_run):
        _, ws = completed_run
        es = ws / _PROG / _VENDOR / "history" / "evidence_state.json"
        assert es.exists()

    def test_commercial_state_written(self, completed_run):
        _, ws = completed_run
        cs = ws / _PROG / _VENDOR / "history" / "commercial_state.json"
        assert cs.exists()
        data = json.loads(cs.read_text())
        assert data["prior_contract_type"] == "UNKNOWN"

    def test_action_history_initialised(self, completed_run):
        _, ws = completed_run
        ah = ws / _PROG / _VENDOR / "history" / "action_history.json"
        assert ah.exists()

    def test_analysis_log_written(self, completed_run):
        _, ws = completed_run
        log = ws / _PROG / "programme_run" / "analysis_log.md"
        assert log.exists()
        assert _VENDOR in log.read_text()

    def test_ledger_appended(self, completed_run):
        _, ws = completed_run
        lp = ws / _PROG / _VENDOR / "execution" / "ledger.md"
        assert lp.exists()
        assert "P4 analysis completed" in lp.read_text()

    def test_no_error(self, completed_run):
        result, _ = completed_run
        assert result.error is None

    def test_analysed_at_set(self, completed_run):
        result, _ = completed_run
        assert result.analysed_at
        assert "T" in result.analysed_at


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

class TestUtilities:
    def test_now_iso_is_string(self):
        val = mod._now_iso()
        assert isinstance(val, str)
        assert "T" in val

    def test_read_md_frontmatter_missing(self, tmp_path):
        data = mod._read_md_frontmatter(tmp_path / "nonexistent.md")
        assert data == {}

    def test_read_md_frontmatter_valid(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("---\nkey: value\n---\n\n# Body\n", encoding="utf-8")
        data = mod._read_md_frontmatter(p)
        assert data["key"] == "value"

    def test_load_history_json_missing(self, tmp_path):
        result = mod._load_history_json(tmp_path / "missing.json", HistoricalScoreState)
        assert result is None

    def test_load_history_json_valid(self, tmp_path):
        p = tmp_path / "score.json"
        p.write_text(json.dumps({"vendor_id": "v-1", "runs": []}), encoding="utf-8")
        result = mod._load_history_json(p, HistoricalScoreState)
        assert result is not None
        assert result.vendor_id == "v-1"

    def test_default_scoring_config_weights_sum_to_one(self):
        cfg = mod._default_scoring_config()
        total = sum(cfg.dimension_weights.values())
        assert abs(total - 1.0) < 0.001

    def test_read_pcs_no_profile(self, ws):
        _write_entity(ws)
        val = mod._read_pcs(_PROG, _VENDOR)
        assert val == 0.0

    def test_read_pcs_from_frontmatter(self, ws):
        _write_entity(ws)
        _write_rs_profile(ws)
        val = mod._read_pcs(_PROG, _VENDOR)
        assert val == 0.5


# ---------------------------------------------------------------------------
# _build_analysis_result_md
# ---------------------------------------------------------------------------

class TestBuildAnalysisResultMd:
    def test_contains_cri_score(self):
        content = mod._build_analysis_result_md(
            vendor_id=_VENDOR,
            programme_id=_PROG,
            score_bundle=_make_score_bundle(80),
            findings_bundle=_make_findings_bundle(),
            narrative_bundle=_make_narrative_bundle(),
            vendor_state="HEALTHY",
            pcs_contribution=0.05,
            pcs_total=0.55,
            flags=["CRI_COMPUTED"],
            now_iso=_NOW,
        )
        assert "cri_score: 80" in content

    def test_contains_frontmatter_block(self):
        content = mod._build_analysis_result_md(
            vendor_id=_VENDOR,
            programme_id=_PROG,
            score_bundle=_make_score_bundle(),
            findings_bundle=_make_findings_bundle(),
            narrative_bundle=_make_narrative_bundle(),
            vendor_state="WATCH",
            pcs_contribution=0.05,
            pcs_total=0.55,
            flags=[],
            now_iso=_NOW,
        )
        assert content.startswith("---\n")

    def test_contains_vendor_summary_section(self):
        content = mod._build_analysis_result_md(
            vendor_id=_VENDOR,
            programme_id=_PROG,
            score_bundle=_make_score_bundle(),
            findings_bundle=_make_findings_bundle(),
            narrative_bundle=_make_narrative_bundle(),
            vendor_state="WATCH",
            pcs_contribution=0.05,
            pcs_total=0.55,
            flags=[],
            now_iso=_NOW,
        )
        assert "## Vendor Summary" in content

    def test_contains_scores_section(self):
        content = mod._build_analysis_result_md(
            vendor_id=_VENDOR,
            programme_id=_PROG,
            score_bundle=_make_score_bundle(),
            findings_bundle=_make_findings_bundle(),
            narrative_bundle=_make_narrative_bundle(),
            vendor_state="WATCH",
            pcs_contribution=0.0,
            pcs_total=0.5,
            flags=[],
            now_iso=_NOW,
        )
        assert "## Scores" in content


# ---------------------------------------------------------------------------
# run_analysis_all_confirmed
# ---------------------------------------------------------------------------

class TestRunAnalysisAllConfirmed:
    def test_empty_vendors_returns_empty(self, ws):
        with patch("cobalt.db.queries.get_confirmed_vendors", return_value=[]):
            results = mod.run_analysis_all_confirmed(_PROG)
        assert results == []

    def test_calls_run_analysis_per_vendor(self, ws):
        vendors = ["v-aaa", "v-bbb"]
        with patch("cobalt.db.queries.get_confirmed_vendors", return_value=vendors):
            with patch.object(mod, "run_analysis", return_value=MagicMock()) as mock_ra:
                mod.run_analysis_all_confirmed(_PROG)
        assert mock_ra.call_count == 2

    def test_db_query_failure_returns_empty(self, ws):
        with patch("cobalt.db.queries.get_confirmed_vendors", side_effect=Exception("db down")):
            results = mod.run_analysis_all_confirmed(_PROG)
        assert results == []

    def test_one_vendor_failure_does_not_affect_others(self, ws):
        vendors = ["v-ok", "v-bad"]
        good = MagicMock(status="COMPLETED")
        bad  = MagicMock(status="FAILED")

        def side_effect(vendor_id, programme_id, **kw):
            return good if vendor_id == "v-ok" else bad

        with patch("cobalt.db.queries.get_confirmed_vendors", return_value=vendors):
            with patch.object(mod, "run_analysis", side_effect=side_effect):
                results = mod.run_analysis_all_confirmed(_PROG)

        assert len(results) == 2
        assert results[0].status == "COMPLETED"
        assert results[1].status == "FAILED"
