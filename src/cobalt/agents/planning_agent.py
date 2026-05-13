"""Planning Agent — writes every plan in the system.

Rules engine lives inside this class. Mode 1 (rules) for clear cases.
Mode 2 (ONE LLM call) for conflicting signals.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from cobalt.core.exceptions import ReplanLLMError, WorkflowCreationError
from cobalt.core.llm_call import ToolCallResponse, llm_call, llm_tool_call
import cobalt.workspace.plan_writer as _pw
from cobalt.models.schemas.campaign_plan_schema import (
    CampaignPlan,
    CampaignPlanInput,
    CampaignSpec,
)
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
from cobalt.runtime.execution_state import ExecutionState
from cobalt.runtime.runtime_engine import ReplanDecision
from cobalt.runtime.workflow_definition import (
    RetryPolicy,
    StepStatus,
    WorkflowDefinition,
    WorkflowStep,
)

logger = logging.getLogger(__name__)

_SANCTIONED_COUNTRIES = {
    "CU", "IR", "KP", "RU", "SY", "BY", "MM",
    "VE", "ZW", "SD", "SS", "LY", "SO", "YE",
    "CF", "CD", "ML", "NI",
}

_DEPTH_ORDER = {
    "SKIP": 0, "FAST": 1, "STANDARD": 2,
    "DEEP": 3, "INTENSIVE": 4, "TRIAGE": 5,
}
_FRAUD_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

_STEP_ORDER = [
    "TRANSLITERATION", "MULTILINGUAL_SEARCH",
    "WEB_RESEARCH_FAST", "WEB_RESEARCH_STANDARD",
    "WEB_RESEARCH_DEEP", "DOCUMENT_EXTRACTION",
    "SANCTIONS_CHECK", "FRAUD_CHECK_BASIC",
    "FRAUD_CHECK_DEEP", "HR_OVERLAP_CHECK",
    "MERGE_CANONICAL", "CONFIRM_REBRAND",
    "CONFIRM_ALIAS", "ROUTE_TO_HUMAN",
    "LEGAL_GATE", "FRAUD_CHECK_ASYNC",
]

_CONNECTOR_CAMPAIGN_TYPES = {
    "ERP_INTEGRATION", "CONTRACT_DISCOVERY",
    "SSO_EXTRACTION", "VENDOR_ADMIN_PULL",
}
_CHECKIN_CAMPAIGN_TYPES = {
    "OWNER_CHECKIN", "MVC_CHECKIN", "CRITICALITY_SURVEY",
}

_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "compute_investigation_plan",
            "description": "Compute an investigation plan for a vendor using signal profile fields.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vendor_name": {"type": "string"},
                    "entity_type": {
                        "type": "string",
                        "enum": ["COMPANY", "PERSON", "INTERNAL", "AMBIGUOUS"],
                    },
                    "script_type": {
                        "type": "string",
                        "enum": ["LATIN", "ARABIC", "CJK", "CYRILLIC", "OTHER"],
                    },
                    "country_hint": {"type": "string"},
                    "brain_hit_matched": {"type": "boolean"},
                    "brain_hit_confidence": {"type": "number"},
                    "rebrand_match": {"type": "boolean"},
                    "dedup_status": {"type": "string"},
                    "dedup_match_name": {"type": "string"},
                    "erp_spend": {"type": "number"},
                    "invoice_count": {"type": "integer"},
                    "invoice_flags": {"type": "array", "items": {"type": "string"}},
                    "single_approver": {"type": "boolean"},
                    "linked_doc_count": {"type": "integer"},
                },
                "required": ["vendor_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_programme_plan",
            "description": "Write a programme plan .md file for a programme run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "programme_id": {"type": "string"},
                    "candidate_count": {"type": "integer"},
                    "doc_count": {"type": "integer"},
                },
                "required": ["programme_id"],
            },
        },
    },
]

_SYSTEM = (
    "You are the Planning Agent for the Cobalt vendor intelligence platform. "
    "Write investigation and programme plans based on vendor data. "
    "Use the available tools to compute and write plans, then return a text summary."
)

_MAX_ITER = 4


class PlanningAgent:

    def _max_depth(self, a: str, b: str) -> str:
        return a if _DEPTH_ORDER.get(a, 0) >= _DEPTH_ORDER.get(b, 0) else b

    def _max_fraud(self, a: str, b: str) -> str:
        return a if _FRAUD_ORDER.get(a, 0) >= _FRAUD_ORDER.get(b, 0) else b

    # ------------------------------------------------------------------
    # Public: compute_investigation_plan
    # ------------------------------------------------------------------

    def compute_investigation_plan(self, profile: SignalProfile) -> InvestigationPlan:
        """Try rules first; fall back to LLM only for genuinely conflicting signals."""
        plan = self._rules_engine(profile)
        if plan is not None:
            return plan
        return self._llm_plan(profile)

    # ------------------------------------------------------------------
    # Public: compute_campaign_plan
    # ------------------------------------------------------------------

    def compute_campaign_plan(self, input: CampaignPlanInput) -> CampaignPlan:
        """Map blocking gaps to ordered CampaignSpec list."""
        seen: set[str] = set()
        specs: list[CampaignSpec] = []

        def _add(
            campaign_type: str,
            goal: str,
            resolves: list[str],
            condition: str,
            pcs_tgt: int,
            stakeholder: str | None = None,
            gates: list[str] | None = None,
        ) -> None:
            if campaign_type in seen:
                return
            seen.add(campaign_type)
            priority = 1 if campaign_type in _CONNECTOR_CAMPAIGN_TYPES else 2
            specs.append(CampaignSpec(
                campaign_type=campaign_type,
                goal=goal,
                resolves_gaps=resolves,
                completion_condition=condition,
                pcs_target=pcs_tgt,
                stakeholder=stakeholder,
                gates_required=gates or [],
                priority=priority,
            ))

        pcs_base = input.pcs

        for gap in input.blocking_gaps:
            if gap == "annual_spend":
                if input.erp_configured:
                    _add("ERP_INTEGRATION", "Extract spend from ERP system",
                         ["annual_spend"], "spend_confirmed", pcs_base + 5)
                else:
                    _add("MVC_CHECKIN", "Confirm annual spend via vendor check-in",
                         ["annual_spend"], "spend_confirmed", pcs_base + 5)

            elif gap == "renewal_date" or gap.startswith("contract_"):
                if input.clm_configured:
                    _add("CONTRACT_DISCOVERY", "Extract contract data from CLM",
                         ["renewal_date", "contract_value"], "contract_identified", pcs_base + 5)
                else:
                    _add("OWNER_CHECKIN", "Identify contract details via owner check-in",
                         ["renewal_date"], "owner_identified", pcs_base + 3)

            elif gap == "utilisation" or gap.startswith("dormancy"):
                if input.sso_configured:
                    _add("SSO_EXTRACTION", "Extract utilisation data from SSO",
                         ["utilisation"], "utilisation_confirmed", pcs_base + 5)
                else:
                    _add("VENDOR_ADMIN_PULL", "Pull utilisation from vendor admin portal",
                         ["utilisation"], "utilisation_confirmed", pcs_base + 5)

            elif gap in ("stakeholder", "owner"):
                _add("OWNER_CHECKIN", "Identify vendor owner and stakeholder",
                     ["stakeholder", "owner"], "owner_identified", pcs_base + 3)

            elif gap == "baa_present":
                _add("OWNER_CHECKIN", "Confirm BAA status with vendor owner",
                     ["baa_present"], "baa_confirmed", pcs_base + 3)

        specs.sort(key=lambda s: s.priority)

        return CampaignPlan(
            vendor_id=input.vendor_id,
            campaigns=specs,
            reason="gap-to-campaign mapping",
            planned_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------
    # Public: compute_programme_plan
    # ------------------------------------------------------------------

    def compute_programme_plan(self, source_summary: dict) -> dict:
        """Return a programme-level plan dict from source summary."""
        doc_count = source_summary.get("doc_count", 0)
        return {
            "research_tier": "STANDARD",
            "document_extraction_enabled": doc_count > 0,
            "expected_distribution": {
                "FAST": 0.40,
                "STANDARD": 0.35,
                "DEEP": 0.15,
                "INTENSIVE": 0.05,
                "SKIP": 0.05,
            },
            "planned_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Public: write_programme_plan
    # ------------------------------------------------------------------

    def write_programme_plan(self, programme_id: str, source_summary: dict) -> Path:
        """Generate strategy prose via LLM, write programme_plan.md. Returns Path."""
        logger.info("Generating strategy prose for programme %r ...", programme_id)
        strategy_prose = self._generate_strategy_prose(source_summary)
        plan_context = {
            "research_tier": "STANDARD",
            "document_extraction_enabled": source_summary.get("doc_count", 0) > 0,
            "strategy_prose": strategy_prose,
            "source_summary": source_summary,
            "expected_distribution": {
                "FAST": 0.40, "STANDARD": 0.35,
                "DEEP": 0.15, "INTENSIVE": 0.05, "SKIP": 0.05,
            },
            "planned_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        path = _pw.write_programme_plan(programme_id, plan_context)
        logger.info("Programme plan written → %s", path)
        return path

    # ------------------------------------------------------------------
    # Public: write_investigation_plan
    # ------------------------------------------------------------------

    def write_investigation_plan(
        self,
        programme_id: str,
        candidate_key: str,
        profile: SignalProfile,
    ) -> tuple[Path | None, "InvestigationPlan"]:
        """Compute plan via rules engine, generate why-prose via LLM, write IP .md.

        Returns (path, plan).  Path is None when depth == SKIP (no file written).
        """
        plan = self.compute_investigation_plan(profile)
        logger.info(
            "Plan for %r → depth=%s  steps=%s  fraud=%s",
            candidate_key, plan.depth.value, plan.steps, plan.fraud_risk.value,
        )

        if plan.depth == InvestigationDepth.SKIP:
            logger.info("Plan depth=SKIP — no investigation file written for %r", candidate_key)
            return None, plan

        # LLM enrichment: add per-step rationale, focus, optional step escalation
        plan = self._enrich_with_llm(profile, plan)
        path = _pw.write_investigation_plan(programme_id, candidate_key, profile, plan)
        logger.info("Investigation plan written → %s", path)
        return path, plan

    # ------------------------------------------------------------------
    # Public: run — open-ended agentic loop
    # ------------------------------------------------------------------

    def run(self, task: str, context: str = "") -> str:
        """Execute an open-ended planning task via LLM tool loop. Never raises."""
        content = f"{task}\n\nContext:\n{context}" if context else task
        messages: list[dict] = [{"role": "user", "content": content}]

        for _ in range(_MAX_ITER):
            try:
                response = llm_tool_call(messages, _TOOLS, system=_SYSTEM)
            except Exception as exc:
                logger.warning("PlanningAgent.run: llm_tool_call failed: %s", exc)
                return ""

            if response.type == "text":
                return response.content or ""

            result_text = self._dispatch(response.tool_name or "", response.tool_args or {})
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": response.tool_call_id or "call_0",
                    "type": "function",
                    "function": {
                        "name": response.tool_name or "",
                        "arguments": json.dumps(response.tool_args or {}),
                    },
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": response.tool_call_id or "call_0",
                "content": result_text,
            })

        return ""

    def _dispatch(self, name: str, args: dict) -> str:
        """Route a planning tool call by name. Returns a string result."""
        try:
            if name == "compute_investigation_plan":
                vendor = args.get("vendor_name", "unknown")
                profile = SignalProfile(
                    raw=vendor,
                    cleaned=vendor,
                    script_type=ScriptType(args.get("script_type", "LATIN")),
                    country_hint=args.get("country_hint"),
                    normalized=vendor.lower(),
                    comparison_key=vendor.lower(),
                    brain_hit=BrainHit(
                        matched=bool(args.get("brain_hit_matched", False)),
                        confidence=float(args.get("brain_hit_confidence", 0.0)),
                        canonical=None,
                        match_type=None,
                        rebrand_match=bool(args.get("rebrand_match", False)),
                        rebrand_target=None,
                        alias_match=False,
                        alias_target=None,
                    ),
                    erp_signal=ErpSignal(
                        exists=args.get("erp_spend") is not None,
                        spend=args.get("erp_spend"),
                        category=None,
                        vendor_ids=[],
                    ),
                    ap_signal=ApSignal(
                        invoice_count=int(args.get("invoice_count", 0)),
                        flags=list(args.get("invoice_flags", [])),
                        single_approver=bool(args.get("single_approver", False)),
                    ),
                    linked_doc_ids=["placeholder"] * int(args.get("linked_doc_count", 0)),
                    spend_hint=None,
                    category_hint=None,
                    entity_type=EntityType(args.get("entity_type", "COMPANY")),
                    dedup_result=DedupResult(
                        status=args.get("dedup_status", "UNIQUE"),
                        match_key=None,
                        match_name=args.get("dedup_match_name"),
                        similarity=0.0,
                    ),
                )
                plan = self.compute_investigation_plan(profile)
                return json.dumps({
                    "depth": plan.depth.value,
                    "steps": plan.steps,
                    "reason": plan.reason,
                    "require_human_gate": plan.require_human_gate,
                    "require_legal_gate": plan.require_legal_gate,
                })

            if name == "write_programme_plan":
                source_summary = {
                    "candidates_total": int(args.get("candidate_count", 0)),
                    "doc_count": int(args.get("doc_count", 0)),
                    "from_list_only": int(args.get("candidate_count", 0)),
                    "from_docs_only": 0,
                }
                path = self.write_programme_plan(args["programme_id"], source_summary)
                return str(path)

            logger.warning("PlanningAgent._dispatch: unknown tool %r", name)
            return f"unknown tool: {name}"
        except Exception as exc:
            logger.warning("PlanningAgent._dispatch failed for %r: %s", name, exc)
            return f"error: {exc}"

    # ------------------------------------------------------------------
    # Private: LLM prose generators
    # ------------------------------------------------------------------

    def _generate_strategy_prose(self, source_summary: dict) -> str:
        """ONE LLM call — write the programme-level strategy paragraph. Never raises."""
        vendor_count = source_summary.get("candidates_total", 0)
        doc_count    = source_summary.get("doc_count", 0)

        system = (
            "You are the Planning Agent for the Cobalt vendor intelligence platform. "
            "Write 2-3 sentences describing the intake strategy for this programme. "
            "Mention vendor count, document coverage, and expected investigation mix. "
            "Be specific and practical. Plain prose — no headers, no bullets."
        )
        prompt = (
            f"Source summary:\n"
            f"- Total vendors: {vendor_count}\n"
            f"- Documents: {doc_count}\n"
            f"- From list only: {source_summary.get('from_list_only', vendor_count)}\n"
            f"- From docs only: {source_summary.get('from_docs_only', 0)}\n\n"
            "Describe the intake strategy in 2-3 sentences."
        )
        try:
            response = llm_call(prompt, system, expect_json=False)
            return response.strip() if isinstance(response, str) else str(response)
        except Exception:
            doc_note = "Document extraction enabled." if doc_count > 0 else "No documents provided."
            return f"Standard intake run for {vendor_count} vendors. {doc_note}"

    def _generate_why_prose(self, profile: SignalProfile, plan: "InvestigationPlan") -> str:
        """ONE LLM call — explain why this investigation plan was chosen. Never raises."""
        depth_val = plan.depth.value if hasattr(plan.depth, "value") else str(plan.depth)
        etype_val = (
            profile.entity_type.value
            if hasattr(profile.entity_type, "value")
            else str(profile.entity_type)
        )

        system = (
            "You are the Planning Agent for the Cobalt vendor intelligence platform. "
            "Write 2-3 sentences explaining WHY this investigation plan was chosen. "
            "Reference specific signals: spend level, entity type, brain hit status, "
            "country, fraud flags, linked documents. "
            "Write for a compliance analyst. Plain prose — no headers, no bullets."
        )
        prompt = (
            f"Vendor: {profile.raw!r}\n"
            f"Entity type: {etype_val}\n"
            f"Brain hit: {profile.brain_hit.matched} "
            f"(confidence {profile.brain_hit.confidence:.0%})\n"
            f"ERP spend: {profile.erp_signal.spend}\n"
            f"Country: {profile.country_hint}\n"
            f"Dedup status: {profile.dedup_result.status}\n"
            f"Linked docs: {len(profile.linked_doc_ids)}\n"
            f"Depth chosen: {depth_val}\n"
            f"Steps chosen: {plan.steps}\n"
            f"Reason: {plan.reason}\n\n"
            "Explain in 2-3 sentences why this plan was chosen."
        )
        try:
            response = llm_call(prompt, system, expect_json=False)
            return response.strip() if isinstance(response, str) else str(response)
        except Exception:
            return plan.reason or "Standard investigation."

    # ------------------------------------------------------------------
    # Private: _enrich_with_llm
    # ------------------------------------------------------------------

    def _enrich_with_llm(self, profile: SignalProfile, plan: "InvestigationPlan") -> "InvestigationPlan":
        """ONE LLM call: confirm/escalate steps, add per-step rationale, write focus.

        Rules always fire first and guarantee minimum correct steps.  This call
        gives the LLM full signal context so it can:
          - Write a specific rationale for each step (what to look for and why)
          - Write a focus statement (the single key question the investigation answers)
          - Optionally add steps the rules missed given the full context
          - Optionally escalate depth or fraud_risk if signals warrant it

        Failure is fully safe — original rules plan is returned unchanged.
        """
        depth_val  = plan.depth.value if hasattr(plan.depth, "value") else str(plan.depth)
        fraud_val  = plan.fraud_risk.value if hasattr(plan.fraud_risk, "value") else str(plan.fraud_risk)
        spend_str  = f"${float(profile.erp_signal.spend):,.0f}" if profile.erp_signal.spend else "no spend data"
        flags_str  = ", ".join(profile.ap_signal.flags) if profile.ap_signal.flags else "none"
        etype_val  = profile.entity_type.value if hasattr(profile.entity_type, "value") else str(profile.entity_type)

        system = (
            "You are the Cobalt Planning Agent. You enrich rule-generated investigation plans "
            "for unknown vendors using all available signals.\n\n"
            "Your job:\n"
            "  1. Write a specific step_rationale for each rule-selected step — what to look "
            "for and why it matters for THIS vendor given its signals.\n"
            "  2. Write a focus — the single most important question this investigation answers.\n"
            "  3. Optionally add steps if signals justify it (never remove rule-selected steps).\n"
            "  4. Optionally escalate depth or fraud_risk if signals warrant it.\n\n"
            "Allowed steps:\n"
            "  WEB_RESEARCH_FAST, WEB_RESEARCH_STANDARD, WEB_RESEARCH_DEEP,\n"
            "  FRAUD_CHECK_BASIC, FRAUD_CHECK_DEEP, FRAUD_CHECK_ASYNC,\n"
            "  SANCTIONS_CHECK, HR_OVERLAP_CHECK, DOCUMENT_EXTRACTION,\n"
            "  TRANSLITERATION, MULTILINGUAL_SEARCH, LEGAL_GATE,\n"
            "  MERGE_CANONICAL, CONFIRM_REBRAND, ROUTE_TO_HUMAN\n\n"
            "Return ONLY valid JSON — no markdown, no explanation outside JSON."
        )

        prompt = (
            f"Enrich this investigation plan for vendor: {profile.raw!r}\n\n"
            f"=== VENDOR IDENTITY ===\n"
            f"Normalized:       {profile.normalized!r}\n"
            f"Entity type:      {etype_val}\n"
            f"Script:           {profile.script_type.value}\n"
            f"Country hint:     {profile.country_hint or 'unknown'}\n\n"
            f"=== BRAIN KNOWLEDGE ===\n"
            f"Brain matched:    {profile.brain_hit.matched} ({profile.brain_hit.confidence:.0%} confidence)\n"
            f"Canonical:        {profile.brain_hit.canonical or 'none'}\n"
            f"Match type:       {profile.brain_hit.match_type or 'none'}\n"
            f"Rebrand:          {profile.brain_hit.rebrand_match}\n\n"
            f"=== FINANCIAL SIGNALS ===\n"
            f"ERP spend:        {spend_str}\n"
            f"Invoice count:    {profile.ap_signal.invoice_count}\n"
            f"AP flags:         {flags_str}\n"
            f"Single approver:  {profile.ap_signal.single_approver}\n\n"
            f"=== DOCUMENT SIGNALS ===\n"
            f"Linked docs:      {len(profile.linked_doc_ids)}\n\n"
            f"=== RULE-GENERATED PLAN ===\n"
            f"Depth:            {depth_val}\n"
            f"Steps selected:   {plan.steps}\n"
            f"Rule reason:      {plan.reason}\n\n"
            "Return JSON:\n"
            "{\n"
            '  "steps": ["STEP_1", ...],\n'
            '  "step_rationale": {\n'
            '    "STEP_1": "What specifically to look for in this step and why"\n'
            '  },\n'
            '  "focus": "The single most important question this investigation must answer",\n'
            '  "depth": "FAST|STANDARD|DEEP|INTENSIVE",\n'
            '  "fraud_risk": "LOW|MEDIUM|HIGH|CRITICAL"\n'
            "}"
        )

        try:
            response = llm_call(prompt, system, expect_json=True)
            assert isinstance(response, dict)

            # LLM may add steps — merge with rule steps, never dropping rule steps
            valid_steps = set(_STEP_ORDER)
            llm_steps = [s for s in response.get("steps", []) if s in valid_steps]
            extra = [s for s in llm_steps if s not in plan.steps]
            merged = plan.steps + extra

            # Sort by canonical step order
            step_rank = {s: i for i, s in enumerate(_STEP_ORDER)}
            ordered = sorted(merged, key=lambda s: step_rank.get(s, len(_STEP_ORDER)))

            new_depth = self._max_depth(depth_val, response.get("depth", depth_val))
            new_fraud = self._max_fraud(fraud_val, response.get("fraud_risk", fraud_val))

            logger.info(
                "_enrich_with_llm: %r → depth=%s fraud=%s focus=%r steps=%s",
                profile.raw, new_depth, new_fraud,
                response.get("focus", "")[:60], ordered,
            )

            return InvestigationPlan(
                depth=InvestigationDepth(new_depth),
                steps=ordered,
                require_human_gate=plan.require_human_gate,
                require_legal_gate=plan.require_legal_gate,
                fraud_risk=FraudRisk(new_fraud),
                resolving_question=plan.resolving_question,
                reason=plan.reason,
                step_rationale=response.get("step_rationale", {}),
                focus=response.get("focus", ""),
            )
        except Exception as exc:
            logger.warning("_enrich_with_llm failed for %r — using rule plan: %s", profile.raw, exc)
            return plan

    # ------------------------------------------------------------------
    # Private: _rules_engine
    # ------------------------------------------------------------------

    def _rules_engine(self, profile: SignalProfile) -> InvestigationPlan | None:
        """Evaluate R01-R14. R01-R06 exit early; R07-R14 accumulate.

        Returns None when no accumulative rule fires (caller uses LLM).
        """
        # ---- R01 ----
        if profile.entity_type == EntityType.PERSON:
            return InvestigationPlan(
                depth=InvestigationDepth.SKIP,
                steps=[],
                require_human_gate=False,
                require_legal_gate=False,
                fraud_risk=FraudRisk.LOW,
                resolving_question=None,
                reason="Person name — discard",
            )

        # ---- R02 ----
        if profile.entity_type == EntityType.INTERNAL:
            return InvestigationPlan(
                depth=InvestigationDepth.SKIP,
                steps=[],
                require_human_gate=False,
                require_legal_gate=False,
                fraud_risk=FraudRisk.LOW,
                resolving_question=None,
                reason="Internal entity — discard",
            )

        # ---- R03 ----
        if (
            profile.brain_hit.matched
            and profile.brain_hit.match_type in ("KNOWN_VENDOR", "ALIAS")
        ):
            return InvestigationPlan(
                depth=InvestigationDepth.SKIP,
                steps=["FRAUD_CHECK_ASYNC"],
                require_human_gate=False,
                require_legal_gate=False,
                fraud_risk=FraudRisk.LOW,
                resolving_question=None,
                reason="Known vendor confirmed in Brain",
            )

        # ---- R04 ----
        if profile.brain_hit.rebrand_match:
            return InvestigationPlan(
                depth=InvestigationDepth.SKIP,
                steps=["CONFIRM_REBRAND", "FRAUD_CHECK_ASYNC"],
                require_human_gate=False,
                require_legal_gate=False,
                fraud_risk=FraudRisk.LOW,
                resolving_question=None,
                reason="Known rebrand in rebrand_map",
            )

        # ---- R05 ----
        if profile.dedup_result.status == "AUTO_MERGE":
            return InvestigationPlan(
                depth=InvestigationDepth.SKIP,
                steps=["MERGE_CANONICAL"],
                require_human_gate=False,
                require_legal_gate=False,
                fraud_risk=FraudRisk.LOW,
                resolving_question=None,
                reason=f"Duplicate of '{profile.dedup_result.match_name}'",
            )

        # ---- R06 ----
        if profile.dedup_result.status == "CANDIDATE":
            return InvestigationPlan(
                depth=InvestigationDepth.TRIAGE,
                steps=["ROUTE_TO_HUMAN"],
                require_human_gate=True,
                require_legal_gate=False,
                fraud_risk=FraudRisk.LOW,
                resolving_question=(
                    f"Are '{profile.normalized}' and "
                    f"'{profile.dedup_result.match_name}' "
                    f"the same vendor?"
                ),
                reason="Dedup candidate — needs human confirmation",
            )

        # ---- R07-R14: accumulate ----
        depth = "FAST"
        steps: list[str] = []
        fraud_risk = "LOW"
        require_human_gate = False
        require_legal_gate = False
        reason_parts: list[str] = []

        # R07
        if profile.country_hint in _SANCTIONED_COUNTRIES:
            depth = self._max_depth(depth, "INTENSIVE")
            steps += ["SANCTIONS_CHECK", "LEGAL_GATE"]
            require_legal_gate = True
            reason_parts.append("sanctioned country")

        # R08
        if profile.script_type != ScriptType.LATIN:
            steps += ["TRANSLITERATION", "MULTILINGUAL_SEARCH"]
            reason_parts.append("non-Latin script")

        # R09
        if (
            profile.erp_signal.spend is not None
            and profile.erp_signal.spend > 100000
            and not profile.brain_hit.matched
        ):
            depth = self._max_depth(depth, "DEEP")
            steps += ["WEB_RESEARCH_DEEP", "FRAUD_CHECK_BASIC"]
            reason_parts.append("high spend unknown vendor")

        # R10
        if (
            profile.erp_signal.spend is not None
            and 0 < profile.erp_signal.spend <= 100000
            and not profile.brain_hit.matched
        ):
            depth = self._max_depth(depth, "STANDARD")
            steps += ["WEB_RESEARCH_STANDARD", "FRAUD_CHECK_BASIC"]
            reason_parts.append("medium spend unknown vendor")

        # R11
        if (
            (profile.erp_signal.spend is None or profile.erp_signal.spend == 0)
            and profile.entity_type in (EntityType.COMPANY, EntityType.AMBIGUOUS)
            and not profile.brain_hit.matched
        ):
            depth = self._max_depth(depth, "FAST")
            steps += ["WEB_RESEARCH_FAST"]
            reason_parts.append("unknown vendor no spend")

        # R12
        if (
            "ROUND_NUMBERS" in profile.ap_signal.flags
            or "THRESHOLD_AVOIDANCE" in profile.ap_signal.flags
            or profile.ap_signal.single_approver
        ):
            depth = self._max_depth(depth, "INTENSIVE")
            steps += ["FRAUD_CHECK_DEEP", "HR_OVERLAP_CHECK"]
            fraud_risk = self._max_fraud(fraud_risk, "MEDIUM")
            reason_parts.append("invoice fraud patterns")

        # R13
        if (
            profile.erp_signal.spend is not None
            and profile.erp_signal.spend > 10000
            and not profile.brain_hit.matched
            and profile.ap_signal.invoice_count > 5
        ):
            steps += ["FRAUD_CHECK_DEEP"]
            fraud_risk = self._max_fraud(fraud_risk, "MEDIUM")
            reason_parts.append("shell company signals")

        # R14
        if profile.linked_doc_ids and not profile.brain_hit.matched:
            steps += ["DOCUMENT_EXTRACTION"]
            reason_parts.append("document available for unknown vendor")

        # No accumulative rule fired — let LLM handle conflicting signals
        if not reason_parts:
            return None

        # Deduplicate preserving insertion order, then sort by canonical step order
        seen_steps: set[str] = set()
        unique: list[str] = []
        for s in steps:
            if s not in seen_steps:
                seen_steps.add(s)
                unique.append(s)

        step_rank = {s: i for i, s in enumerate(_STEP_ORDER)}
        ordered = sorted(unique, key=lambda s: step_rank.get(s, len(_STEP_ORDER)))

        return InvestigationPlan(
            depth=InvestigationDepth(depth),
            steps=ordered,
            require_human_gate=require_human_gate,
            require_legal_gate=require_legal_gate,
            fraud_risk=FraudRisk(fraud_risk),
            resolving_question=None,
            reason=", ".join(reason_parts),
        )

    # ------------------------------------------------------------------
    # Private: _llm_plan
    # ------------------------------------------------------------------

    def _llm_plan(self, profile: SignalProfile) -> InvestigationPlan:
        """Called only when _rules_engine returns None. ONE llm_call. Never raises."""
        system = (
            "You are the Planning Agent for the Cobalt platform.\n"
            "You produce executable investigation plans from vendor signal profiles.\n\n"
            "Available investigation tools:\n"
            "  WEB_RESEARCH_FAST        1 Brave search query\n"
            "  WEB_RESEARCH_STANDARD    1 richer query\n"
            "  WEB_RESEARCH_DEEP        2 queries + entity structuring\n"
            "  DOCUMENT_EXTRACTION      extract terms from linked PDFs\n"
            "  FRAUD_CHECK_BASIC        shell signals, invoice patterns\n"
            "  FRAUD_CHECK_DEEP         full fraud including HR overlap\n"
            "  SANCTIONS_CHECK          OFAC/EU/UN check (stub in V1)\n"
            "  HR_OVERLAP_CHECK         employee cross-reference (stub)\n"
            "  MERGE_CANONICAL          confirmed duplicate → merge\n"
            "  CONFIRM_REBRAND          confirmed rebrand → update\n"
            "  ROUTE_TO_HUMAN           cannot resolve → triage\n"
            "  LEGAL_GATE               sanctions → compliance review\n"
            "  FRAUD_CHECK_ASYNC        background fraud for known vendors\n\n"
            "Rules: select minimum tools necessary. Never duplicate tools. "
            "Order tools correctly. Return ONLY valid JSON."
        )

        prompt = (
            f"Produce an investigation plan for this vendor signal profile.\n\n"
            f"raw: {profile.raw!r}\n"
            f"entity_type: {profile.entity_type.value}\n"
            f"script_type: {profile.script_type.value}\n"
            f"country_hint: {profile.country_hint!r}\n"
            f"brain_hit.matched: {profile.brain_hit.matched}\n"
            f"brain_hit.confidence: {profile.brain_hit.confidence}\n"
            f"erp_signal.spend: {profile.erp_signal.spend}\n"
            f"ap_signal.invoice_count: {profile.ap_signal.invoice_count}\n"
            f"ap_signal.flags: {profile.ap_signal.flags!r}\n"
            f"ap_signal.single_approver: {profile.ap_signal.single_approver}\n"
            f"linked_doc_ids: {profile.linked_doc_ids!r}\n"
            f"dedup_result.status: {profile.dedup_result.status!r}\n\n"
            'Return JSON only:\n'
            '{\n'
            '  "depth": "SKIP|FAST|STANDARD|DEEP|INTENSIVE|TRIAGE",\n'
            '  "steps": ["TOOL_1", "TOOL_2"],\n'
            '  "reason": "why this path",\n'
            '  "require_human_gate": false,\n'
            '  "require_legal_gate": false,\n'
            '  "fraud_risk": "LOW|MEDIUM|HIGH|CRITICAL",\n'
            '  "resolving_question": null\n'
            '}'
        )

        try:
            response = llm_call(prompt, system, expect_json=True)
            assert isinstance(response, dict)
            return InvestigationPlan(
                depth=InvestigationDepth(response.get("depth", "STANDARD")),
                steps=response.get("steps", []),
                require_human_gate=bool(response.get("require_human_gate", False)),
                require_legal_gate=bool(response.get("require_legal_gate", False)),
                fraud_risk=FraudRisk(response.get("fraud_risk", "LOW")),
                resolving_question=response.get("resolving_question"),
                reason=response.get("reason", "LLM plan"),
            )
        except Exception:
            logger.warning("LLM plan failed for %r — using STANDARD fallback", profile.raw)
            return InvestigationPlan(
                depth=InvestigationDepth.STANDARD,
                steps=["WEB_RESEARCH_STANDARD", "FRAUD_CHECK_BASIC"],
                require_human_gate=False,
                require_legal_gate=False,
                fraud_risk=FraudRisk.LOW,
                resolving_question=None,
                reason="LLM fallback — standard check",
            )


# ---------------------------------------------------------------------------
# Era 2 constants (module-level — used by create_workflow and replan)
# ---------------------------------------------------------------------------

_RETRYABLE_STEP_TYPES: frozenset[str] = frozenset({
    "WEB_RESEARCH_FAST", "WEB_RESEARCH_STANDARD", "WEB_RESEARCH_DEEP",
    "DOCUMENT_EXTRACTION", "COLLECT_SOURCES",
})

_ENRICHMENT_STEPS = [
    ("s1", "COLLECT_SOURCES",    [],           "Gather raw evidence from web, registry, and news sources."),
    ("s2", "EXTRACT_ATTRIBUTES", ["s1"],        "Extract structured vendor attributes from collected evidence."),
    ("s3", "MAP_RELATIONSHIPS",  ["s1"],        "Map parent/subsidiary relationships and detect rebrands or acquisitions."),
    ("s4", "CREATE_PROFILE",     ["s2", "s3"], "Reconcile all extracted data into a complete vendor profile."),
]

_STEP_RATIONALE: dict[str, str] = {
    "WEB_RESEARCH_FAST":     "Quick web search to confirm vendor identity and basic details.",
    "WEB_RESEARCH_STANDARD": "Standard web research to establish vendor legitimacy and coverage.",
    "WEB_RESEARCH_DEEP":     "Deep web research for high-spend or unknown vendors requiring thorough validation.",
    "DOCUMENT_EXTRACTION":   "Extract contract terms and vendor details from linked documents.",
    "FRAUD_CHECK_BASIC":     "Basic fraud screening for shell company and invoice patterns.",
    "FRAUD_CHECK_DEEP":      "Deep fraud investigation for elevated risk signals.",
    "FRAUD_CHECK_ASYNC":     "Background fraud check for known vendors.",
    "SANCTIONS_CHECK":       "Sanctions screening required for vendor in monitored jurisdiction.",
    "HR_OVERLAP_CHECK":      "Check for employee-vendor conflicts of interest.",
    "MERGE_CANONICAL":       "Confirmed duplicate — merge into canonical vendor record.",
    "CONFIRM_REBRAND":       "Known rebrand — update canonical vendor record.",
    "CONFIRM_ALIAS":         "Confirm this is a known alias of an existing vendor.",
    "ROUTE_TO_HUMAN":        "Ambiguous or conflicting signals — route to human review.",
    "LEGAL_GATE":            "Requires legal review before proceeding.",
    "TRANSLITERATION":       "Non-Latin script — transliterate for matching.",
    "MULTILINGUAL_SEARCH":   "Non-Latin vendor — perform multilingual web search.",
    "COLLECT_SOURCES":       "Gather raw evidence from web, registry, and news sources.",
    "EXTRACT_ATTRIBUTES":    "Extract structured vendor attributes from collected evidence.",
    "MAP_RELATIONSHIPS":     "Map parent/subsidiary relationships and detect rebrands or acquisitions.",
    "CREATE_PROFILE":        "Reconcile all extracted data into a complete vendor profile.",
}

# Available step types for the LLM replan prompt.
_AVAILABLE_STEP_TYPES: list[str] = _STEP_ORDER + [
    "COLLECT_SOURCES", "EXTRACT_ATTRIBUTES", "MAP_RELATIONSHIPS", "CREATE_PROFILE",
]

_REPLAN_SYSTEM = (
    "You are a Planning Agent for the Cobalt vendor intelligence platform. "
    "You revise workflow plans when step results change the picture. You may "
    "only modify PENDING steps. You must preserve all DONE/FAILED/SKIPPED "
    "steps. Output ONLY JSON, no preamble."
)


# ---------------------------------------------------------------------------
# Era 2 methods — added to PlanningAgent
# ---------------------------------------------------------------------------

def _make_retry_policy(step_type: str) -> RetryPolicy:
    if step_type in _RETRYABLE_STEP_TYPES:
        return RetryPolicy(max_attempts=2, backoff_seconds=5)
    return RetryPolicy(max_attempts=1, backoff_seconds=0)


def _safe_key(s: str) -> str:
    """Replace non-alphanumeric chars with hyphens for use in workflow IDs."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _next_step_id(workflow: "WorkflowDefinition") -> int:
    """Return the next sequential step number from existing step IDs."""
    max_n = 0
    for step in workflow.steps:
        m = re.match(r"s(\d+)$", step.step_id)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


class _Era2Mixin:
    """Era 2 workflow methods injected into PlanningAgent."""

    # ------------------------------------------------------------------
    # create_workflow
    # ------------------------------------------------------------------

    def create_workflow(
        self,
        profile: "SignalProfile | None",
        workflow_type: str,
        programme_id: str,
        context_overrides: dict | None = None,
    ) -> WorkflowDefinition:
        """Build and persist a WorkflowDefinition.

        workflow_type must be 'INTAKE_INVESTIGATION' or 'ENRICHMENT'.
        Raises WorkflowCreationError for unsupported types or missing vendor_id.
        """
        if workflow_type == "INTAKE_INVESTIGATION":
            return self._create_intake_workflow(profile, programme_id, context_overrides)
        if workflow_type == "ENRICHMENT":
            return self._create_enrichment_workflow(programme_id, context_overrides)
        raise WorkflowCreationError(
            f"Unsupported workflow_type {workflow_type!r}. "
            "Supported: 'INTAKE_INVESTIGATION', 'ENRICHMENT'."
        )

    def _create_intake_workflow(
        self,
        profile: "SignalProfile",
        programme_id: str,
        context_overrides: dict | None,
    ) -> WorkflowDefinition:
        plan = self.compute_investigation_plan(profile)  # type: ignore[attr-defined]

        steps: list[WorkflowStep] = []
        for n, step_type in enumerate(plan.steps):
            step_id = f"s{n + 1}"
            depends_on = [f"s{n}"] if n > 0 else []
            steps.append(WorkflowStep(
                step_id=step_id,
                step_type=step_type,
                status=StepStatus.PENDING,
                depends_on=depends_on,
                condition=None,
                retry_policy=_make_retry_policy(step_type),
                planning_rationale=_STEP_RATIONALE.get(
                    step_type, f"Execute {step_type} for this vendor."
                ),
                added_in_version=1,
            ))

        context: dict = {
            "entity_type":          profile.entity_type.value,
            "brain_hit":            profile.brain_hit.matched,
            "fraud_risk":           plan.fraud_risk.value,
            "investigation_depth":  plan.depth.value,
            "planning_rationale":   plan.reason or "",
            "candidate_key":        profile.comparison_key,
            "input_name":           profile.raw,
        }
        if context_overrides:
            context.update(context_overrides)

        safe = _safe_key(profile.comparison_key)
        workflow_id = f"wf-intake-{safe}-{int(time.time())}"

        wf = WorkflowDefinition(
            workflow_id=workflow_id,
            programme_id=programme_id,
            vendor_key=profile.comparison_key,
            vendor_id=None,
            workflow_type="INTAKE_INVESTIGATION",
            created_by="planning_agent",
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            steps=steps,
            context=context,
        )
        wf.save()
        logger.info(  # type: ignore[attr-defined]
            "create_workflow: INTAKE_INVESTIGATION saved %s (%d steps)", workflow_id, len(steps)
        )
        return wf

    def _create_enrichment_workflow(
        self,
        programme_id: str,
        context_overrides: dict | None,
    ) -> WorkflowDefinition:
        vendor_id = (context_overrides or {}).get("vendor_id")
        if not vendor_id:
            raise WorkflowCreationError(
                "ENRICHMENT workflow requires 'vendor_id' in context_overrides."
            )

        steps: list[WorkflowStep] = [
            WorkflowStep(
                step_id=sid,
                step_type=stype,
                status=StepStatus.PENDING,
                depends_on=deps,
                condition=None,
                retry_policy=_make_retry_policy(stype),
                planning_rationale=rationale,
                added_in_version=1,
            )
            for sid, stype, deps, rationale in _ENRICHMENT_STEPS
        ]

        context: dict = {
            "workflow_purpose": "vendor_enrichment",
            "depth_tier":       (context_overrides or {}).get("depth_tier", "STANDARD"),
            "vendor_id":        vendor_id,
        }

        workflow_id = f"wf-enrich-{_safe_key(str(vendor_id))}-{int(time.time())}"

        wf = WorkflowDefinition(
            workflow_id=workflow_id,
            programme_id=programme_id,
            vendor_key=None,
            vendor_id=vendor_id,
            workflow_type="ENRICHMENT",
            created_by="planning_agent",
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            steps=steps,
            context=context,
        )
        wf.save()
        logger.info(  # type: ignore[attr-defined]
            "create_workflow: ENRICHMENT saved %s", workflow_id
        )
        return wf

    # ------------------------------------------------------------------
    # evaluate_step
    # ------------------------------------------------------------------

    def evaluate_step(
        self,
        step: WorkflowStep,
        result: dict,
        state: "ExecutionState",
    ) -> ReplanDecision:
        """Pure heuristic decision. No LLM. First match wins."""
        # 1 — early exit BLOCKED
        if result.get("early_exit") and result.get("exit_status") == "BLOCKED":
            return ReplanDecision(action="TERMINATE", reason="step_blocked", rationale="")

        # 2 — early exit TRIAGE_REQUIRED
        if result.get("early_exit") and result.get("exit_status") == "TRIAGE_REQUIRED":
            return ReplanDecision(action="ESCALATE_HUMAN", reason="triage_required", rationale="")

        reason: str | None = None
        rationale: str = ""

        # 3 — low confidence
        conf = result.get("confidence")
        if conf is not None and conf < 0.35:
            reason = "low_confidence"
            rationale = (
                f"Step {step.step_id} returned confidence {conf:.2f}, "
                "below 0.35 threshold"
            )

        # 4 — new fraud signals
        if reason is None:
            new_fraud = set(result.get("fraud_signals") or [])
            known_fraud = set(state.accumulated_signals.get("known_fraud_signals") or [])
            novel = new_fraud - known_fraud
            if novel:
                reason = "new_fraud_signals"
                rationale = (
                    f"Step {step.step_id} discovered new fraud signals: "
                    f"{sorted(novel)}"
                )

        # 5 — entity ambiguity
        if reason is None and result.get("multiple_matches"):
            reason = "entity_ambiguity"
            rationale = "Multiple entities match — disambiguation step needed"

        # 6 — replan-or-continue gate
        if reason is not None:
            if state.programme_id:
                try:
                    wf = WorkflowDefinition.load(state.workflow_id, state.programme_id)
                    if wf.replanning_count >= 3:
                        return ReplanDecision(
                            action="CONTINUE",
                            reason="replan_limit_reached",
                            rationale=(
                                f"Already replanned {wf.replanning_count} times "
                                "— proceeding without further replan"
                            ),
                        )
                except Exception as exc:
                    logger.warning(  # type: ignore[attr-defined]
                        "evaluate_step: cannot load workflow to check replan count: %s", exc
                    )
            return ReplanDecision(action="REPLAN", reason=reason, rationale=rationale)

        # 7 — default
        return ReplanDecision(action="CONTINUE", reason="step_ok", rationale="")

    # ------------------------------------------------------------------
    # replan
    # ------------------------------------------------------------------

    def replan(
        self,
        workflow: WorkflowDefinition,
        completed_step: WorkflowStep,
        step_result: dict,
        accumulated_signals: dict,
    ) -> list[WorkflowStep]:
        """Revise remaining steps via ONE LLM call. Returns list of WorkflowStep.

        Falls back to a single ROUTE_TO_HUMAN step if the LLM call fails or
        returns an invalid response. Never raises.
        """
        next_id = _next_step_id(workflow)
        completed_ids = {
            s.step_id for s in workflow.steps
            if s.status in (StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED)
        }
        pending_steps = [s for s in workflow.steps if s.step_id not in completed_ids]

        completed_summary = [
            {
                "step_id":   s.step_id,
                "step_type": s.step_type,
                "status":    s.status.value,
            }
            for s in workflow.steps if s.step_id in completed_ids
        ]

        result_summary = str(step_result)[:300]
        context_str = str(workflow.context)[:1000]
        signals_str = str(accumulated_signals)[:500]
        pending_summary = [
            {"step_id": s.step_id, "step_type": s.step_type}
            for s in pending_steps
        ]

        prompt = (
            f"Workflow type: {workflow.workflow_type}\n"
            f"Context: {context_str}\n\n"
            f"Completed steps: {json.dumps(completed_summary)}\n\n"
            f"Triggering step: {completed_step.step_id} ({completed_step.step_type})\n"
            f"Step result: {result_summary}\n\n"
            f"Accumulated signals: {signals_str}\n\n"
            f"Remaining PENDING steps: {json.dumps(pending_summary)}\n\n"
            f"Available step types: {_AVAILABLE_STEP_TYPES}\n\n"
            f"Return a JSON array of revised steps. Each object must have:\n"
            f"  step_id (start from s{next_id}), step_type, depends_on (list),\n"
            f"  condition (null or string), max_attempts (int), backoff_seconds (int),\n"
            f"  planning_rationale (string).\n"
            f"Step IDs must not collide with: {sorted(completed_ids)}.\n"
            f"depends_on may reference completed step_ids or new step_ids in this list."
        )

        try:
            raw = llm_call(prompt, _REPLAN_SYSTEM, expect_json=True)
            if not isinstance(raw, list):
                raise ReplanLLMError(f"LLM returned non-list: {type(raw).__name__}")
            validated = self._validate_replan_response(raw, workflow, completed_ids)
            return validated
        except ReplanLLMError as exc:
            logger.warning(  # type: ignore[attr-defined]
                "replan: invalid LLM response — using fallback ROUTE_TO_HUMAN: %s", exc
            )
        except Exception as exc:
            logger.warning(  # type: ignore[attr-defined]
                "replan: LLM call failed — using fallback ROUTE_TO_HUMAN: %s", exc
            )

        return [WorkflowStep(
            step_id=f"s{next_id}",
            step_type="ROUTE_TO_HUMAN",
            status=StepStatus.PENDING,
            depends_on=[completed_step.step_id],
            condition=None,
            retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0),
            planning_rationale=(
                "Replanning LLM call failed or returned invalid response "
                "— escalating for human review"
            ),
            added_in_version=workflow.version + 1,
        )]

    def _validate_replan_response(
        self,
        raw: list,
        workflow: WorkflowDefinition,
        completed_ids: set[str],
    ) -> list[WorkflowStep]:
        """Parse and validate LLM replan JSON. Raises ReplanLLMError on any failure."""
        all_existing_ids = {s.step_id for s in workflow.steps}
        new_ids: set[str] = set()
        steps: list[WorkflowStep] = []

        for item in raw:
            if not isinstance(item, dict):
                raise ReplanLLMError("Item is not a dict")
            for fld in ("step_id", "step_type", "depends_on", "planning_rationale"):
                if fld not in item:
                    raise ReplanLLMError(f"Missing required field {fld!r}")
            if item["step_type"] not in _AVAILABLE_STEP_TYPES:
                raise ReplanLLMError(
                    f"Invalid step_type {item['step_type']!r}"
                )
            if item["step_id"] in completed_ids:
                raise ReplanLLMError(
                    f"step_id {item['step_id']!r} collides with completed step"
                )
            new_ids.add(item["step_id"])

        # Validate depends_on references
        all_reachable = all_existing_ids | new_ids
        for item in raw:
            for dep in (item.get("depends_on") or []):
                if dep not in all_reachable:
                    raise ReplanLLMError(
                        f"depends_on references unknown step_id {dep!r}"
                    )

        # Build WorkflowStep objects
        for item in raw:
            steps.append(WorkflowStep(
                step_id=item["step_id"],
                step_type=item["step_type"],
                status=StepStatus.PENDING,
                depends_on=item.get("depends_on") or [],
                condition=item.get("condition"),
                retry_policy=RetryPolicy(
                    max_attempts=int(item.get("max_attempts", 1)),
                    backoff_seconds=int(item.get("backoff_seconds", 0)),
                ),
                planning_rationale=item["planning_rationale"],
                added_in_version=workflow.version + 1,
            ))

        return steps


# Attach Era 2 methods to PlanningAgent — avoids MRO manipulation.
PlanningAgent.create_workflow = _Era2Mixin.create_workflow  # type: ignore[attr-defined]
PlanningAgent._create_intake_workflow = _Era2Mixin._create_intake_workflow  # type: ignore[attr-defined]
PlanningAgent._create_enrichment_workflow = _Era2Mixin._create_enrichment_workflow  # type: ignore[attr-defined]
PlanningAgent.evaluate_step = _Era2Mixin.evaluate_step  # type: ignore[attr-defined]
PlanningAgent.replan = _Era2Mixin.replan  # type: ignore[attr-defined]
PlanningAgent._validate_replan_response = _Era2Mixin._validate_replan_response  # type: ignore[attr-defined]
