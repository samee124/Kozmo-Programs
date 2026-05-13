# VW Agent Specification

## Role
Per-vendor lifecycle driver after workspace exists.
ONE LLM call per tick for tactical decisions.
Calls Planning Agent for strategic decisions.
The ONLY agent that writes workspace files after intake.

## When VW Agent Starts
ONLY after workspace is created by workspace_builder.
VW Agent NEVER runs during intake.
First tick = Stage 1 of programme stages.

## Starting State Depends on Intake
Scenario 1 vendor (no documents): PCS 5-15, CLASS_C
Scenario 2 vendor (with documents): PCS 35-50, CLASS_B
VW Agent picks up from wherever intake left off.

## Tactical vs Strategic — The Decision Rule

### VW Agent Handles (tactical — ONE LLM call per tick)
What connector to call next.
When to dispatch a check-in.
When to advance a campaign step.
Processing a check-in response.
Generating the VIF when conditions are met.
Any single next action this tick.

### VW Agent Calls Planning Agent (strategic)
Stage 4: gap analysis complete → need campaign plans.
  VW Agent passes CampaignPlanInput to Planning Agent.
  Planning Agent returns list of CampaignSpec.
  VW Agent writes CAMP-{id}.md files from specs.

Stage 5: significant mid-campaign event occurs:
  Check-in response changes commercial picture materially.
  New HIGH signal fired that changes campaign priority.
  Campaign timed out — need revised approach.
  VW Agent passes updated WorkspaceState + event.
  Planning Agent returns revised campaign specs.
  VW Agent updates CAMP-{id}.md files.

P3: vendor ready for negotiation:
  VW Agent passes NegotiationPlanInput to Planning Agent.
  Planning Agent returns NegotiationPlan.
  VW Agent writes negotiation_plan.md.

## The APE Pattern Per Tick

### ANALYZE — _read_workspace()
Read workspace files into WorkspaceState.
Never pass raw file content to LLM.
Maximum 800 tokens in prompt.

@dataclass
class WorkspaceState:
    vendor_id:              str
    programme_id:           str
    directive:              str
    vendor_name:            str
    tier:                   str | None
    data_class:             str
    identity_confidence:    float
    category:               str | None
    pcs:                    int
    pcs_band:               str
    blocking_gaps:          list[str]
    active_signals:         list[str]
    pending_actions:        list[str]
    active_campaigns:       list[str]
    annual_spend:           float | None
    renewal_date:           str | None
    days_to_opt_out:        int | None
    auto_renewal:           bool | None
    contract_status:        str

### PLAN — _reason()
Checks: does this tick require strategic planning?
  Stage 4 with no campaigns yet → call Planning Agent first
  P3 ready with no negotiation plan → call Planning Agent first
  Significant mid-campaign event → call Planning Agent first

Otherwise: ONE LLM call. Temperature=0. JSON output.
Returns AgentDecision:
{
  "action_type": str,
  "tool_calls": list[str],
  "reason": str,
  "next_status": str,
  "next_action_due_hours": int
}

### EXECUTE — _act()
If Planning Agent was called: write plan files from returned specs.
Execute AgentDecision tool_calls via TOOL_REGISTRY.
Write results via atomic_write() only.
Append to ledger after every action.

## Action Types
RESEARCH_ENTITY      data_class=CLASS_D, no entity research yet
CONNECTOR_CALL       run configured connectors
CHECKIN_COMPOSE      build check-in from blocking gaps
CHECKIN_DISPATCH     send composed check-in
PROCESS_RESPONSE     check-in response received
ADVANCE_CAMPAIGN     campaign active, next step ready
WAITING_DELIBERATE   strategic pause
GATE_AWAIT           waiting for human gate
GENERATE_VIF         pcs >= 50 AND blocking_gaps=[]

## Hard Rules
RULE 1: ONE LLM call per tick for tactical decisions.
RULE 2: Call Planning Agent for strategic decisions (see above).
RULE 3: Never write files directly — always atomic_write().
RULE 4: Never update DB directly — sync_to_db() via atomic_write().
RULE 5: Ledger before complete. LedgerWriteError = HALT.
RULE 6: entity.md input_name IMMUTABLE.
RULE 7: evidence/ev-{id}.md files IMMUTABLE after creation.
RULE 8: Never run during intake. Never before workspace exists.
