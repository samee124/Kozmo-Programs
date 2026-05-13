"""Tests for cobalt.workspace.plan_writer."""

from pathlib import Path

import pytest
import yaml

from cobalt.intake.steps import StepResult
from cobalt.models.schemas.investigation_plan_schema import (
    FraudRisk,
    InvestigationDepth,
    InvestigationPlan,
)
from cobalt.models.schemas.signal_profile_schema import (
    ApSignal,
    BrainHit,
    DedupResult,
    EntityType,
    ErpSignal,
    ScriptType,
    SignalProfile,
)
from cobalt.workspace.plan_writer import (
    finalise_plan,
    read_next_pending_step,
    update_plan_step,
    write_investigation_plan,
    write_programme_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_frontmatter(path: Path) -> dict:
    """Parse the YAML front-matter from a .md plan file."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    if len(parts) >= 3:
        return yaml.safe_load(parts[1]) or {}
    return yaml.safe_load(text) or {}


def _no_hit() -> BrainHit:
    return BrainHit(
        matched=False, confidence=0.0, canonical=None, match_type=None,
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def make_profile(**overrides) -> SignalProfile:
    defaults = dict(
        raw="IBM",
        cleaned="IBM",
        script_type=ScriptType.LATIN,
        country_hint=None,
        normalized="ibm",
        comparison_key="ibm",
        brain_hit=_no_hit(),
        erp_signal=ErpSignal(exists=False, spend=None, category=None, vendor_ids=[]),
        ap_signal=ApSignal(invoice_count=0, flags=[], single_approver=False),
        linked_doc_ids=[],
        spend_hint=None,
        category_hint=None,
        entity_type=EntityType.COMPANY,
        dedup_result=DedupResult(status="UNIQUE", match_key=None, match_name=None, similarity=0.0),
    )
    defaults.update(overrides)
    return SignalProfile(**defaults)


def make_plan(**overrides) -> InvestigationPlan:
    defaults = dict(
        depth=InvestigationDepth.FAST,
        steps=["WEB_RESEARCH_FAST", "FRAUD_CHECK_BASIC"],
        require_human_gate=False,
        require_legal_gate=False,
        fraud_risk=FraudRisk.LOW,
        resolving_question=None,
        reason="standard investigation",
    )
    defaults.update(overrides)
    return InvestigationPlan(**defaults)


def _make_step_result(step: str = "FRAUD_CHECK_BASIC") -> StepResult:
    return StepResult(
        step=step,
        success=True,
        early_exit=False,
        exit_status=None,
        data={"risk_level": "LOW", "fraud_signals": []},
        note="ok",
    )


# ---------------------------------------------------------------------------
# write_programme_plan
# ---------------------------------------------------------------------------

def test_write_programme_plan_creates_file(tmp_workspace):
    path = write_programme_plan("prog-001", {"research_tier": "STANDARD"})
    assert path.exists()
    assert path.name == "programme_plan.md"


def test_write_programme_plan_correct_path(tmp_workspace):
    path = write_programme_plan("prog-001", {})
    assert "programme_run" in str(path)
    assert "programme_plan.md" in str(path)


def test_write_programme_plan_content(tmp_workspace):
    path = write_programme_plan("prog-42", {"research_tier": "DEEP"})
    data = _read_frontmatter(path)
    assert data["programme_id"] == "prog-42"
    assert data["research_tier"] == "DEEP"
    assert "created_at" in data


def test_write_programme_plan_has_step_checkboxes(tmp_workspace):
    path = write_programme_plan("prog-01", {})
    content = path.read_text(encoding="utf-8")
    assert "- [ ] `source_intake`" in content
    assert "- [ ] `candidate_screening`" in content
    assert "- [ ] `entity_resolution`" in content


def test_write_programme_plan_has_strategy_section(tmp_workspace):
    path = write_programme_plan("prog-01", {"strategy_prose": "My custom strategy."})
    content = path.read_text(encoding="utf-8")
    assert "My custom strategy." in content
    assert "## Strategy" in content


# ---------------------------------------------------------------------------
# write_investigation_plan
# ---------------------------------------------------------------------------

def test_write_investigation_plan_creates_001(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    assert path.name == "IP-ibm-001.md"
    assert path.exists()


def test_write_investigation_plan_second_call_creates_002(tmp_workspace):
    write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    path2 = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    assert path2.name == "IP-ibm-002.md"


def test_write_investigation_plan_different_keys_independent(tmp_workspace):
    p1 = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    p2 = write_investigation_plan("prog-1", "aramark", make_profile(), make_plan())
    assert p1.name == "IP-ibm-001.md"
    assert p2.name == "IP-aramark-001.md"


def test_write_investigation_plan_contains_signals(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    data = _read_frontmatter(path)
    assert "signals" in data
    assert "brain_hit" in data["signals"]
    assert "dedup_result" in data["signals"]
    assert "entity_type" in data["signals"]


def test_write_investigation_plan_contains_plan(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    data = _read_frontmatter(path)
    assert "plan" in data
    assert data["plan"]["depth"] == "FAST"
    assert "WEB_RESEARCH_FAST" in data["plan"]["steps"]


def test_write_investigation_plan_outcome_pending(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    data = _read_frontmatter(path)
    assert data["outcome"]["status"] == "PENDING"
    assert data["outcome"]["vendor_id"] is None


def test_write_investigation_plan_execution_empty(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    data = _read_frontmatter(path)
    assert data["execution"]["step_results"] == {}
    assert data["execution"]["started_at"] is None


def test_write_investigation_plan_has_step_checkboxes(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    content = path.read_text(encoding="utf-8")
    assert "- [ ] `WEB_RESEARCH_FAST`" in content
    assert "- [ ] `FRAUD_CHECK_BASIC`" in content


def test_write_investigation_plan_has_entity_profile_table(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    content = path.read_text(encoding="utf-8")
    assert "## Entity Profile" in content
    assert "| Raw input |" in content


def test_write_investigation_plan_has_why_prose(tmp_workspace):
    path = write_investigation_plan(
        "prog-1", "ibm", make_profile(), make_plan(), why_prose="Custom reasoning here."
    )
    content = path.read_text(encoding="utf-8")
    assert "Custom reasoning here." in content


def test_write_investigation_plan_has_execution_log_table(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    content = path.read_text(encoding="utf-8")
    assert "## Execution Log" in content
    assert "| `WEB_RESEARCH_FAST` | PENDING | — |" in content


def test_write_investigation_plan_no_steps_shows_no_steps_text(tmp_workspace):
    path = write_investigation_plan(
        "prog-1", "ibm", make_profile(),
        make_plan(steps=[], depth=InvestigationDepth.SKIP),
    )
    content = path.read_text(encoding="utf-8")
    assert "No steps required" in content


# ---------------------------------------------------------------------------
# update_plan_step
# ---------------------------------------------------------------------------

def test_update_plan_step_adds_result(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    result = _make_step_result("FRAUD_CHECK_BASIC")
    update_plan_step(path, "FRAUD_CHECK_BASIC", result)
    data = _read_frontmatter(path)
    assert "FRAUD_CHECK_BASIC" in data["execution"]["step_results"]


def test_update_plan_step_stores_success_flag(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    update_plan_step(path, "FRAUD_CHECK_BASIC", _make_step_result())
    data = _read_frontmatter(path)
    assert data["execution"]["step_results"]["FRAUD_CHECK_BASIC"]["success"] is True


def test_update_plan_step_multiple_steps(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    update_plan_step(path, "WEB_RESEARCH_FAST", _make_step_result("WEB_RESEARCH_FAST"))
    update_plan_step(path, "FRAUD_CHECK_BASIC", _make_step_result("FRAUD_CHECK_BASIC"))
    data = _read_frontmatter(path)
    assert "WEB_RESEARCH_FAST" in data["execution"]["step_results"]
    assert "FRAUD_CHECK_BASIC" in data["execution"]["step_results"]


def test_update_plan_step_ticks_checkbox(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    update_plan_step(path, "WEB_RESEARCH_FAST", _make_step_result("WEB_RESEARCH_FAST"))
    content = path.read_text(encoding="utf-8")
    assert "- [x] `WEB_RESEARCH_FAST`" in content
    # The other step stays unchecked
    assert "- [ ] `FRAUD_CHECK_BASIC`" in content


def test_update_plan_step_updates_execution_log_row(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    update_plan_step(path, "WEB_RESEARCH_FAST", _make_step_result("WEB_RESEARCH_FAST"))
    content = path.read_text(encoding="utf-8")
    assert "| `WEB_RESEARCH_FAST` | DONE |" in content


def test_update_plan_step_missing_file_silent(tmp_workspace):
    missing = tmp_workspace / "nonexistent" / "IP-x-001.md"
    update_plan_step(missing, "STEP", _make_step_result())  # must not raise


# ---------------------------------------------------------------------------
# finalise_plan
# ---------------------------------------------------------------------------

def test_finalise_plan_sets_status(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED", vendor_id="v-abc123")
    data = _read_frontmatter(path)
    assert data["outcome"]["status"] == "CONFIRMED"


def test_finalise_plan_sets_vendor_id(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED", vendor_id="v-abc123")
    data = _read_frontmatter(path)
    assert data["outcome"]["vendor_id"] == "v-abc123"


def test_finalise_plan_sets_workspace_path(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED", workspace_path="/workspace/prog-1/v-abc123")
    data = _read_frontmatter(path)
    assert data["outcome"]["workspace_path"] == "/workspace/prog-1/v-abc123"


def test_finalise_plan_sets_started_at(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED")
    data = _read_frontmatter(path)
    assert data["execution"]["started_at"] is not None


def test_finalise_plan_updates_outcome_section_in_body(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED", vendor_id="v-abc123", workspace_path="/ws/v-abc123")
    content = path.read_text(encoding="utf-8")
    assert "**Status:** CONFIRMED" in content
    assert "**Vendor ID:** v-abc123" in content
    assert "**Workspace:** /ws/v-abc123" in content


def test_finalise_plan_missing_file_silent(tmp_workspace):
    missing = tmp_workspace / "nonexistent" / "IP-x-001.md"
    finalise_plan(missing, "CONFIRMED")  # must not raise


# ---------------------------------------------------------------------------
# read_next_pending_step
# ---------------------------------------------------------------------------

def test_read_next_pending_step_returns_first(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    step = read_next_pending_step(path)
    assert step == "WEB_RESEARCH_FAST"


def test_read_next_pending_step_advances_after_tick(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    update_plan_step(path, "WEB_RESEARCH_FAST", _make_step_result("WEB_RESEARCH_FAST"))
    step = read_next_pending_step(path)
    assert step == "FRAUD_CHECK_BASIC"


def test_read_next_pending_step_returns_none_when_all_done(tmp_workspace):
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    update_plan_step(path, "WEB_RESEARCH_FAST", _make_step_result("WEB_RESEARCH_FAST"))
    update_plan_step(path, "FRAUD_CHECK_BASIC", _make_step_result("FRAUD_CHECK_BASIC"))
    assert read_next_pending_step(path) is None


def test_read_next_pending_step_missing_file_returns_none(tmp_workspace):
    missing = tmp_workspace / "nonexistent" / "IP-x-001.md"
    assert read_next_pending_step(missing) is None


# ---------------------------------------------------------------------------
# Regression: vendor names / paths starting with digits (group-ref bug)
# ---------------------------------------------------------------------------

def test_finalise_plan_vendor_id_starts_with_digit(tmp_workspace):
    """vendor_id like 'V-20th-001' must not raise invalid group reference."""
    path = write_investigation_plan("prog-1", "20th-television", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED", vendor_id="V-20th-001", workspace_path="workspace/20th-television-001")
    content = path.read_text(encoding="utf-8")
    assert "**Vendor ID:** V-20th-001" in content
    assert "**Workspace:** workspace/20th-television-001" in content


def test_finalise_plan_with_windows_backslash_path(tmp_workspace):
    """workspace_path with Windows backslashes (the actual real-world trigger) must not raise."""
    path = write_investigation_plan("prog-1", "20th-television", make_profile(), make_plan())
    ws_path = r"workspace\nova-2026\V-20th-001"
    finalise_plan(path, "CONFIRMED", vendor_id="V-20th-001", workspace_path=ws_path)
    content = path.read_text(encoding="utf-8")
    assert "**Vendor ID:** V-20th-001" in content
    assert r"workspace\nova-2026\V-20th-001" in content


def test_finalise_plan_with_pure_digit_name(tmp_workspace):
    """vendor name starting with four digits (e.g. '3033 Group LLC') must not raise."""
    path = write_investigation_plan("prog-1", "3033-group-llc", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED", vendor_id="V-3033-001", workspace_path=r"workspace\nova-2026\V-3033-001")
    content = path.read_text(encoding="utf-8")
    assert "**Vendor ID:** V-3033-001" in content


def test_finalise_plan_with_mixed_digits_letters(tmp_workspace):
    """vendor name '24by7 security' style must not raise."""
    path = write_investigation_plan("prog-1", "24by7-security", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED", vendor_id="V-24by7-001", workspace_path=r"workspace\nova-2026\V-24by7-001")
    content = path.read_text(encoding="utf-8")
    assert "**Vendor ID:** V-24by7-001" in content


def test_finalise_plan_normal_name_still_works(tmp_workspace):
    """Regression: plain alphabetic vendor names still finalise correctly."""
    path = write_investigation_plan("prog-1", "salesforce", make_profile(), make_plan())
    finalise_plan(path, "CONFIRMED", vendor_id="V-salesforce-001", workspace_path="workspace/nova-2026/V-salesforce-001")
    content = path.read_text(encoding="utf-8")
    assert "**Status:** CONFIRMED" in content
    assert "**Vendor ID:** V-salesforce-001" in content


def test_update_plan_step_summary_with_backslash(tmp_workspace):
    """step note containing a backslash must not raise invalid group reference."""
    path = write_investigation_plan("prog-1", "ibm", make_profile(), make_plan())
    result = _make_step_result("WEB_RESEARCH_FAST")
    result = StepResult(**{**result.__dict__, "note": r"ok \3 results"})
    update_plan_step(path, "WEB_RESEARCH_FAST", result)
    content = path.read_text(encoding="utf-8")
    assert r"ok \3 results" in content
