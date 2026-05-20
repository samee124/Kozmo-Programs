# an_schema (Process 4 Data Models)

## Overview

**File:** `src/cobalt/models/schemas/an_schema.py`
**Role:** All dataclasses for Process 4 — Analysis & Intelligence. Defines data contracts
between tools, the orchestrator, and the workspace writer. Every class implements
`to_dict()` / `from_dict()` for RuntimeEngine snapshot compatibility.
**Depends on:** Nothing from rs_schema or enrichment_schema. Plain string literals
for confidence values throughout P4.

---

## Purpose

Central schema file for Process 4. Analogous to `rs_schema.py` for Process 3.
All P4 tools import from this file — no schema classes defined inside tool files.

**GapReport:** imported from `rs_schema.py` where it is already defined.
Do not redefine it here.

---

## Enumerations

```python
class ExtractionType(str, Enum):
    AUTO_EXTRACTED = "AUTO_EXTRACTED"
    COMPUTED       = "COMPUTED"
    SIGNAL         = "SIGNAL"

class FreshnessStatus(str, Enum):
    CURRENT = "CURRENT"
    STALE   = "STALE"
    MISSING = "MISSING"

class ContractType(str, Enum):
    SAAS             = "SAAS"
    SERVICES         = "SERVICES"
    MANAGED_SERVICES = "MANAGED_SERVICES"
    MIXED            = "MIXED"
    UNKNOWN          = "UNKNOWN"

class TrendDirection(str, Enum):
    IMPROVING = "IMPROVING"
    STABLE    = "STABLE"
    DECLINING = "DECLINING"
    UNKNOWN   = "UNKNOWN"

class TrendPattern(str, Enum):
    CYCLICAL     = "CYCLICAL"
    ACCELERATING = "ACCELERATING"
    SEASONAL     = "SEASONAL"
    STEADY       = "STEADY"
    UNKNOWN      = "UNKNOWN"

class FindingSeverity(str, Enum):
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"

class FindingStatus(str, Enum):
    OPEN       = "OPEN"
    CLOSED     = "CLOSED"
    MONITORING = "MONITORING"

class FindingSource(str, Enum):
    SCORE      = "SCORE"
    QA         = "QA"
    TREND      = "TREND"
    COMMERCIAL = "COMMERCIAL"

class GapSeverityAN(str, Enum):
    BLOCKING   = "BLOCKING"
    ENRICHMENT = "ENRICHMENT"

class CommercialRisk(str, Enum):
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"

class QACompleteness(str, Enum):
    COMPLETE     = "COMPLETE"
    PARTIAL      = "PARTIAL"
    UNANSWERABLE = "UNANSWERABLE"

class ANRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    SKIPPED   = "SKIPPED"
    BLOCKED   = "BLOCKED"
    FAILED    = "FAILED"
```

---

## Dataclasses

### `ValidatedEvidenceFact`
Produced by: `evidence_validator`

```python
@dataclass
class ValidatedEvidenceFact:
    field_name:       str
    value:            Any
    display_value:    str
    extraction_type:  str          # ExtractionType value
    source_file:      str
    source_section:   str | None
    confidence:       str          # HIGH / MEDIUM / LOW
    trust_level:      str          # OFFICIAL / SYSTEM_EXPORT / USER_SUBMITTED / AI_INFERRED
    freshness_status: str          # FreshnessStatus value
    conflict_flag:    bool
    conflict_values:  list[Any]    # populated when conflict_flag=True; else []
    quality_score:    float        # 0.0–1.0
    validated_at:     str          # ISO timestamp
```
`to_dict()`: all fields flat dict, None → null, lists → JSON arrays.
`from_dict(d)`: missing keys default to None; conflict_values defaults to [].

### `ValidatedEvidenceAssembly`
Produced by: `evidence_validator`. Consumed by all downstream AN tools.

```python
@dataclass
class ValidatedEvidenceAssembly:
    vendor_id:         str
    programme_id:      str
    facts:             list[ValidatedEvidenceFact]
    completeness_pct:  float        # 0.0–1.0
    conflict_count:    int
    stale_count:       int
    missing_count:     int
    validated_at:      str
```
`to_dict()`: facts serialised via ValidatedEvidenceFact.to_dict().
`from_dict(d)`: facts reconstructed via ValidatedEvidenceFact.from_dict().

### `DimensionScore`
Inside ScoreBundle.

```python
@dataclass
class DimensionScore:
    dimension:       str          # delivery_reliability / responsiveness /
                                  # commercial_value / risk_compliance / relationship_trend
    score:           int          # 0–100
    prior_score:     int | None
    delta:           int | None
    trend_direction: str | None   # TrendDirection value
```

### `ScoreBundle`
Produced by: `scoring_engine`.

```python
@dataclass
class ScoreBundle:
    vendor_id:           str
    cri_score:           int
    prior_cri:           int | None
    cri_delta:           int | None
    health_band:         str          # HEALTHY / WATCH / AT_RISK / CRITICAL
    dimension_scores:    list[DimensionScore]
    operational_metrics: dict         # sla_compliance_pct, avg_response_time, etc.
    portfolio_rank:      int | None   # V2 — always None in V1
    category_rank:       int | None   # V2 — always None in V1
    scored_at:           str
```

### `CommercialAnalysisResult`
Produced by: `commercial_analyser`.

```python
@dataclass
class CommercialAnalysisResult:
    vendor_id:                   str
    contract_type:               str          # ContractType value
    contract_type_confidence:    str          # HIGH / MEDIUM / LOW
    # SaaS fields (None when contract_type != SAAS/MIXED)
    utilisation_score:           float | None
    licence_waste_pct:           float | None
    cost_per_seat:               float | None
    shelfware_flag:              bool
    # Services fields (None when contract_type != SERVICES/MIXED)
    sla_adherence_pct:           float | None
    delivery_score:              float | None
    milestone_status:            str | None
    penalty_exposure:            float | None
    # Managed Services fields (None when contract_type != MANAGED_SERVICES/MIXED)
    uptime_pct:                  float | None
    incident_trend:              str | None
    mttr_days:                   float | None
    # Common
    commercial_risk_level:       str          # CommercialRisk value
    commercial_findings:         list[str]    # flag strings e.g. LICENCE_WASTE
    spend_efficiency_score:      float | None
    renewal_risk_scenarios:      list[dict]   # [{scenario, description, cost, probability}]
    spend_efficiency_narrative:  str | None
    analysed_at:                 str
```

### `ActionLearning`
Inside TrendReport.

```python
@dataclass
class ActionLearning:
    action_type:     str
    action_taken_at: str
    before_score:    int
    after_score:     int
    delta:           int
    outcome_label:   str          # IMPROVED / NO_CHANGE / WORSENED
    insight:         str | None   # LLM-generated per action type
```

### `TrendReport`
Produced by: `trend_analyser`.

```python
@dataclass
class TrendReport:
    vendor_id:               str
    dimension_trends:        dict   # dimension -> {direction, velocity, inflection_point, pattern}
    action_learning:         list[ActionLearning]
    action_learning_summary: str | None
    spend_trend:             dict   # {direction, velocity, yoy_delta}
    sla_trend:               dict   # {response_time_direction, breach_rate_direction}
    sentiment_trend:         dict   # {direction, last_signal_date}
    trend_computed_at:       str
    data_points_available:   int
```

### `EvidenceCitation`
Inside QAPair.

```python
@dataclass
class EvidenceCitation:
    evidence_id:     str
    source_file:     str
    source_section:  str | None
    extraction_type: str
    quality_score:   float
    display_text:    str   # "SLA Exhibit A · § 11.2 [AUTO-EXTRACTED]"
```

### `QAPair`
Produced by: `inquiry_engine`.

```python
@dataclass
class QAPair:
    question_id:        str
    question:           str
    answer_text:        str
    confidence:         str           # HIGH / MEDIUM / LOW
    completeness:       str           # QACompleteness value
    answered_by:        str           # "inquiry_engine"
    evidence_citations: list[EvidenceCitation]
    missing_evidence:   list[str]
    tier:               int           # 1 / 2 / 3
    answered_at:        str
```

### `ANGap`
Inside FindingsBundle.

```python
@dataclass
class ANGap:
    description:      str
    severity:         str    # GapSeverityAN value
    suggested_action: str
```

### `NBA`
Inside FindingsBundle.

```python
@dataclass
class NBA:
    action:            str
    why:               str
    owner:             str
    timing:            str    # NOW / THIS_WEEK / BEFORE_RENEWAL / MONITOR
    review_required:   bool
    linked_finding_id: str
    created_at:        str
```

### `Finding`
Inside FindingsBundle.

```python
@dataclass
class Finding:
    finding_id:   str
    title:        str
    severity:     str          # FindingSeverity value
    why:          str
    evidence_ids: list[str]
    source:       str          # FindingSource value
    status:       str          # FindingStatus value
    created_at:   str
```

### `FindingsBundle`
Produced by: `finding_engine`.

```python
@dataclass
class FindingsBundle:
    vendor_id:     str
    findings:      list[Finding]
    gaps:          list[ANGap]
    nba:           NBA | None
    top_findings:  list[Finding]   # top 3 by severity — always populated
    triage_tasks:  list[dict]
    generated_at:  str
```

### `FindingNarrative`
Inside NarrativeBundle.

```python
@dataclass
class FindingNarrative:
    finding_id:       str
    narrative_text:   str
    tone:             str
    evidence_summary: str
    redaction_flag:   bool
```

### `QASummary`
Inside NarrativeBundle.

```python
@dataclass
class QASummary:
    question_id:   str
    question:      str
    prose_summary: str
```

### `NarrativeBundle`
Produced by: `narrative_engine`.

```python
@dataclass
class NarrativeBundle:
    vendor_id:            str
    vendor_summary:       str
    finding_narratives:   list[FindingNarrative]
    commercial_summary:   str | None
    qa_summaries:         list[QASummary]
    evidence_citations:   list[str]   # formatted display strings
    redaction_flags:      list[str]   # finding_ids needing review before external send
    generated_at:         str
```

### Historical state types — written by orchestrator after each run

```python
@dataclass
class HistoricalEvidenceState:
    vendor_id:          str
    prior_assembly_at:  str
    fact_snapshot:      dict   # field_name -> {value, quality_score, validated_at}

@dataclass
class HistoricalScoreState:
    vendor_id: str
    runs:      list[dict]   # [{run_at, cri_score, health_band, dimension_scores: dict}]

@dataclass
class HistoricalQAState:
    vendor_id:   str
    prior_pairs: list[dict]  # [{question_id, answer_text, confidence, answered_at}]

@dataclass
class HistoricalCommercialState:
    vendor_id:           str
    prior_analysis_at:   str
    prior_contract_type: str | None
    prior_risk_level:    str | None

@dataclass
class ActionOutcomeHistory:
    vendor_id: str
    actions:   list[dict]  # [{action_type, taken_at, before_cri, after_cri, delta}]
```

### Config types

```python
@dataclass
class QuestionSetItem:
    question_id:    str
    question:       str
    tier:           int
    dimension:      str
    contract_types: list[str]   # empty list = applies to all contract types

@dataclass
class ScoringConfig:
    dimension_weights:       dict   # dimension -> float, must sum to 1.0
    health_band_thresholds:  dict   # HEALTHY/WATCH/AT_RISK/CRITICAL -> min_score
    tier_cri_thresholds:     dict   # relationship_type -> min_cri
    spike_multiplier:        float

@dataclass
class ANRunResult:
    vendor_id:     str
    programme_id:  str
    status:        str          # ANRunStatus value
    cri_score:     int | None
    health_band:   str | None
    finding_count: int
    nba_action:    str | None
    pcs_before:    float | None
    pcs_after:     float | None
    tools_run:     list[str]
    skip_reason:   str | None
    error:         str | None
    analysed_at:   str
```

---

## Import map

```python
# In all P4 tools:
from cobalt.models.schemas.an_schema import (
    ValidatedEvidenceFact, ValidatedEvidenceAssembly,
    DimensionScore, ScoreBundle,
    CommercialAnalysisResult, ActionLearning, TrendReport,
    EvidenceCitation, QAPair,
    Finding, ANGap, NBA, FindingsBundle,
    FindingNarrative, QASummary, NarrativeBundle,
    HistoricalEvidenceState, HistoricalScoreState,
    HistoricalQAState, HistoricalCommercialState, ActionOutcomeHistory,
    QuestionSetItem, ScoringConfig, ANRunResult,
    ExtractionType, FreshnessStatus, ContractType, TrendDirection,
    TrendPattern, FindingSeverity, FindingStatus, FindingSource,
    GapSeverityAN, CommercialRisk, QACompleteness, ANRunStatus,
)
```

---

## Tests required — tests/models/test_an_schema.py

- `ValidatedEvidenceFact` round-trip: `from_dict(f.to_dict()) == f`
- `ValidatedEvidenceFact` with `conflict_flag=True` and `conflict_values=[v1, v2]` — round-trips correctly
- `ValidatedEvidenceAssembly` round-trip including nested facts list
- `ScoreBundle` round-trip including nested `DimensionScore` list
- `CommercialAnalysisResult` with all None fields — round-trips, no KeyError
- `TrendReport` round-trip including nested `ActionLearning` list
- `QAPair` round-trip including nested `EvidenceCitation` list
- `FindingsBundle` with `nba=None` — round-trips correctly
- `NarrativeBundle` with empty lists — round-trips correctly
- `ANRunResult` with all None fields — round-trips cleanly
- All Enum values are `str` subclasses — JSON serialisable without extra conversion
- `HistoricalScoreState` with empty runs list — round-trips correctly
