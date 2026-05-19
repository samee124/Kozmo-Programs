# document_intelligence

## Overview

**Process:** Process 3 — Relationship & Spend Data Gathering
**Stages covered:** Stage 2 (Document Extraction)
**File:** `src/cobalt/tools/document_intelligence.py`
**Role:** Read and extract structured facts from unstructured documents — contract PDFs, uploaded invoices, SOWs, amendment letters, QBR notes, compliance certificates. Uses LLM (one call per document).
**Writes to workspace:** No — returns `DocumentIntelligenceResult` in memory.
**Agent:** Analysis Agent is the logical owner, but `llm_call()` is imported directly from `cobalt.core.llm_call` — the agent is not a runtime parameter. This matches the existing pattern in `attribute_extractor.py`.

---

## Purpose

Converts unstructured document text into structured `ContractTerms` records. This is the primary LLM-using tool in Process 3 (the classifier uses LLM only in an ambiguous edge band). Each document gets exactly one LLM call — never more. All extraction failures produce degraded but non-null output — this tool never raises.

**One LLM call per document. Maximum 6,000 characters passed to LLM per call (truncated if longer).**

The Analysis Agent constraint is respected here: this tool does not write to the workspace, does not make external HTTP calls beyond the LLM gateway, and returns structured data for downstream assembly.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor for attribution |
| `programme_id` | Caller | Workspace path resolution |
| `document_paths` | Caller | Absolute paths to PDFs or text files |

---

## Output

Returns `DocumentIntelligenceResult` in memory.

```json
{
  "vendor_id": "V-XXXX-001",
  "documents_processed": 2,
  "documents_skipped": 0,
  "extracted_contracts": [
    {
      "document_id": "doc_001",
      "document_type": "CONTRACT",
      "effective_date": "2024-01-01",
      "expiry_date": "2026-12-31",
      "auto_renews": true,
      "notice_period_days": 90,
      "total_value": 480000.0,
      "currency": "GBP",
      "payment_terms_days": 30,
      "governing_law": "England and Wales",
      "termination_clauses": ["Material breach with 30-day cure period"],
      "key_obligations": ["Quarterly service reviews", "99.9% uptime SLA"],
      "sla_summary": "99.9% uptime; P1 response within 4 hours",
      "extraction_confidence": "HIGH"
    }
  ],
  "extraction_warnings": []
}
```

---

## Skills

### 1. Document type classification

Classifies each document before LLM extraction to provide contextual grounding.

**Method:** Regex heuristics against normalised filename + first 500 characters of extracted text. No LLM used for classification.

| Keyword pattern (case-insensitive) | Type assigned |
|---|---|
| `invoice`, `inv-`, `bill of` | INVOICE |
| `statement of work`, `sow`, `scope of work` | SOW |
| `amendment`, `addendum`, `change order`, `variation order` | AMENDMENT |
| `quarterly business review`, `qbr`, `business review` | QBR |
| `certificate`, `certification`, `iso \d{4,5}`, `compliance cert` | COMPLIANCE |
| `master service`, `msa`, `master agreement`, `framework agreement` | CONTRACT |
| `contract`, `agreement`, `terms and conditions`, `service agreement` | CONTRACT |
| (no pattern matches) | OTHER |

When multiple patterns match, precedence order: CONTRACT > SOW > AMENDMENT > INVOICE > QBR > COMPLIANCE > OTHER.

### 2. Document text extraction

**PDFs:** Uses `pypdf` (same approach as `research_agent.fetch_pdf_text()`). Page text concatenated with newlines.

**Text and markdown files (`.txt`, `.md`):** Direct UTF-8 read with `latin-1` fallback.

**Unsupported formats** (`.docx`, `.pptx`, etc.): Skip document; add `UNSUPPORTED_FORMAT_{doc_id}` to `extraction_warnings`. Count toward `documents_skipped`.

**Post-extraction checks:**
- Extracted text < 100 characters → skip as unreadable; add `DOCUMENT_UNREADABLE_{doc_id}`.
- Extracted text > 50,000 characters → truncate to first 50,000; add `DOCUMENT_TRUNCATED_{doc_id}`; LLM still called on truncated version.

Documents counted in `documents_processed` only if LLM call is attempted. Documents that fail pre-LLM checks count in `documents_skipped`.

### 3. LLM extraction

One `llm_call()` per document. Imported directly from `src/cobalt/core/llm_call.py` — never through an agent object, never via direct OpenAI. This matches the pattern in `attribute_extractor.py`.

**Model:** `gpt-4o`, temperature 0, max_tokens 1000.

**Prompt template:**

```
You are extracting contract terms from a {document_type} document for procurement records.
Extract the following fields and return as JSON only.
Return null for any field not present in the document — do not infer or guess values.

Fields to extract:
- effective_date: ISO date string (YYYY-MM-DD) or null
- expiry_date: ISO date string (YYYY-MM-DD) or null
- auto_renews: boolean (true/false) or null
- notice_period_days: integer (number of days) or null
- total_value: float (numeric value only, no currency symbol) or null
- currency: 3-letter ISO currency code or null
- payment_terms_days: integer (e.g. 30 for "Net 30") or null
- governing_law: string (jurisdiction name) or null
- termination_clauses: list of strings (one sentence each) or []
- key_obligations: list of strings (one sentence each) or []
- sla_summary: string (one sentence) or null

Document type: {document_type}
Document text:
{text[:6000]}

Return only the JSON object. No explanation.
```

**LLM failure handling:** If `llm_call()` raises or returns text that cannot be parsed as JSON → create `ContractTerms` with all value fields `None`, `extraction_confidence = "LOW"`, and add `LLM_EXTRACTION_FAILED_{doc_id}` to `extraction_warnings`. Document still counted in `documents_processed` (the call was attempted).

### 4. Response parsing and validation

After receiving LLM JSON response:
1. Attempt `json.loads()` on response string
2. If parse fails → treat as LLM failure (see Skill 3)
3. If parse succeeds → extract each field by key, coerce types:
   - Dates: validate ISO format; if invalid, set to `None`
   - Floats: accept integer responses, coerce to float
   - Lists: ensure each item is a string; filter empty strings
4. Unknown fields in JSON response → silently ignored

### 5. Confidence scoring

Assigned after successful LLM extraction.

| Confidence | Conditions |
|---|---|
| `HIGH` | Valid JSON returned; ≥ 5 non-null fields; `effective_date` or `total_value` is non-null |
| `MEDIUM` | Valid JSON returned; 2–4 non-null fields |
| `LOW` | Valid JSON but < 2 non-null fields; OR document was truncated; OR LLM call failed |

LLM failure always produces `LOW` confidence regardless of field count.

### 6. Duplicate detection

Before appending to `extracted_contracts`, check if a document with the same fingerprint already exists.

**Fingerprint:** tuple of `(effective_date, total_value, currency)` — all three must be non-null for fingerprint comparison to trigger.

Matching fingerprint → do not append to `extracted_contracts`; add `DUPLICATE_DOCUMENT_{doc_id}` to `extraction_warnings`.

If fingerprint fields are null → always append (cannot determine duplicacy).

---

## Flags produced

| Warning | Condition |
|---|---|
| `LLM_EXTRACTION_FAILED_{doc_id}` | `llm_call()` raised or returned invalid JSON |
| `DOCUMENT_UNREADABLE_{doc_id}` | < 100 characters extracted from document |
| `DOCUMENT_TRUNCATED_{doc_id}` | Document exceeded 50,000 characters; truncated before LLM call |
| `UNSUPPORTED_FORMAT_{doc_id}` | File type is not PDF, .txt, or .md |
| `DUPLICATE_DOCUMENT_{doc_id}` | Same `(effective_date, total_value, currency)` fingerprint as prior document |

---

## Routing

| Result | Next step |
|---|---|
| ≥ 1 `ContractTerms` extracted at MEDIUM+ confidence | Pass to Tool 5 (`rs_profile_assembler`) with contract coverage |
| All extractions LOW confidence | Pass with warnings; Tool 5 classifies `contract_coverage = UNCOVERED` |
| No documents provided (`document_paths` empty) | Return empty result; Tool 5 proceeds without contract data |
| All documents skipped | Return result with `documents_skipped = N`, empty `extracted_contracts`; Tool 5 notes gap |

---

## Internal structure

```python
def process_documents(
    vendor_id: str,
    programme_id: str,
    document_paths: list[str],
) -> DocumentIntelligenceResult:

def _classify_document_type(filename: str, first_500_chars: str) -> str
def _fetch_document_text(path: str) -> str | None
def _extract_contract_terms(doc_text: str, document_id: str, document_type: str) -> ContractTerms
def _score_confidence(terms: ContractTerms, was_truncated: bool) -> str
def _is_duplicate(new_terms: ContractTerms, existing: list[ContractTerms]) -> bool
```

---

## Tests required

- Valid PDF with contract content → `ContractTerms` extracted with `extraction_confidence = HIGH`
- Mock `llm_call` returns valid JSON with ≥ 5 fields → `extraction_confidence = HIGH`
- Mock `llm_call` returns valid JSON with 2 fields → `extraction_confidence = MEDIUM`
- Mock `llm_call` returns valid JSON with 1 field → `extraction_confidence = LOW`
- Mock `llm_call` returns malformed JSON → `extraction_confidence = LOW`, `LLM_EXTRACTION_FAILED_{doc_id}`, no raise
- Mock `llm_call` raises exception → `extraction_confidence = LOW`, warning logged, no raise
- PDF fetch failure (file not found) → document skipped, `DOCUMENT_UNREADABLE_{doc_id}` warning
- File with < 100 chars extracted → skipped, `DOCUMENT_UNREADABLE_{doc_id}`
- Document > 50,000 chars → `DOCUMENT_TRUNCATED_{doc_id}` warning, LLM still called
- `.docx` file → `UNSUPPORTED_FORMAT_{doc_id}`, document skipped
- `"invoice_2024_q1.pdf"` filename → `document_type = INVOICE`
- `"master_service_agreement.pdf"` filename → `document_type = CONTRACT`
- `"qbr_notes_oct25.txt"` filename → `document_type = QBR`
- Two documents with same `(effective_date, total_value, currency)` → second gets `DUPLICATE_DOCUMENT_{doc_id}`
- Two documents where fingerprint fields are null → both appended (no duplicate rejection)
- Empty `document_paths` list → empty result, no raise, `documents_processed = 0`
- LLM response includes unknown JSON fields → silently ignored, known fields extracted correctly
