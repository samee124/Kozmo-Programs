"""Tests for cobalt.models.schemas.campaign_plan_schema."""

from cobalt.models.schemas.campaign_plan_schema import (
    CampaignPlan,
    CampaignPlanInput,
    CampaignSpec,
)


def _connector_spec() -> CampaignSpec:
    return CampaignSpec(
        campaign_type="ERP_INTEGRATION",
        goal="Connect ERP to confirm spend",
        resolves_gaps=["spend_unconfirmed"],
        completion_condition="ERP connector returns data",
        pcs_target=30,
        stakeholder=None,
        gates_required=[],
        priority=1,
    )


def _checkin_spec() -> CampaignSpec:
    return CampaignSpec(
        campaign_type="OWNER_CHECKIN",
        goal="Identify vendor owner",
        resolves_gaps=["owner_unknown"],
        completion_condition="Owner responds",
        pcs_target=15,
        stakeholder="procurement@example.com",
        gates_required=["HUMAN_GATE"],
        priority=2,
    )


def test_campaign_spec_instantiates_correctly():
    spec = _connector_spec()
    assert spec.campaign_type == "ERP_INTEGRATION"
    assert spec.priority == 1


def test_campaign_plan_input_instantiates_with_all_fields():
    inp = CampaignPlanInput(
        vendor_id="v001",
        programme_id="p001",
        pcs=22,
        pcs_band="LOW",
        data_class="CLASS_C",
        tier="T2",
        blocking_gaps=["spend_unconfirmed"],
        enrichment_gaps=["owner_unknown"],
        active_signals=["erp_exists"],
        erp_configured=True,
        clm_configured=False,
        sso_configured=False,
        spend_confirmed=False,
        contract_status="NO_CONTRACT",
        inference_burden="MEDIUM",
    )
    assert inp.vendor_id == "v001"
    assert inp.erp_configured is True


def test_campaign_plan_with_empty_campaigns_is_valid():
    plan = CampaignPlan(
        vendor_id="v001",
        campaigns=[],
        reason="No gaps identified",
        planned_at="2026-05-07T10:00:00",
    )
    assert plan.campaigns == []
    assert plan.vendor_id == "v001"


def test_priority_1_for_connector_campaigns():
    spec = _connector_spec()
    assert spec.priority == 1


def test_priority_2_for_checkin_campaigns():
    spec = _checkin_spec()
    assert spec.priority == 2


def test_campaign_plan_holds_multiple_specs():
    plan = CampaignPlan(
        vendor_id="v001",
        campaigns=[_connector_spec(), _checkin_spec()],
        reason="Two gaps",
        planned_at="2026-05-07T10:00:00",
    )
    assert len(plan.campaigns) == 2
    assert plan.campaigns[0].priority == 1
    assert plan.campaigns[1].priority == 2
