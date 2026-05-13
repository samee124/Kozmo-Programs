# Analysis Agent Specification

## Role
Extracts meaning from raw evidence.
Evaluates, scores, structures, detects.
Returns structured dicts. Never writes workspace files.

## The Absolute Rule
Analysis Agent extracts structure from raw data.
Research Agent collects raw data.
Document extraction belongs HERE — not in Research Agent.

## State Model
Stateless per call.
Receives evidence. Returns structured dict.
No files written.

## Public Methods

### extract_vendor_name_from_doc(raw_text, filename) → dict
  Input: raw PDF text from Research Agent
  Extracts: counterparty vendor name from parties clause,
            signature block, or document header
  Returns: {vendor_name, legal_name, doc_type_hint, confidence}
  Uses: LLM call (ONE per document)
  Purpose: Identity extraction for document-to-candidate linking

### extract_contract_terms(raw_text, vendor_name) → dict
  Input: raw PDF text from Research Agent + vendor name context
  Extracts all commercial terms:
    renewal_date, opt_out_deadline, auto_renewal,
    contract_value, contract_value_type,
    price_escalation, escalation_rate_max,
    payment_terms, early_termination,
    baa_present, phi_scope, nda_active,
    sla_uptime_pct, liability_cap,
    counterparty_name, doc_type
  Returns: {terms dict, confidence, doc_type}
  Uses: ONE LLM call with gpt-4o (precision needed)

### consolidate_documents(extracted_terms_list) → dict
  Input: list of extracted terms from multiple documents
  Consolidates into effective_terms:
    Amendments override MSA terms
    Most recent document wins on conflicts
  Returns: {effective_terms, conflicts_detected, source_map}
  Uses: deterministic rules — no LLM

### structure_entity(vendor_name, raw_research_text) → dict
  Input: raw web research text from Research Agent
  Extracts entity fields:
    legal_entity, ticker, parent_company, acquired_by,
    rebranded_to, category, subcategory, vendor_type,
    company_status, hq_city, hq_country, acquisition_status
  Returns: {structured_dict, confidence}
  Uses: ONE LLM call

### score_confidence(evidence_sources, resolution_method) → float
  Input: all evidence sources for a candidate
  Returns: overall confidence 0.0-1.0
  Uses: deterministic calculation — no LLM

### detect_contradictions(evidence_list) → list
  Input: multiple evidence items for same field
  Returns: list of contradiction records
  Uses: deterministic comparison — no LLM

## Document Extraction Flow
Orchestrator has PDF text from Research Agent.
Orchestrator calls Analysis Agent:
  1. extract_vendor_name_from_doc(raw_text) → vendor name for linking
  2. After vendor confirmed:
     extract_contract_terms(raw_text, vendor_name) → commercial terms
  3. If multiple docs:
     consolidate_documents([terms1, terms2, ...]) → effective terms

## Hard Rules
RULE 1: Never calls Research Agent. Receives raw text as input.
RULE 2: Returns dicts only. Never writes files.
RULE 3: ONE LLM call per method invocation maximum.
RULE 4: Temperature = 0 on all LLM calls.
RULE 5: JSON output enforced on all LLM calls.
RULE 6: On LLM failure: partial extraction at low confidence.
        Never return empty dict — return partial with confidence=0.25.
