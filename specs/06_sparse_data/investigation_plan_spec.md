# Investigation Plan Specification

## Purpose
Per-candidate plan written by Planning Agent.
Written BEFORE execution starts.
Updated AS execution proceeds.
Finalised WITH outcome.

## Who Writes It
Planning Agent writes IP-{key}.md.
Orchestrator calls Planning Agent with SignalProfile.
Planning Agent returns InvestigationPlan dataclass AND writes file.

## Location
workspace/{programme_id}/programme_run/intake_plans/IP-{key}-{n}.md

## When It Is Written
ONLY for candidates that need investigation.
NOT written for:
  - Brain matches (auto-confirmed)
  - Persons/Internal (auto-discarded)
  - Auto-merge dedup candidates
  - Document-backed known vendors

## InvestigationPlan Dataclass
@dataclass
class InvestigationPlan:
    depth:              InvestigationDepth
    steps:              list[str]
    require_human_gate: bool
    require_legal_gate: bool
    fraud_risk:         FraudRisk
    resolving_question: str | None
    reason:             str

class InvestigationDepth(str, Enum):
    SKIP      = "SKIP"
    FAST      = "FAST"
    STANDARD  = "STANDARD"
    DEEP      = "DEEP"
    INTENSIVE = "INTENSIVE"
    TRIAGE    = "TRIAGE"

## The 14 Rules (Planning Agent applies these)

R01: entity_type=PERSON → DISCARD immediately
R02: entity_type=INTERNAL → DISCARD immediately
R03: brain_hit.matched=True, match_type=KNOWN_VENDOR or ALIAS
     → depth=SKIP, steps=[FRAUD_CHECK_ASYNC]
R04: brain_hit.rebrand_match=True
     → depth=SKIP, steps=[CONFIRM_REBRAND, FRAUD_CHECK_ASYNC]
R05: dedup_result.status=AUTO_MERGE
     → depth=SKIP, steps=[MERGE_CANONICAL]
R06: dedup_result.status=CANDIDATE
     → depth=TRIAGE, steps=[ROUTE_TO_HUMAN]
     → resolving_question set
R07: country_hint in SANCTIONED_COUNTRIES
     → depth=max(current, INTENSIVE)
     → steps += [SANCTIONS_CHECK, LEGAL_GATE]
     → require_legal_gate=True
R08: script_type != LATIN
     → steps += [TRANSLITERATION, MULTILINGUAL_SEARCH]
R09: erp_spend > 100000 AND brain_hit=False
     → depth=max(current, DEEP)
     → steps += [WEB_RESEARCH_DEEP, FRAUD_CHECK_BASIC]
R10: 0 < erp_spend <= 100000 AND brain_hit=False
     → depth=max(current, STANDARD)
     → steps += [WEB_RESEARCH_STANDARD, FRAUD_CHECK_BASIC]
R11: erp_spend=None AND entity_type=COMPANY AND brain_hit=False
     → depth=max(current, FAST)
     → steps += [WEB_RESEARCH_FAST]
R12: ap_signal has ROUND_NUMBERS or THRESHOLD_AVOIDANCE or single_approver
     → depth=max(current, INTENSIVE)
     → steps += [FRAUD_CHECK_DEEP, HR_OVERLAP_CHECK]
     → fraud_risk=max(current, HIGH)
R13: erp_spend > 10000 AND brain_hit=False AND invoice_count > 5
     → steps += [FRAUD_CHECK_DEEP]
     → fraud_risk=max(current, MEDIUM)
R14: linked_documents exist AND brain_hit=False
     → steps += [DOCUMENT_EXTRACTION]
     (document extraction on unknown vendor with contract)

## Step Order
TRANSLITERATION first, then MULTILINGUAL_SEARCH,
then WEB_RESEARCH_*, then DOCUMENT_EXTRACTION,
then SANCTIONS_CHECK, then FRAUD_CHECK_*,
then HR_OVERLAP_CHECK, then MERGE/CONFIRM/ROUTE,
then LEGAL_GATE, then FRAUD_CHECK_ASYNC last.

## IP File Structure
intake_plan_id:   IP-{key}-001
programme_id:     prog-nova-2026
candidate_raw:    "Blackboard Inc."
candidate_key:    blackboard

signals:
  brain_hit: {matched, confidence, match_type, rebrand_match, ...}
  dedup_result: {status, match_key, similarity}
  entity_type: COMPANY
  erp_signal: {exists, spend, category}
  linked_documents: [doc_id1, doc_id2]

plan:
  depth: SKIP
  steps: [CONFIRM_REBRAND, FRAUD_CHECK_ASYNC]
  reason: "Known rebrand in rebrand_map"

execution:
  started_at: null
  step_results: {}

outcome:
  status: PENDING
  vendor_id: null
