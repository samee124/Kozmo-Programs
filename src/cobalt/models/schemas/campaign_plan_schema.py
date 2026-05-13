"""Campaign plan schema — output of Planning Agent Level 3."""

from dataclasses import dataclass, field


@dataclass
class CampaignSpec:
    campaign_type: str  # ERP_INTEGRATION / CONTRACT_DISCOVERY / OWNER_CHECKIN /
                        # MVC_CHECKIN / SSO_EXTRACTION / VENDOR_ADMIN_PULL /
                        # CRITICALITY_SURVEY
    goal: str
    resolves_gaps: list[str]
    completion_condition: str
    pcs_target: int
    stakeholder: str | None
    gates_required: list[str]
    priority: int  # 1=connectors first, 2=checkins


@dataclass
class CampaignPlanInput:
    vendor_id: str
    programme_id: str
    pcs: int
    pcs_band: str
    data_class: str
    tier: str | None
    blocking_gaps: list[str]
    enrichment_gaps: list[str]
    active_signals: list[str]
    erp_configured: bool
    clm_configured: bool
    sso_configured: bool
    spend_confirmed: bool
    contract_status: str
    inference_burden: str


@dataclass
class CampaignPlan:
    vendor_id: str
    campaigns: list[CampaignSpec]
    reason: str
    planned_at: str
