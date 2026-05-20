# inquiry_engine (AN-05)

## Overview

**Process:** Process 4 — Analysis & Intelligence
**Stage:** Stage 3 — Q&A Reasoning
**File:** `src/cobalt/tools/inquiry_engine.py`
**Role:** Answers structured questions about the vendor from validated evidence.
Tiered depth — Tier 1 always, Tier 2 on weak answers, Tier 3 on material issues.
One LLM call per question. Returns QAPairs that feed scoring and finding detection.
**Writes to workspace:** No.
**LLM:** Yes — one call per question. 6–12 calls per vendor depending on tier activation.

---

## Purpose

The core intelligence tool. Reads evidence text and synthesises structured answers with
citations. Tier 1 questions cover all five CRI dimensions. Tier 2 drills into gaps.
Tier 3 investigates material issues. Every answer cites specific evidence.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `validated_assembly` | AN-01 | Quality-scored evidence for citation |
| `commercial_result` | AN-03 | Commercial metrics as additional evidence context |
| `rs_profile` | RS-05 output | Contract type, relationship context |
| `historical_qa` | Prior run (optional) | Prior answers for continuity |
| `scoring_config` | Config | Question set and thresholds |

---

## Output

Returns `list[QAPair]` in memory. Always contains at least 6 QAPairs (Tier 1).

---

## Module-level constants

```python
TIER_1_QUESTIONS: list[QuestionSetItem] = [
    QuestionSetItem(
        question_id="Q1", tier=1, dimension="delivery_reliability",
        question="Is this vendor meeting its contracted delivery commitments and SLA targets?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q2", tier=1, dimension="responsiveness",
        question="Is this vendor responding to issues, escalations, and requests within agreed timeframes?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q3", tier=1, dimension="commercial_value",
        question="Is the spend on this vendor delivering expected business value relative to contract cost?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q4", tier=1, dimension="risk_compliance",
        question="Are there active compliance failures, security risks, or regulatory concerns with this vendor?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q5", tier=1, dimension="relationship_trend",
        question="Is the overall quality of the relationship improving, stable, or declining?",
        contract_types=[],
    ),
    QuestionSetItem(
        question_id="Q6", tier=1, dimension="renewal_readiness",
        question="What is the renewal posture and are we prepared for the upcoming contract decision?",
        contract_types=[],
    ),
]

CRITICAL_QUESTIONS = {"Q1", "Q4"}   # Tier 3 only triggered for these
MAX_TIER3_QUESTIONS = 2             # cost control
```

---

## Skills

### 1. Evidence text construction

Build `evidence_text` from `ValidatedEvidenceAssembly`:
```
For each ValidatedEvidenceFact sorted by quality_score descending:
  "{field_name}: {display_value} [source: {source_file}, confidence: {confidence}, quality: {quality_score:.2f}]"
```

Append commercial metrics from `commercial_result`:
```
"[COMMERCIAL] contract_type: {contract_type}"
"[COMMERCIAL] commercial_risk: {commercial_risk_level}"
"[COMMERCIAL] sla_adherence: {sla_adherence_pct}%" (if not None)
"[COMMERCIAL] licence_waste: {licence_waste_pct}%" (if not None)
```

Cap total evidence_text at 4000 characters — take highest quality_score facts first.

### 2. LLM prompt (one call per question)

Model: gpt-4o, temperature 0, max_tokens 400.

```
SYSTEM: "You are a procurement intelligence analyst answering questions about vendor
         performance. Answer only from the evidence provided. If evidence is insufficient,
         say so explicitly. Return JSON only. No preamble."

USER:
Question: {question}
Vendor: {vendor_id}
Contract type: {commercial_result.contract_type}
Prior answer (if available): {prior_answer_text or "None"}

Evidence:
{evidence_text}

Return JSON:
{
  "answer_text": "2-3 sentences",
  "confidence": "HIGH|MEDIUM|LOW",
  "completeness": "COMPLETE|PARTIAL|UNANSWERABLE",
  "evidence_used": ["field_name_1", "field_name_2"],
  "missing_evidence": ["description of what would improve this answer"]
}
```

**On LLM failure:**
Return QAPair with:
  `answer_text = "Unable to answer — LLM unavailable."`
  `confidence = "LOW"`
  `completeness = "UNANSWERABLE"`
  `evidence_citations = []`
  No raise.

### 3. Tier 2 activation

For each Tier 1 QAPair where `confidence == "LOW"` OR `completeness == "PARTIAL"`:
  Generate one Tier 2 question:
  ```python
  missing = qa_pair.missing_evidence[0] if qa_pair.missing_evidence else "evidence is limited"
  tier2_q = (f"Given that {missing}, specifically regarding: {qa_pair.question} "
             f"— what can be determined from the available signals and context?")
  ```
  Run `_answer_question(tier2_q, tier=2)`.

### 4. Tier 3 activation

For each Tier 2 QAPair where:
  `completeness == "UNANSWERABLE"` AND `question_id in CRITICAL_QUESTIONS`:
  
  Generate one Tier 3 question:
  ```python
  tier3_q = (f"For the critical question '{original_question}': "
             f"given all available signals, what is the best available assessment "
             f"and what specific evidence would be needed to answer definitively?")
  ```
  Run `_answer_question(tier3_q, tier=3)`.
  
  Cap at `MAX_TIER3_QUESTIONS = 2` total Tier 3 questions per run.

### 5. Evidence citation building

After LLM returns `evidence_used` field names:
  For each field_name in evidence_used:
    Find matching `ValidatedEvidenceFact` in assembly where `fact.field_name == field_name`.
    If found, build `EvidenceCitation`:
      `evidence_id = field_name`
      `source_file = fact.source_file`
      `source_section = fact.source_section`
      `extraction_type = fact.extraction_type`
      `quality_score = fact.quality_score`
      `display_text` = build as:
        `"{source_file} · {source_section} [{extraction_type}]"` if source_section
        else `"{source_file} [{extraction_type}]"`
    If not found → skip silently.

---

## Internal structure

```python
def run_inquiry(
    vendor_id: str,
    validated_assembly: ValidatedEvidenceAssembly,
    commercial_result: CommercialAnalysisResult,
    rs_profile: "RelationshipSpendProfile",
    historical_qa: HistoricalQAState | None,
    scoring_config: ScoringConfig,
) -> list[QAPair]:

def _build_evidence_text(validated_assembly, commercial_result) -> str
def _get_prior_answer(question_id, historical_qa) -> str | None
def _answer_question(
    question: str,
    question_id: str,
    evidence_text: str,
    prior_answer: str | None,
    vendor_id: str,
    contract_type: str,
    tier: int,
) -> QAPair
def _build_citations(evidence_used, validated_assembly) -> list[EvidenceCitation]
```

---

## Tests required — tests/tools/test_inquiry_engine.py

- Mock llm_call returns COMPLETE + HIGH → QAPair confidence=HIGH, no Tier 2 generated
- Mock llm_call returns PARTIAL + LOW → Tier 2 question generated for that QAPair
- Mock llm_call raises exception → QAPair completeness=UNANSWERABLE, no crash, no raise
- Empty ValidatedEvidenceAssembly (all MISSING facts) → 6 Tier 1 QAPairs all UNANSWERABLE
- historical_qa with prior Q1 answer → prior answer text appears in prompt
- Tier 3 only triggered for Q1 and Q4 (CRITICAL_QUESTIONS)
- Tier 3 capped at MAX_TIER3_QUESTIONS=2 regardless of how many Tier 2 are UNANSWERABLE
- Evidence citations built from evidence_used field names matched to assembly facts
- Evidence citation with source_section → display_text = "filename · § 3.1 [AUTO-EXTRACTED]"
- Evidence citation without source_section → display_text = "filename [COMPUTED]"
- commercial_result metrics appended to evidence_text
- evidence_text capped at 4000 chars
- Always returns at least 6 QAPairs (one per Tier 1 question)
- LLM failure on Q3 → Q3 UNANSWERABLE, Q1/Q2/Q4/Q5/Q6 unaffected
