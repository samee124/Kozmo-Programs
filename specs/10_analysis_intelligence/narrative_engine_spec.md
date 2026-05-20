# narrative_engine (AN-07)

## Overview

**Process:** Process 4 — Analysis & Intelligence
**Stage:** Stage 7 — Narrative Generation
**File:** `src/cobalt/tools/narrative_engine.py`
**Role:** Generates human-readable summaries and narratives from structured outputs
of all upstream tools. The only tool whose primary job is prose generation.
**Writes to workspace:** No — returns `NarrativeBundle` in memory.
**LLM:** Yes — 2 batched calls. All LLM failures produce degraded-but-non-null output.

---

## Purpose

Converts structured findings, scores, and Q&A answers into natural language.
Narratives feed the Overview page vendor summary, finding cards, and artifact generation.
Flags narratives containing internal labels before external distribution.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `findings_bundle` | AN-06 | Findings and NBA for narrative generation |
| `score_bundle` | AN-02 | CRI and dimension scores for vendor summary |
| `commercial_result` | AN-03 | Commercial metrics for commercial summary |
| `qa_pairs` | AN-05 | Q&A answers for prose summaries |
| `validated_assembly` | AN-01 | Source citations for every narrative |
| `vendor_file` | Workspace | Vendor name, renewal date, tier, relationship type |

---

## Output

Returns `NarrativeBundle` in memory.

---

## Skills

### 1. LLM Call 1 — Finding narratives + vendor summary

Always runs. Model: gpt-4o, temperature 0, max_tokens 800.

Resolve evidence_ids in findings to source file names before calling:
```python
citation_map = {
    fact.field_name: fact.source_file
    for fact in validated_assembly.facts
}
```

Prompt:
```
SYSTEM: "You are a professional procurement analyst writing clear, factual summaries.
         Be concise. 2-3 sentences per finding. Do not include internal scores,
         severity labels (HIGH/MEDIUM/CRITICAL), flag names (SLA_BREACH_PATTERN etc.),
         or CRI numbers in output. Return JSON only. No preamble."

USER: "Vendor: {vendor_name}
       Health: {health_band}
       CRI trend: {cri_delta direction if available, else 'first analysis'}
       Renewal: {renewal_days} days
       Primary finding: {top_findings[0].title if top_findings else 'None identified'}

       Write:
       1. VENDOR_SUMMARY: Exactly 2 sentences summarising current vendor status.
          Do not mention scores or severity labels.
       2. For each finding below, write FINDING_{finding_id}: 2-3 sentences explaining
          why it exists, what evidence supports it, and what risk exists if unaddressed.
          Do not use the word 'CRITICAL', 'HIGH', 'MEDIUM', or 'LOW'.

       Findings:
       {[{'id': f.finding_id, 'title': f.title, 'why': f.why,
          'evidence_sources': [citation_map.get(eid, eid) for eid in f.evidence_ids[:3]]}
         for f in findings_bundle.findings]}

       Return JSON:
       {
         \"vendor_summary\": \"...\",
         \"finding_narratives\": {\"finding_id_1\": \"...\", \"finding_id_2\": \"...\"}
       }"
```

**On LLM failure:**
```python
vendor_name = vendor_file.get("name", vendor_id)
vendor_summary = f"{vendor_name} — Analysis completed. {len(findings_bundle.findings)} findings identified."
finding_narratives = {}   # empty dict — not an error
```

### 2. LLM Call 2 — Commercial summary + Q&A summaries

Only runs when: `commercial_result.contract_type != "UNKNOWN"` AND `len(qa_pairs) > 0`.

Model: gpt-4o, temperature 0, max_tokens 600.

Build commercial metrics text based on contract_type:
```python
if contract_type == "SAAS":
    metrics_text = f"Utilisation: {utilisation_score:.0%}, Licence waste: {licence_waste_pct:.0f}%"
elif contract_type == "SERVICES":
    metrics_text = f"SLA adherence: {sla_adherence_pct:.0f}%, Delivery: {delivery_score:.0f}%"
elif contract_type == "MANAGED_SERVICES":
    metrics_text = f"Uptime: {uptime_pct:.0f}%, Incident trend: {incident_trend}"
else:
    metrics_text = "Mixed contract — see individual metrics"
```

Prompt:
```
SYSTEM: "You are writing procurement briefing content. Be factual and concise.
         One sentence per Q&A summary. Return JSON only."

USER: "Contract type: {contract_type}
       Key metrics: {metrics_text}
       Commercial risk: {commercial_risk_level}
       Active flags: {commercial_findings}

       Q&A answers:
       {[{'question_id': p.question_id, 'question': p.question,
          'answer': p.answer_text, 'confidence': p.confidence}
         for p in qa_pairs]}

       Write:
       1. COMMERCIAL_SUMMARY: 2-3 sentences on commercial performance and risk.
          Do not include flag names or technical labels.
       2. For each Q&A: QA_{question_id}: one sentence summary for a briefing.

       Return JSON:
       {\"commercial_summary\": \"...\",
        \"qa_summaries\": {\"Q1\": \"...\", \"Q2\": \"...\", ...}}"
```

**On LLM failure:**
```python
commercial_summary = None
qa_summaries = {}
```

### 3. Redaction check

After both LLM calls, scan all narrative text for:
```python
REDACTION_PATTERNS = [
    r'\bCRITICAL\b', r'\bHIGH finding\b', r'\bAT_RISK\b',
    r'\bSLA_BREACH_PATTERN\b', r'\bLICENCE_WASTE\b', r'\bCONTRACT_DEVIATION\b',
    r'\bCRI\s*\d+\b',   # CRI score numbers
    r'\b[0-9]+/100\b',  # score numbers like "45/100"
]
```

For each `FindingNarrative`, if any pattern matches `narrative_text`:
  Set `redaction_flag = True` on that narrative.
  Add `finding_id` to `redaction_flags` in NarrativeBundle.

### 4. Evidence citation formatting

For each `QAPair.evidence_citations`:
  `display_text = "{source_file} · {source_section} [{extraction_type}]"` if source_section
  else `display_text = "{source_file} [{extraction_type}]"`

Collect all formatted display_texts into `NarrativeBundle.evidence_citations`.

---

## Internal structure

```python
def generate_narratives(
    vendor_id: str,
    findings_bundle: FindingsBundle,
    score_bundle: ScoreBundle,
    commercial_result: CommercialAnalysisResult,
    qa_pairs: list[QAPair],
    validated_assembly: ValidatedEvidenceAssembly,
    vendor_file: dict,
) -> NarrativeBundle:

def _build_citation_map(validated_assembly) -> dict[str, str]
def _call_findings_llm(findings_bundle, score_bundle, vendor_file, citation_map) -> tuple[str, dict]
def _call_commercial_llm(commercial_result, qa_pairs) -> tuple[str | None, dict]
def _check_redaction(narrative_text: str) -> bool
def _format_citations(qa_pairs: list[QAPair]) -> list[str]
def _get_renewal_days(vendor_file: dict) -> int | None
```

---

## Tests required — tests/tools/test_narrative_engine.py

- Mock LLM Call 1 returns valid JSON → vendor_summary and finding_narratives populated
- Mock LLM Call 1 fails → fallback vendor_summary generated, finding_narratives={}, no raise
- Mock LLM Call 2 returns valid JSON → commercial_summary and qa_summaries populated
- Mock LLM Call 2 fails → commercial_summary=None, qa_summaries={}, no raise
- contract_type=UNKNOWN → LLM Call 2 not made
- qa_pairs=[] → LLM Call 2 not made
- Narrative containing "CRITICAL" → redaction_flag=True for that FindingNarrative
- Narrative containing "SLA_BREACH_PATTERN" → redaction_flag=True
- Clean narrative (no patterns) → redaction_flag=False
- Evidence citation with source_section → display_text = "filename · § 3.1 [AUTO-EXTRACTED]"
- Evidence citation without source_section → display_text = "filename [COMPUTED]"
- Empty findings_bundle (no findings) → vendor_summary generated, finding_narratives={}, no crash
- NarrativeBundle.redaction_flags contains finding_ids of flagged narratives
- Both LLM calls fail → NarrativeBundle still returned with fallback text, no raise
