# finding_engine (AN-06)

## Overview

**Process:** Process 4 — Analysis & Intelligence
**Stage:** Stage 6 — Finding Detection
**File:** `src/cobalt/tools/finding_engine.py`
**Role:** Detects findings from scores, Q&A answers, trends, and commercial analysis.
Classifies evidence gaps. Generates Next Best Action. Deterministic rule engine.
**Writes to workspace:** No — returns `FindingsBundle` in memory.
**LLM:** Conditional — one call for severity calibration when >= 3 HIGH findings.

---

## Purpose

Every finding must be traceable to an exact rule with an exact threshold. Rules are
the single source of truth — same inputs always produce the same findings. The optional
LLM severity calibration can only elevate severity, never reduce below the rule-based floor.

---

## Inputs

| Input | Source | Purpose |
|---|---|---|
| `vendor_id` | Caller | Target vendor |
| `programme_id` | Caller | For triage task generation |
| `score_bundle` | AN-02 | Dimension scores and deltas |
| `qa_pairs` | AN-05 | Completeness and confidence per question |
| `trend_report` | AN-04 | Trend direction and velocity |
| `commercial_result` | AN-03 | Commercial risk flags |
| `validated_assembly` | AN-01 | Evidence completeness for gap detection |
| `rs_profile` | RS-05 | Relationship type for tier thresholds |
| `scoring_config` | Config | Tier CRI thresholds |

---

## Output

Returns `FindingsBundle` in memory.

---

## Module-level constants

```python
SCORE_FINDING_THRESHOLD_MEDIUM = 65
SCORE_FINDING_THRESHOLD_HIGH   = 50
SCORE_DELTA_FINDING_THRESHOLD  = -10   # pts/run (note: not pts/month)

TIER_CRI_THRESHOLDS = {
    "STRATEGIC":  70,
    "PREFERRED":  65,
    "TRANSACTIONAL": 55,
    "INCIDENTAL": 45,
}

TREND_VELOCITY_HIGH_THRESHOLD = -5.0   # pts/month

CRITICAL_QA_QUESTIONS    = {"Q1", "Q4"}
MATERIAL_QA_QUESTIONS    = {"Q1", "Q2", "Q4", "Q6"}

NBA_RENEWAL_URGENCY_DAYS = 120   # elevate urgency if renewal within this window
NBA_COMPLIANCE_DAYS      = 90    # escalate compliance finding if within this window
```

---

## Skills

### 1. Score-based finding rules

```python
for dim_score in score_bundle.dimension_scores:
    if dim_score.score < SCORE_FINDING_THRESHOLD_HIGH:
        findings.append(Finding(
            severity="HIGH", source="SCORE",
            title=f"{dim_score.dimension} performance significantly below threshold",
            why=f"{dim_score.dimension} score is {dim_score.score}/100, "
                f"below the critical threshold of {SCORE_FINDING_THRESHOLD_HIGH}",
        ))
    elif dim_score.score < SCORE_FINDING_THRESHOLD_MEDIUM:
        findings.append(Finding(
            severity="MEDIUM", source="SCORE",
            title=f"{dim_score.dimension} below acceptable threshold",
            why=f"{dim_score.dimension} score is {dim_score.score}/100, "
                f"below the acceptable threshold of {SCORE_FINDING_THRESHOLD_MEDIUM}",
        ))
    
    if dim_score.delta is not None and dim_score.delta <= SCORE_DELTA_FINDING_THRESHOLD:
        findings.append(Finding(
            severity="MEDIUM", source="SCORE",
            title=f"{dim_score.dimension} declining rapidly",
            why=f"{dim_score.dimension} fell {abs(dim_score.delta)} points since last review",
        ))

# Tier CRI threshold
relationship_type = rs_profile.relationship_classification.relationship_type
tier_threshold = TIER_CRI_THRESHOLDS.get(relationship_type, 55)
if score_bundle.cri_score < tier_threshold:
    findings.append(Finding(
        severity="HIGH", source="SCORE",
        title=f"CRI below threshold for {relationship_type} vendor",
        why=f"CRI of {score_bundle.cri_score} is below the {tier_threshold} threshold "
            f"expected for a {relationship_type} vendor",
    ))
```

### 2. Q&A-based finding rules

```python
for qa_pair in qa_pairs:
    if qa_pair.completeness == "PARTIAL" and qa_pair.question_id in MATERIAL_QA_QUESTIONS:
        findings.append(Finding(
            severity="MEDIUM", source="QA",
            title=f"Insufficient evidence for {qa_pair.question_id} assessment",
            why=f"Incomplete answer to: {qa_pair.question[:80]}",
        ))
    
    if qa_pair.completeness == "UNANSWERABLE" and qa_pair.question_id in CRITICAL_QA_QUESTIONS:
        findings.append(Finding(
            severity="HIGH", source="QA",
            title=f"Critical question unanswerable: {qa_pair.question_id}",
            why=f"Cannot assess: {qa_pair.question[:80]}",
        ))
        gaps.append(ANGap(
            severity="BLOCKING",
            description=f"Evidence required to answer: {qa_pair.question}",
            suggested_action=f"Upload supporting documents or provide check-in response "
                             f"addressing: {', '.join(qa_pair.missing_evidence[:2])}",
        ))
```

### 3. Trend-based finding rules

```python
for dimension, trend in trend_report.dimension_trends.items():
    if trend["direction"] == "DECLINING" and trend.get("velocity") and trend["velocity"] <= TREND_VELOCITY_HIGH_THRESHOLD:
        findings.append(Finding(
            severity="HIGH", source="TREND",
            title=f"{dimension} in accelerating decline",
            why=f"{dimension} declining at {abs(trend['velocity']):.1f} pts/month",
        ))
    
    if trend.get("inflection_point"):   # was stable, now declining
        findings.append(Finding(
            severity="MEDIUM", source="TREND",
            title=f"{dimension} trend reversed to declining",
            why=f"Trend changed direction at {trend['inflection_point']}",
        ))
```

### 4. Commercial finding rules

```python
if commercial_result.commercial_risk_level in ["HIGH", "CRITICAL"]:
    findings.append(Finding(
        severity="HIGH", source="COMMERCIAL",
        title="Commercial risk elevated",
        why=f"Commercial risk assessed as {commercial_result.commercial_risk_level}",
    ))

for flag, (severity, title, why_template) in COMMERCIAL_FLAG_FINDINGS.items():
    if flag in commercial_result.commercial_findings:
        findings.append(Finding(severity=severity, source="COMMERCIAL", title=title,
                                why=why_template.format(result=commercial_result)))

COMMERCIAL_FLAG_FINDINGS = {
    "LICENCE_WASTE": (
        "MEDIUM",
        "Licence utilisation below threshold",
        "Licence waste: {result.licence_waste_pct:.0f}% of licences unused",
    ),
    "SLA_BREACH_PATTERN": (
        "HIGH",
        "SLA breach pattern detected",
        "SLA adherence at {result.sla_adherence_pct:.0f}% against 90% target",
    ),
    "MILESTONE_RISK": (
        "MEDIUM",
        "Delivery milestone at risk",
        "Delivery score {result.delivery_score:.0f}% against 80% threshold",
    ),
    "INCIDENT_FREQUENCY_RISING": (
        "MEDIUM",
        "Incident frequency increasing",
        "Month-over-month incident count is rising",
    ),
}
```

### 5. Gap classification from evidence

```python
for fact in validated_assembly.facts:
    if fact.freshness_status == "MISSING" and fact.field_name in EXPECTED_FIELDS:
        gaps.append(ANGap(
            severity="ENRICHMENT",
            description=f"Missing evidence: {fact.field_name}",
            suggested_action=f"Provide {fact.field_name} data for accurate analysis",
        ))
```

### 6. Finding deduplication

Group by `(title_prefix, source)`. If same concept triggered by multiple rules:
```python
# Keep highest severity. Merge evidence_ids.
by_key = {}
for f in raw_findings:
    key = (f.source, f.title[:40])
    if key in by_key:
        existing = by_key[key]
        if SEVERITY_ORDER[f.severity] > SEVERITY_ORDER[existing.severity]:
            by_key[key] = Finding(..., severity=f.severity, evidence_ids=existing.evidence_ids + f.evidence_ids)
        else:
            by_key[key].evidence_ids.extend(f.evidence_ids)
    else:
        by_key[key] = f

SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
```

### 7. LLM severity calibration (optional)

Only when `len([f for f in findings if f.severity in ["HIGH", "CRITICAL"]]) >= 3`:

One `llm_call()` model=gpt-4o, temperature=0, max_tokens=400:

```
SYSTEM: "You are reviewing findings for a vendor risk assessment. You may ELEVATE
         severity but NEVER reduce below the rule-based floor. Return JSON only."

USER: "Vendor: {vendor_id}, CRI: {cri_score}, Tier: {relationship_type}
       Renewal in: {renewal_days} days

       Findings:
       [{finding_id, title, current_severity, why, source} for each finding]

       Return: {\"calibrations\": [{\"finding_id\": \"...\", \"severity\": \"...\",
                \"reason\": \"...\"}]}"
```

Apply calibrations — accept ONLY elevations:
```python
for cal in calibrations:
    finding = find_by_id(cal["finding_id"])
    if SEVERITY_ORDER[cal["severity"]] > SEVERITY_ORDER[finding.severity]:
        finding.severity = cal["severity"]   # elevation accepted
    # else: ignored — never lower severity
```

On LLM failure → use rule-based severities unchanged, no raise.

### 8. Triage task generation

```python
for gap in gaps:
    if gap.severity == "BLOCKING":
        tasks = triage.generate_triage_tasks(
            [{"severity": "BLOCKING", "description": gap.description,
              "suggested_action": gap.suggested_action}],
            [], vendor_id, programme_id,
        )
        triage_tasks.extend(tasks)
```

### 9. NBA selection (deterministic)

```python
# Sort by severity descending
sorted_findings = sorted(findings, key=lambda f: SEVERITY_ORDER[f.severity], reverse=True)

# Apply urgency modifiers
renewal_days = _get_renewal_days(rs_profile)
if renewal_days and renewal_days < NBA_RENEWAL_URGENCY_DAYS:
    # Elevate top finding's urgency (not severity — urgency is a separate attribute)
    urgency = "URGENT"
else:
    urgency = "STANDARD"

# NBA: select highest severity finding
nba_finding = sorted_findings[0] if sorted_findings else None
nba = NBA(
    action=_derive_action(nba_finding),
    why=nba_finding.why if nba_finding else "",
    owner="vendor_owner",   # V1 default — V2 reads from PolicyConfig
    timing=_derive_timing(nba_finding, renewal_days),
    review_required=nba_finding.severity in ["HIGH", "CRITICAL"] if nba_finding else False,
    linked_finding_id=nba_finding.finding_id if nba_finding else "",
    created_at=now_iso(),
) if nba_finding else None

top_findings = sorted_findings[:3]
```

---

## Internal structure

```python
def detect_findings(
    vendor_id: str,
    programme_id: str,
    score_bundle: ScoreBundle,
    qa_pairs: list[QAPair],
    trend_report: TrendReport,
    commercial_result: CommercialAnalysisResult,
    validated_assembly: ValidatedEvidenceAssembly,
    rs_profile: "RelationshipSpendProfile",
    scoring_config: ScoringConfig,
) -> FindingsBundle:

def _score_findings(score_bundle, rs_profile) -> list[Finding]
def _qa_findings(qa_pairs) -> tuple[list[Finding], list[ANGap]]
def _trend_findings(trend_report) -> list[Finding]
def _commercial_findings(commercial_result) -> list[Finding]
def _evidence_gaps(validated_assembly) -> list[ANGap]
def _deduplicate(findings: list[Finding]) -> list[Finding]
def _llm_calibrate(findings, score_bundle, rs_profile, vendor_id) -> list[Finding]
def _select_nba(findings, renewal_days) -> NBA | None
def _get_renewal_days(rs_profile) -> int | None
```

---

## Tests required — tests/tools/test_finding_engine.py

- Dimension score 45 → Finding severity HIGH, source=SCORE
- Dimension score 60 → Finding severity MEDIUM, source=SCORE
- Dimension score 70 → no score-based finding for that dimension
- Dimension delta -12 → Finding severity MEDIUM source=SCORE (rapid decline)
- STRATEGIC vendor CRI 68 → Finding HIGH (below 70 threshold)
- Q1 UNANSWERABLE → Finding HIGH + ANGap BLOCKING
- Q3 PARTIAL → Finding MEDIUM (Q3 in MATERIAL_QA_QUESTIONS)
- Q5 PARTIAL → Finding MEDIUM (Q5 in MATERIAL_QA_QUESTIONS)
- DECLINING velocity -7 → Finding HIGH source=TREND
- DECLINING velocity -2 → no trend finding (above threshold)
- inflection_point set in trend → Finding MEDIUM source=TREND
- SLA_BREACH_PATTERN in commercial_findings → Finding HIGH source=COMMERCIAL
- LICENCE_WASTE in commercial_findings → Finding MEDIUM source=COMMERCIAL
- commercial_risk=HIGH → Finding HIGH source=COMMERCIAL
- commercial_risk=LOW → no commercial risk finding
- Same concept from score + trend rules → deduplicated to one finding, evidence_ids merged
- No findings at all → FindingsBundle.nba=None, top_findings=[], no crash
- renewal_days=100 → NBA review_required=True for HIGH finding
- top_findings always contains at most 3 findings
- Mock LLM calibration elevates MEDIUM to HIGH → accepted
- Mock LLM calibration tries to lower HIGH to MEDIUM → rejected (floor rule enforced)
- LLM calibration only called when >= 3 HIGH/CRITICAL findings present
- LLM calibration fails → rule-based severities used unchanged, no crash
- triage_tasks generated for each BLOCKING gap
