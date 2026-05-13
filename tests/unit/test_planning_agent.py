"""Tests for cobalt.agents.planning_agent."""

from decimal import Decimal

import pytest

from cobalt.agents.planning_agent import PlanningAgent
from cobalt.brain.loader import invalidate_cache, load_brain
from cobalt.models.schemas.campaign_plan_schema import CampaignPlanInput
from cobalt.models.schemas.investigation_plan_schema import (
    FraudRisk,
    InvestigationDepth,
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


@pytest.fixture(autouse=True)
def _clear_brain_cache():
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def brain():
    return load_brain()


def _no_hit() -> BrainHit:
    return BrainHit(
        matched=False, confidence=0.0, canonical=None, match_type=None,
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def _known_hit(match_type: str = "KNOWN_VENDOR") -> BrainHit:
    return BrainHit(
        matched=True, confidence=0.99, canonical="Test Corp", match_type=match_type,
        rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
    )


def _rebrand_hit() -> BrainHit:
    return BrainHit(
        matched=True, confidence=0.95, canonical="New Corp", match_type="REBRAND",
        rebrand_match=True, rebrand_target="New Corp", alias_match=False, alias_target=None,
    )


def make_profile(**overrides) -> SignalProfile:
    defaults = dict(
        raw="Test",
        cleaned="Test",
        script_type=ScriptType.LATIN,
        country_hint=None,
        normalized="Test",
        comparison_key="test",
        brain_hit=_no_hit(),
        erp_signal=ErpSignal(exists=False, spend=None, category=None, vendor_ids=[]),
        ap_signal=ApSignal(invoice_count=0, flags=[], single_approver=False, approver_id=None),
        linked_doc_ids=[],
        spend_hint=None,
        category_hint=None,
        entity_type=EntityType.COMPANY,
        dedup_result=DedupResult(status="UNIQUE", match_key=None, match_name=None, similarity=0.0),
    )
    defaults.update(overrides)
    return SignalProfile(**defaults)


agent = PlanningAgent()


# ---------------------------------------------------------------------------
# _max_depth / _max_fraud
# ---------------------------------------------------------------------------

def test_max_depth_deep_wins():
    assert agent._max_depth("DEEP", "STANDARD") == "DEEP"

def test_max_depth_standard_loses():
    assert agent._max_depth("STANDARD", "DEEP") == "DEEP"

def test_max_fraud_high_wins():
    assert agent._max_fraud("HIGH", "MEDIUM") == "HIGH"

def test_max_fraud_medium_loses():
    assert agent._max_fraud("MEDIUM", "HIGH") == "HIGH"


# ---------------------------------------------------------------------------
# R01 — PERSON
# ---------------------------------------------------------------------------

def test_r01_person_skip():
    plan = agent.compute_investigation_plan(make_profile(entity_type=EntityType.PERSON))
    assert plan.depth == InvestigationDepth.SKIP
    assert plan.steps == []
    assert "Person" in plan.reason


# ---------------------------------------------------------------------------
# R02 — INTERNAL
# ---------------------------------------------------------------------------

def test_r02_internal_skip():
    plan = agent.compute_investigation_plan(make_profile(entity_type=EntityType.INTERNAL))
    assert plan.depth == InvestigationDepth.SKIP
    assert plan.steps == []


# ---------------------------------------------------------------------------
# R03 — known vendor / alias brain match
# ---------------------------------------------------------------------------

def test_r03_known_vendor():
    plan = agent.compute_investigation_plan(make_profile(brain_hit=_known_hit("KNOWN_VENDOR")))
    assert plan.depth == InvestigationDepth.SKIP
    assert plan.steps == ["FRAUD_CHECK_ASYNC"]

def test_r03_alias():
    plan = agent.compute_investigation_plan(make_profile(brain_hit=_known_hit("ALIAS")))
    assert plan.depth == InvestigationDepth.SKIP
    assert plan.steps == ["FRAUD_CHECK_ASYNC"]


# ---------------------------------------------------------------------------
# R04 — rebrand match
# ---------------------------------------------------------------------------

def test_r04_rebrand():
    plan = agent.compute_investigation_plan(make_profile(brain_hit=_rebrand_hit()))
    assert plan.depth == InvestigationDepth.SKIP
    assert "CONFIRM_REBRAND" in plan.steps
    assert "FRAUD_CHECK_ASYNC" in plan.steps


# ---------------------------------------------------------------------------
# R05 — AUTO_MERGE
# ---------------------------------------------------------------------------

def test_r05_auto_merge():
    dedup = DedupResult(status="AUTO_MERGE", match_key="microsfot", match_name="microsfot", similarity=0.97)
    plan = agent.compute_investigation_plan(make_profile(dedup_result=dedup))
    assert plan.depth == InvestigationDepth.SKIP
    assert plan.steps == ["MERGE_CANONICAL"]


# ---------------------------------------------------------------------------
# R06 — CANDIDATE
# ---------------------------------------------------------------------------

def test_r06_candidate():
    dedup = DedupResult(status="CANDIDATE", match_key="amzon", match_name="amzon", similarity=0.85)
    plan = agent.compute_investigation_plan(
        make_profile(normalized="Amazon", dedup_result=dedup)
    )
    assert plan.depth == InvestigationDepth.TRIAGE
    assert plan.steps == ["ROUTE_TO_HUMAN"]
    assert plan.require_human_gate is True
    assert plan.resolving_question is not None


# ---------------------------------------------------------------------------
# R07 — sanctioned country
# ---------------------------------------------------------------------------

def test_r07_sanctioned_country():
    plan = agent.compute_investigation_plan(make_profile(country_hint="IR"))
    assert "SANCTIONS_CHECK" in plan.steps
    assert "LEGAL_GATE" in plan.steps
    assert plan.require_legal_gate is True
    assert plan.depth == InvestigationDepth.INTENSIVE


# ---------------------------------------------------------------------------
# R08 — non-Latin script
# ---------------------------------------------------------------------------

def test_r08_non_latin_script():
    plan = agent.compute_investigation_plan(make_profile(script_type=ScriptType.CJK))
    assert "TRANSLITERATION" in plan.steps
    assert "MULTILINGUAL_SEARCH" in plan.steps


# ---------------------------------------------------------------------------
# R09 — high spend unknown vendor
# ---------------------------------------------------------------------------

def test_r09_high_spend():
    erp = ErpSignal(exists=True, spend=Decimal("200000"), category=None, vendor_ids=[])
    plan = agent.compute_investigation_plan(make_profile(erp_signal=erp))
    assert plan.depth == InvestigationDepth.DEEP
    assert "WEB_RESEARCH_DEEP" in plan.steps


# ---------------------------------------------------------------------------
# R10 — medium spend unknown vendor
# ---------------------------------------------------------------------------

def test_r10_medium_spend():
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    plan = agent.compute_investigation_plan(make_profile(erp_signal=erp))
    assert plan.depth == InvestigationDepth.STANDARD
    assert "WEB_RESEARCH_STANDARD" in plan.steps


# ---------------------------------------------------------------------------
# R11 — unknown vendor no spend
# ---------------------------------------------------------------------------

def test_r11_no_spend_company():
    plan = agent.compute_investigation_plan(make_profile())
    assert plan.depth == InvestigationDepth.FAST
    assert "WEB_RESEARCH_FAST" in plan.steps


# ---------------------------------------------------------------------------
# R12 — invoice fraud patterns
# ---------------------------------------------------------------------------

def test_r12_round_numbers():
    ap = ApSignal(invoice_count=3, flags=["ROUND_NUMBERS"], single_approver=False)
    plan = agent.compute_investigation_plan(make_profile(ap_signal=ap))
    assert "FRAUD_CHECK_DEEP" in plan.steps
    assert plan.fraud_risk == FraudRisk.MEDIUM

def test_r12_single_approver():
    ap = ApSignal(invoice_count=3, flags=[], single_approver=True)
    plan = agent.compute_investigation_plan(make_profile(ap_signal=ap))
    assert "FRAUD_CHECK_DEEP" in plan.steps
    assert plan.fraud_risk == FraudRisk.MEDIUM


# ---------------------------------------------------------------------------
# R13 — shell company signals
# ---------------------------------------------------------------------------

def test_r13_shell_signals():
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    ap = ApSignal(invoice_count=10, flags=[], single_approver=False)
    plan = agent.compute_investigation_plan(make_profile(erp_signal=erp, ap_signal=ap))
    assert "FRAUD_CHECK_DEEP" in plan.steps


# ---------------------------------------------------------------------------
# R14 — document available for unknown vendor
# ---------------------------------------------------------------------------

def test_r14_linked_docs():
    plan = agent.compute_investigation_plan(make_profile(linked_doc_ids=["doc-001"]))
    assert "DOCUMENT_EXTRACTION" in plan.steps


# ---------------------------------------------------------------------------
# Step ordering
# ---------------------------------------------------------------------------

def test_step_ordering_r07_r08_r09():
    erp = ErpSignal(exists=True, spend=Decimal("200000"), category=None, vendor_ids=[])
    plan = agent.compute_investigation_plan(
        make_profile(country_hint="IR", script_type=ScriptType.CJK, erp_signal=erp)
    )
    idx = {s: i for i, s in enumerate(plan.steps)}
    assert idx["TRANSLITERATION"] < idx["WEB_RESEARCH_DEEP"]
    assert idx["WEB_RESEARCH_DEEP"] < idx["SANCTIONS_CHECK"]


# ---------------------------------------------------------------------------
# Step deduplication
# ---------------------------------------------------------------------------

def test_step_deduplication_r12_r13():
    erp = ErpSignal(exists=True, spend=Decimal("50000"), category=None, vendor_ids=[])
    ap = ApSignal(invoice_count=10, flags=["ROUND_NUMBERS"], single_approver=False)
    plan = agent.compute_investigation_plan(make_profile(erp_signal=erp, ap_signal=ap))
    assert plan.steps.count("FRAUD_CHECK_DEEP") == 1


# ---------------------------------------------------------------------------
# compute_campaign_plan
# ---------------------------------------------------------------------------

def _make_input(**overrides) -> CampaignPlanInput:
    defaults = dict(
        vendor_id="v-001",
        programme_id="p-001",
        pcs=40,
        pcs_band="C",
        data_class="CLASS_C",
        tier="T2",
        blocking_gaps=[],
        enrichment_gaps=[],
        active_signals=[],
        erp_configured=False,
        clm_configured=False,
        sso_configured=False,
        spend_confirmed=False,
        contract_status="UNKNOWN",
        inference_burden="LOW",
    )
    defaults.update(overrides)
    return CampaignPlanInput(**defaults)


def test_campaign_annual_spend_erp_configured():
    plan = agent.compute_campaign_plan(
        _make_input(blocking_gaps=["annual_spend"], erp_configured=True)
    )
    types = [c.campaign_type for c in plan.campaigns]
    assert "ERP_INTEGRATION" in types


def test_campaign_annual_spend_no_erp():
    plan = agent.compute_campaign_plan(
        _make_input(blocking_gaps=["annual_spend"], erp_configured=False)
    )
    types = [c.campaign_type for c in plan.campaigns]
    assert "MVC_CHECKIN" in types


def test_campaign_renewal_date_clm_configured():
    plan = agent.compute_campaign_plan(
        _make_input(blocking_gaps=["renewal_date"], clm_configured=True)
    )
    types = [c.campaign_type for c in plan.campaigns]
    assert "CONTRACT_DISCOVERY" in types


def test_campaign_connector_priority_1():
    plan = agent.compute_campaign_plan(
        _make_input(blocking_gaps=["annual_spend"], erp_configured=True)
    )
    erp = next(c for c in plan.campaigns if c.campaign_type == "ERP_INTEGRATION")
    assert erp.priority == 1


def test_campaign_checkin_priority_2():
    plan = agent.compute_campaign_plan(
        _make_input(blocking_gaps=["annual_spend"], erp_configured=False)
    )
    mvc = next(c for c in plan.campaigns if c.campaign_type == "MVC_CHECKIN")
    assert mvc.priority == 2


def test_campaign_contract_discovery_priority_1():
    plan = agent.compute_campaign_plan(
        _make_input(blocking_gaps=["renewal_date"], clm_configured=True)
    )
    cd = next(c for c in plan.campaigns if c.campaign_type == "CONTRACT_DISCOVERY")
    assert cd.priority == 1


def test_campaign_no_duplicate_types():
    # Two gaps that resolve to the same campaign type (stakeholder + owner → OWNER_CHECKIN)
    plan = agent.compute_campaign_plan(
        _make_input(blocking_gaps=["stakeholder", "owner"])
    )
    types = [c.campaign_type for c in plan.campaigns]
    assert len(types) == len(set(types))


def test_campaign_connectors_before_checkins():
    # ERP (connector) + stakeholder (check-in)
    plan = agent.compute_campaign_plan(
        _make_input(blocking_gaps=["annual_spend", "stakeholder"], erp_configured=True)
    )
    priorities = [c.priority for c in plan.campaigns]
    assert priorities == sorted(priorities)


# ---------------------------------------------------------------------------
# write_programme_plan
# ---------------------------------------------------------------------------

def test_write_programme_plan_creates_file(tmp_workspace, mock_llm):
    path = agent.write_programme_plan("prog-x", {"candidates_total": 50, "doc_count": 0})
    assert path.exists()
    assert path.name == "programme_plan.md"


def test_write_programme_plan_returns_path(tmp_workspace, mock_llm):
    from pathlib import Path
    path = agent.write_programme_plan("prog-x", {})
    assert isinstance(path, Path)


def test_write_programme_plan_has_step_checkboxes(tmp_workspace, mock_llm):
    path = agent.write_programme_plan("prog-x", {})
    content = path.read_text(encoding="utf-8")
    assert "- [ ] `source_intake`" in content
    assert "- [ ] `entity_resolution`" in content


def test_write_programme_plan_frontmatter_has_programme_id(tmp_workspace, mock_llm):
    import yaml
    path = agent.write_programme_plan("prog-x", {})
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    data = yaml.safe_load(parts[1])
    assert data["programme_id"] == "prog-x"


# ---------------------------------------------------------------------------
# write_investigation_plan
# ---------------------------------------------------------------------------

def test_write_investigation_plan_skip_returns_none_path(tmp_workspace, mock_llm):
    # Known vendor → SKIP depth → no file written
    path, plan = agent.write_investigation_plan(
        "prog-x", "ibm",
        make_profile(brain_hit=BrainHit(
            matched=True, confidence=0.99, canonical="IBM", match_type="KNOWN_VENDOR",
            rebrand_match=False, rebrand_target=None, alias_match=False, alias_target=None,
        )),
    )
    assert path is None


def test_write_investigation_plan_returns_plan(tmp_workspace, mock_llm):
    from cobalt.models.schemas.investigation_plan_schema import InvestigationPlan
    _, plan = agent.write_investigation_plan("prog-x", "acme", make_profile())
    assert isinstance(plan, InvestigationPlan)


def test_write_investigation_plan_creates_file(tmp_workspace, mock_llm):
    path, _ = agent.write_investigation_plan("prog-x", "acme", make_profile())
    assert path is not None
    assert path.exists()
    assert path.name == "IP-acme-001.md"


def test_write_investigation_plan_has_why_prose_section(tmp_workspace, mock_llm):
    path, _ = agent.write_investigation_plan("prog-x", "acme", make_profile())
    content = path.read_text(encoding="utf-8")
    assert "## Why This Plan" in content


def test_write_investigation_plan_has_readable_steps(tmp_workspace, mock_llm):
    path, _ = agent.write_investigation_plan("prog-x", "acme", make_profile())
    content = path.read_text(encoding="utf-8")
    # R11 fires for unknown vendor no spend → WEB_RESEARCH_FAST
    assert "WEB_RESEARCH_FAST" in content


# ---------------------------------------------------------------------------
# _generate_why_prose / _generate_strategy_prose
# ---------------------------------------------------------------------------

def test_generate_why_prose_returns_string(mock_llm):
    from cobalt.models.schemas.investigation_plan_schema import FraudRisk, InvestigationDepth, InvestigationPlan
    plan = InvestigationPlan(
        depth=InvestigationDepth.FAST, steps=["WEB_RESEARCH_FAST"],
        require_human_gate=False, require_legal_gate=False,
        fraud_risk=FraudRisk.LOW, resolving_question=None, reason="test",
    )
    result = agent._generate_why_prose(make_profile(), plan)
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_strategy_prose_returns_string(mock_llm):
    result = agent._generate_strategy_prose({"candidates_total": 100, "doc_count": 5})
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_why_prose_falls_back_on_llm_failure():
    from cobalt.models.schemas.investigation_plan_schema import FraudRisk, InvestigationDepth, InvestigationPlan
    plan = InvestigationPlan(
        depth=InvestigationDepth.FAST, steps=[],
        require_human_gate=False, require_legal_gate=False,
        fraud_risk=FraudRisk.LOW, resolving_question=None, reason="fallback reason",
    )
    # No mock_llm — real llm_call will fail (no API key), but _generate_why_prose never raises
    result = agent._generate_why_prose(make_profile(), plan)
    assert isinstance(result, str)
    assert len(result) > 0
