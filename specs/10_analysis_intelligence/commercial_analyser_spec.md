# commercial_analyser (AN-03)

## Overview

**Process:** Process 4 — Analysis & Intelligence
**Stage:** Stage 2 — Commercial Analysis
**File:** `src/cobalt/tools/commercial_analyser.py`
**Role:** Contract-type-aware commercial intelligence. Detects contract type and routes
to the correct analysis path. Computes utilisation, SLA adherence, spend efficiency,
and renewal risk scenarios.
**Writes to workspace:** No — returns `CommercialAnalysisResult` in memory.
**LLM:** Conditional — 3 targeted calls. See Skills section.

---

## Purpose

Produces contract-type-specific commercial metrics that feed AN-02 scoring_engine
(Commercial Value dimension) and AN-06 finding_engine (commercial rules).
Different vendors need different analysis: a SaaS vendor needs licence utilisation
analysis, a services vendor needs SLA adherence analysis.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `validated_assembly` | AN-01 | Quality-scored evidence facts |
| `rs_profile` | RS-05 output | Contract terms, spend summary, relationship classification |
| `structured_bundle` | RS-01 output (optional) | Licence counts, ticket exports, milestone data |
| `historical_state` | Prior run (optional) | Prior commercial analysis for trend context |
| `scoring_config` | Config | Contract-type thresholds |

---

## Output

Returns `CommercialAnalysisResult` in memory.

---

## Skills

### 1. Contract type detection

**Phase A — Deterministic keyword matching:**
Scan `rs_profile.contract_terms` for patterns:
```python
SAAS_SIGNALS     = ["per seat", "per user", "licence", "subscription", "saas",
                    "software as a service", "named user"]
SERVICES_SIGNALS = ["statement of work", "sow", "milestone", "deliverable",
                    "professional services", "time and materials"]
MANAGED_SIGNALS  = ["uptime", "incident response", "managed service", "sla response",
                    "service desk", "24x7"]
```
Count signal matches per type.
`keyword_confidence = max_type_count / total_signals_found`

Rules:
- SAAS signals > 0 AND MANAGED signals = 0 AND SERVICES signals = 0 → SAAS
- SERVICES signals > 0 AND SAAS signals = 0 AND MANAGED signals = 0 → SERVICES
- MANAGED signals > 0 AND SAAS signals = 0 AND SERVICES signals = 0 → MANAGED_SERVICES
- Multiple types detected → MIXED
- No signals → UNKNOWN, keyword_confidence = 0.0

**Phase B — LLM classification (only when keyword_confidence < 0.70):**
One `llm_call()` with model `gpt-4o`, temperature 0, max_tokens 200:

```
SYSTEM: "Classify vendor contract type as SAAS, SERVICES, MANAGED_SERVICES, MIXED, or UNKNOWN.
         Return JSON only."

USER: "Contract description for {vendor_id}:
       Document types: {[ct.document_type for ct in rs_profile.contract_terms]}
       Key obligations: {[ct.key_obligations for ct in rs_profile.contract_terms]}
       SLA terms: {[ct.sla_summary for ct in rs_profile.contract_terms]}

       Return: {\"contract_type\": \"...\", \"confidence\": \"HIGH|MEDIUM|LOW\", \"reasoning\": \"...\"}"
```

On LLM failure → contract_type = UNKNOWN, contract_type_confidence = LOW, no raise.

**Phase C — Set contract_type_confidence:**
- keyword_confidence >= 0.70 → HIGH
- LLM returned HIGH → HIGH
- LLM returned MEDIUM → MEDIUM
- Fallback or LLM LOW → LOW

### 2. SaaS analysis (run when contract_type in [SAAS, MIXED])

Extract from `structured_bundle` or `validated_assembly` facts:
  `active_users`, `total_licences`, `annual_contract_value`

```python
utilisation_rate = active_users / total_licences  (if both present)
licence_waste_pct = (1 - utilisation_rate) * 100  (if utilisation_rate present)
cost_per_seat = annual_contract_value / active_users  (if both present)
```

Flags:
- `utilisation_rate < 0.70` → add `"LICENCE_WASTE"` to commercial_findings
- `activation_rate < 0.50` → set `shelfware_flag = True`, add `"SHELFWARE_DETECTED"`

If licence data absent: add `"LICENCE_DATA_MISSING"` to commercial_findings.
Set utilisation_score, licence_waste_pct, cost_per_seat to None.

### 3. Services analysis (run when contract_type in [SERVICES, MIXED])

Extract from `structured_bundle` or `validated_assembly`:
  `compliant_tickets`, `total_priority_tickets`, `milestones_hit`, `total_milestones`
  `sla_credit_cap` per active breach

```python
sla_adherence_pct = compliant_tickets / total_priority_tickets * 100
delivery_score    = milestones_hit / total_milestones * 100
penalty_exposure  = sum of sla_credit_cap for active breaches
```

Flags:
- `sla_adherence_pct < 90` → add `"SLA_BREACH_PATTERN"`
- `delivery_score < 80` → add `"MILESTONE_RISK"`

If ticket data absent: add `"TICKET_DATA_MISSING"`.

### 4. Managed services analysis (run when contract_type in [MANAGED_SERVICES, MIXED])

Extract: `uptime_pct`, monthly incident counts over time.

```python
incident_trend = "RISING" if MoM incident count increase, else "STABLE" or "FALLING"
```

Flag: `incident_trend == "RISING"` → add `"INCIDENT_FREQUENCY_RISING"`

### 5. Commercial risk level (deterministic, after all paths run)

```python
CRITICAL: ("LICENCE_WASTE" and licence_waste_pct > 30)
          OR ("SLA_BREACH_PATTERN" and penalty_exposure > 0)
HIGH:     "LICENCE_WASTE" in findings
          OR "SLA_BREACH_PATTERN" in findings
          OR "INCIDENT_FREQUENCY_RISING" in findings
MEDIUM:   "TICKET_DATA_MISSING" in findings
          OR "LICENCE_DATA_MISSING" in findings
          OR (utilisation_score and utilisation_score < 0.85)
          OR (delivery_score and delivery_score < 90)
LOW:      no flags, or all metrics None (cannot determine)
```

### 6. Spend efficiency (deterministic)

From `rs_profile.spend_summary` and `rs_profile.contract_terms`:
```python
contract_total = sum(ct.total_value for ct in contract_terms if ct.total_value)
actual_spend   = rs_profile.spend_summary.total_usd_all_time
variance_pct   = (actual_spend - contract_total) / contract_total * 100
spend_efficiency_score = max(0, 100 - abs(variance_pct))
```

Forward `CONTRACT_DEVIATION` flag from `rs_profile.flags` if present.

### 7. Renewal risk scenarios — LLM call (always runs when contract data available)

One `llm_call()` when at least one `ContractTerms` with non-null `expiry_date`:

```
SYSTEM: "You are a procurement analyst. Generate renewal risk scenarios.
         Return JSON only. No preamble."

USER: "Vendor: {vendor_id}
       Contract value: {total_value}
       Expiry: {expiry_date}
       Auto-renews: {auto_renews}
       Notice period: {notice_period_days} days
       Commercial risk: {commercial_risk_level}
       Active flags: {commercial_findings}
       Prior trend: {historical_state.prior_risk_level if available}

       Return a JSON array of exactly 3 scenarios:
       [{\"scenario\": \"best_case\", \"description\": \"...\", \"probability\": 0.X},
        {\"scenario\": \"expected_case\", \"description\": \"...\", \"probability\": 0.X},
        {\"scenario\": \"worst_case\", \"description\": \"...\", \"probability\": 0.X}]"
```

On LLM failure → `renewal_risk_scenarios = []`, no raise.
If no contract data → `renewal_risk_scenarios = []`.

### 8. Spend efficiency narrative — LLM call (only when |variance_pct| > 15)

One `llm_call()` max_tokens 100:

```
SYSTEM: "Write one sentence explaining what this spend variance means for procurement."
USER: "Vendor {vendor_id}: contract value {contract_total}, actual spend {actual_spend},
       variance {variance_pct:.1f}%, commercial risk {commercial_risk_level}."
Return JSON: {"narrative": "..."}
```

On LLM failure → `spend_efficiency_narrative = None`, no raise.

---

## Internal structure

```python
def analyse_commercial(
    vendor_id: str,
    validated_assembly: ValidatedEvidenceAssembly,
    rs_profile: "RelationshipSpendProfile",
    structured_bundle: "StructuredDataBundle | None",
    historical_state: HistoricalCommercialState | None,
    scoring_config: ScoringConfig,
) -> CommercialAnalysisResult:

def _detect_contract_type(rs_profile, scoring_config) -> tuple[str, str]
def _analyse_saas(structured_bundle, validated_assembly) -> dict
def _analyse_services(structured_bundle, validated_assembly) -> dict
def _analyse_managed(structured_bundle, validated_assembly) -> dict
def _compute_risk_level(commercial_findings, metrics) -> str
def _compute_spend_efficiency(rs_profile) -> tuple[float | None, float | None]
def _llm_classify_contract(rs_profile, vendor_id) -> tuple[str, str]
def _llm_renewal_scenarios(rs_profile, vendor_id, commercial_risk_level, commercial_findings) -> list[dict]
def _llm_spend_narrative(vendor_id, contract_total, actual_spend, variance_pct, risk_level) -> str | None
```

---

## Tests required — tests/tools/test_commercial_analyser.py

- SaaS contract keywords → contract_type=SAAS, contract_type_confidence=HIGH
- Services contract keywords → contract_type=SERVICES
- Mixed keywords → contract_type=MIXED
- No keywords + mock LLM returns SERVICES → contract_type=SERVICES, llm used
- No keywords + LLM fails → contract_type=UNKNOWN, no crash
- utilisation_rate=0.60 → LICENCE_WASTE in findings, licence_waste_pct=40.0
- utilisation_rate=0.90 → no LICENCE_WASTE flag
- sla_adherence_pct=85 → SLA_BREACH_PATTERN in findings
- structured_bundle=None → LICENCE_DATA_MISSING in findings, all SaaS metrics None, no crash
- contract_type=UNKNOWN → commercial_risk_level=LOW, no crash, no LLM path
- Mock LLM renewal scenarios returns valid JSON array → renewal_risk_scenarios populated
- LLM renewal scenarios fails → renewal_risk_scenarios=[], no crash
- variance_pct=5 → spend_efficiency_narrative NOT generated (below 15% threshold)
- variance_pct=35 → LLM called for narrative
- LLM narrative fails → spend_efficiency_narrative=None, no crash
- rs_profile with no contract_terms → renewal_risk_scenarios=[], no LLM call
