# Document Extractor Specification
# This is a skill of Analysis Agent — not a standalone tool.
# Lives in: src/Cobalt/agents/analysis_agent.py

## Purpose
Extract structured commercial terms from raw PDF text.
Called by Orchestrator after Research Agent fetches PDF text.

## The Boundary
Research Agent: fetches PDF → returns raw text
Analysis Agent: receives raw text → extracts structured fields

## LLM Prompt for extract_contract_terms()

System prompt must instruct:
  - Return ONLY valid JSON
  - Extract all fields or return null for missing
  - No preamble, no markdown fences

Fields to extract:
{
  "doc_type": "MSA"|"SOW"|"AMENDMENT"|"BAA"|"NDA"|"ORDER_FORM"|"INVOICE"|"UNKNOWN",
  "counterparty_name": string or null,
  "renewal_date": "YYYY-MM-DD" or null,
  "opt_out_deadline": "YYYY-MM-DD" or null,
  "auto_renewal": true|false|null,
  "contract_value": number or null,
  "contract_value_type": "FIXED"|"CEILING"|"MINIMUM"|"RANGE"|null,
  "price_escalation": true|false|null,
  "escalation_rate_max": number or null,
  "payment_terms": string or null,
  "early_termination": "PERMITTED"|"NOT_PERMITTED"|null,
  "liability_cap": number or null,
  "baa_present": true|false|null,
  "phi_scope": string or null,
  "nda_active": true|false|null,
  "nda_expiry": "YYYY-MM-DD" or null,
  "sla_uptime_pct": number or null
}

## Multi-Document Consolidation
consolidate_documents() applies these rules:
  - AMENDMENT overrides MSA for conflicting fields
  - More recent document wins on date fields
  - BAA presence: true if any document has baa_present=true
  - NDA: use most recent NDA document
  - Conflicts: flag SOURCE_FACT_CONFLICT, keep both values

## Confidence Scoring
Non-null fields / total fields = base confidence
+ 0.10 if doc_type confirmed (not UNKNOWN)
+ 0.05 if counterparty_name matches candidate name
Cap at 0.97

## What Workspace Builder Does With Extracted Terms
Writes cost_file/contract.md with effective_terms.
Creates evidence/ev-contract-{id}.md per document. IMMUTABLE.
Seeds cost_file/coverage.md with initial PCS based on OBSERVED fields.
Sets data_class = CLASS_B if spend AND contract both confirmed.
