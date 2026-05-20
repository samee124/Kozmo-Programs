"""Process 4 (Analysis & Intelligence) data models.

All dataclasses for P4. Every class implements to_dict() / from_dict() for
RuntimeEngine snapshot compatibility. Confidence values are plain string
literals throughout — no ConfidenceLevel enum.

GapReport is defined in rs_schema.py. Import it from there — do NOT redefine it here.
ANGap and GapSeverityAN are P4-specific types; they do not replace GapReport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ValidatedEvidenceFact:
    """One validated fact extracted from vendor evidence. Produced by evidence_validator."""

    field_name:       str
    value:            Any
    display_value:    str
    extraction_type:  str           # ExtractionType value
    source_file:      str
    source_section:   str | None
    confidence:       str           # HIGH / MEDIUM / LOW
    trust_level:      str           # OFFICIAL / SYSTEM_EXPORT / USER_SUBMITTED / AI_INFERRED
    freshness_status: str           # FreshnessStatus value
    conflict_flag:    bool
    conflict_values:  list[Any]     # populated when conflict_flag=True; else []
    quality_score:    float         # 0.0–1.0
    validated_at:     str           # ISO timestamp

    def to_dict(self) -> dict:
        return {
            "field_name":       self.field_name,
            "value":            self.value,
            "display_value":    self.display_value,
            "extraction_type":  self.extraction_type,
            "source_file":      self.source_file,
            "source_section":   self.source_section,
            "confidence":       self.confidence,
            "trust_level":      self.trust_level,
            "freshness_status": self.freshness_status,
            "conflict_flag":    self.conflict_flag,
            "conflict_values":  self.conflict_values,
            "quality_score":    self.quality_score,
            "validated_at":     self.validated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ValidatedEvidenceFact":
        return cls(
            field_name=d.get("field_name") or "",
            value=d.get("value"),
            display_value=d.get("display_value") or "",
            extraction_type=d.get("extraction_type") or "",
            source_file=d.get("source_file") or "",
            source_section=d.get("source_section"),
            confidence=d.get("confidence") or "LOW",
            trust_level=d.get("trust_level") or "",
            freshness_status=d.get("freshness_status") or "",
            conflict_flag=bool(d.get("conflict_flag", False)),
            conflict_values=d.get("conflict_values") or [],
            quality_score=d.get("quality_score") or 0.0,
            validated_at=d.get("validated_at") or "",
        )


@dataclass
class ValidatedEvidenceAssembly:
    """Complete validated evidence set. Produced by evidence_validator. Consumed by all downstream AN tools."""

    vendor_id:         str
    programme_id:      str
    facts:             list[ValidatedEvidenceFact]
    completeness_pct:  float        # 0.0–1.0
    conflict_count:    int
    stale_count:       int
    missing_count:     int
    validated_at:      str

    def to_dict(self) -> dict:
        return {
            "vendor_id":         self.vendor_id,
            "programme_id":      self.programme_id,
            "facts":             [f.to_dict() for f in self.facts],
            "completeness_pct":  self.completeness_pct,
            "conflict_count":    self.conflict_count,
            "stale_count":       self.stale_count,
            "missing_count":     self.missing_count,
            "validated_at":      self.validated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ValidatedEvidenceAssembly":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            programme_id=d.get("programme_id") or "",
            facts=[ValidatedEvidenceFact.from_dict(f) for f in (d.get("facts") or [])],
            completeness_pct=d.get("completeness_pct") or 0.0,
            conflict_count=d.get("conflict_count") or 0,
            stale_count=d.get("stale_count") or 0,
            missing_count=d.get("missing_count") or 0,
            validated_at=d.get("validated_at") or "",
        )


@dataclass
class DimensionScore:
    """Score for one CRI dimension. Inside ScoreBundle."""

    dimension:       str          # delivery_reliability / responsiveness /
                                  # commercial_value / risk_compliance / relationship_trend
    score:           int          # 0–100
    prior_score:     int | None
    delta:           int | None
    trend_direction: str | None   # TrendDirection value

    def to_dict(self) -> dict:
        return {
            "dimension":       self.dimension,
            "score":           self.score,
            "prior_score":     self.prior_score,
            "delta":           self.delta,
            "trend_direction": self.trend_direction,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DimensionScore":
        return cls(
            dimension=d.get("dimension") or "",
            score=d.get("score") or 0,
            prior_score=d.get("prior_score"),
            delta=d.get("delta"),
            trend_direction=d.get("trend_direction"),
        )


@dataclass
class ScoreBundle:
    """Complete scoring output. Produced by scoring_engine."""

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

    def to_dict(self) -> dict:
        return {
            "vendor_id":           self.vendor_id,
            "cri_score":           self.cri_score,
            "prior_cri":           self.prior_cri,
            "cri_delta":           self.cri_delta,
            "health_band":         self.health_band,
            "dimension_scores":    [ds.to_dict() for ds in self.dimension_scores],
            "operational_metrics": self.operational_metrics,
            "portfolio_rank":      self.portfolio_rank,
            "category_rank":       self.category_rank,
            "scored_at":           self.scored_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreBundle":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            cri_score=d.get("cri_score") or 0,
            prior_cri=d.get("prior_cri"),
            cri_delta=d.get("cri_delta"),
            health_band=d.get("health_band") or "",
            dimension_scores=[
                DimensionScore.from_dict(ds) for ds in (d.get("dimension_scores") or [])
            ],
            operational_metrics=d.get("operational_metrics") or {},
            portfolio_rank=d.get("portfolio_rank"),
            category_rank=d.get("category_rank"),
            scored_at=d.get("scored_at") or "",
        )


@dataclass
class CommercialAnalysisResult:
    """Commercial analysis output. Produced by commercial_analyser."""

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

    def to_dict(self) -> dict:
        return {
            "vendor_id":                   self.vendor_id,
            "contract_type":               self.contract_type,
            "contract_type_confidence":    self.contract_type_confidence,
            "utilisation_score":           self.utilisation_score,
            "licence_waste_pct":           self.licence_waste_pct,
            "cost_per_seat":               self.cost_per_seat,
            "shelfware_flag":              self.shelfware_flag,
            "sla_adherence_pct":           self.sla_adherence_pct,
            "delivery_score":              self.delivery_score,
            "milestone_status":            self.milestone_status,
            "penalty_exposure":            self.penalty_exposure,
            "uptime_pct":                  self.uptime_pct,
            "incident_trend":              self.incident_trend,
            "mttr_days":                   self.mttr_days,
            "commercial_risk_level":       self.commercial_risk_level,
            "commercial_findings":         self.commercial_findings,
            "spend_efficiency_score":      self.spend_efficiency_score,
            "renewal_risk_scenarios":      self.renewal_risk_scenarios,
            "spend_efficiency_narrative":  self.spend_efficiency_narrative,
            "analysed_at":                 self.analysed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CommercialAnalysisResult":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            contract_type=d.get("contract_type") or "UNKNOWN",
            contract_type_confidence=d.get("contract_type_confidence") or "LOW",
            utilisation_score=d.get("utilisation_score"),
            licence_waste_pct=d.get("licence_waste_pct"),
            cost_per_seat=d.get("cost_per_seat"),
            shelfware_flag=bool(d.get("shelfware_flag", False)),
            sla_adherence_pct=d.get("sla_adherence_pct"),
            delivery_score=d.get("delivery_score"),
            milestone_status=d.get("milestone_status"),
            penalty_exposure=d.get("penalty_exposure"),
            uptime_pct=d.get("uptime_pct"),
            incident_trend=d.get("incident_trend"),
            mttr_days=d.get("mttr_days"),
            commercial_risk_level=d.get("commercial_risk_level") or "LOW",
            commercial_findings=d.get("commercial_findings") or [],
            spend_efficiency_score=d.get("spend_efficiency_score"),
            renewal_risk_scenarios=d.get("renewal_risk_scenarios") or [],
            spend_efficiency_narrative=d.get("spend_efficiency_narrative"),
            analysed_at=d.get("analysed_at") or "",
        )


@dataclass
class ActionLearning:
    """Insight from one historical action. Inside TrendReport."""

    action_type:     str
    action_taken_at: str
    before_score:    int
    after_score:     int
    delta:           int
    outcome_label:   str          # IMPROVED / NO_CHANGE / WORSENED
    insight:         str | None   # LLM-generated per action type

    def to_dict(self) -> dict:
        return {
            "action_type":     self.action_type,
            "action_taken_at": self.action_taken_at,
            "before_score":    self.before_score,
            "after_score":     self.after_score,
            "delta":           self.delta,
            "outcome_label":   self.outcome_label,
            "insight":         self.insight,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ActionLearning":
        return cls(
            action_type=d.get("action_type") or "",
            action_taken_at=d.get("action_taken_at") or "",
            before_score=d.get("before_score") or 0,
            after_score=d.get("after_score") or 0,
            delta=d.get("delta") or 0,
            outcome_label=d.get("outcome_label") or "",
            insight=d.get("insight"),
        )


@dataclass
class TrendReport:
    """Trend analysis output. Produced by trend_analyser."""

    vendor_id:               str
    dimension_trends:        dict   # dimension -> {direction, velocity, inflection_point, pattern}
    action_learning:         list[ActionLearning]
    action_learning_summary: str | None
    spend_trend:             dict   # {direction, velocity, yoy_delta}
    sla_trend:               dict   # {response_time_direction, breach_rate_direction}
    sentiment_trend:         dict   # {direction, last_signal_date}
    trend_computed_at:       str
    data_points_available:   int

    def to_dict(self) -> dict:
        return {
            "vendor_id":               self.vendor_id,
            "dimension_trends":        self.dimension_trends,
            "action_learning":         [al.to_dict() for al in self.action_learning],
            "action_learning_summary": self.action_learning_summary,
            "spend_trend":             self.spend_trend,
            "sla_trend":               self.sla_trend,
            "sentiment_trend":         self.sentiment_trend,
            "trend_computed_at":       self.trend_computed_at,
            "data_points_available":   self.data_points_available,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TrendReport":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            dimension_trends=d.get("dimension_trends") or {},
            action_learning=[
                ActionLearning.from_dict(al) for al in (d.get("action_learning") or [])
            ],
            action_learning_summary=d.get("action_learning_summary"),
            spend_trend=d.get("spend_trend") or {},
            sla_trend=d.get("sla_trend") or {},
            sentiment_trend=d.get("sentiment_trend") or {},
            trend_computed_at=d.get("trend_computed_at") or "",
            data_points_available=d.get("data_points_available") or 0,
        )


@dataclass
class EvidenceCitation:
    """Reference to a specific piece of evidence. Inside QAPair."""

    evidence_id:     str
    source_file:     str
    source_section:  str | None
    extraction_type: str
    quality_score:   float
    display_text:    str   # "SLA Exhibit A · § 11.2 [AUTO-EXTRACTED]"

    def to_dict(self) -> dict:
        return {
            "evidence_id":     self.evidence_id,
            "source_file":     self.source_file,
            "source_section":  self.source_section,
            "extraction_type": self.extraction_type,
            "quality_score":   self.quality_score,
            "display_text":    self.display_text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceCitation":
        return cls(
            evidence_id=d.get("evidence_id") or "",
            source_file=d.get("source_file") or "",
            source_section=d.get("source_section"),
            extraction_type=d.get("extraction_type") or "",
            quality_score=d.get("quality_score") or 0.0,
            display_text=d.get("display_text") or "",
        )


@dataclass
class QAPair:
    """One question-answer pair. Produced by inquiry_engine."""

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

    def to_dict(self) -> dict:
        return {
            "question_id":        self.question_id,
            "question":           self.question,
            "answer_text":        self.answer_text,
            "confidence":         self.confidence,
            "completeness":       self.completeness,
            "answered_by":        self.answered_by,
            "evidence_citations": [ec.to_dict() for ec in self.evidence_citations],
            "missing_evidence":   self.missing_evidence,
            "tier":               self.tier,
            "answered_at":        self.answered_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QAPair":
        return cls(
            question_id=d.get("question_id") or "",
            question=d.get("question") or "",
            answer_text=d.get("answer_text") or "",
            confidence=d.get("confidence") or "LOW",
            completeness=d.get("completeness") or "PARTIAL",
            answered_by=d.get("answered_by") or "",
            evidence_citations=[
                EvidenceCitation.from_dict(ec) for ec in (d.get("evidence_citations") or [])
            ],
            missing_evidence=d.get("missing_evidence") or [],
            tier=d.get("tier") or 1,
            answered_at=d.get("answered_at") or "",
        )


@dataclass
class ANGap:
    """A P4-detected gap. Inside FindingsBundle."""

    description:      str
    severity:         str    # GapSeverityAN value
    suggested_action: str

    def to_dict(self) -> dict:
        return {
            "description":      self.description,
            "severity":         self.severity,
            "suggested_action": self.suggested_action,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ANGap":
        return cls(
            description=d.get("description") or "",
            severity=d.get("severity") or "",
            suggested_action=d.get("suggested_action") or "",
        )


@dataclass
class NBA:
    """Next Best Action. Inside FindingsBundle."""

    action:            str
    why:               str
    owner:             str
    timing:            str    # NOW / THIS_WEEK / BEFORE_RENEWAL / MONITOR
    review_required:   bool
    linked_finding_id: str
    created_at:        str

    def to_dict(self) -> dict:
        return {
            "action":            self.action,
            "why":               self.why,
            "owner":             self.owner,
            "timing":            self.timing,
            "review_required":   self.review_required,
            "linked_finding_id": self.linked_finding_id,
            "created_at":        self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NBA":
        return cls(
            action=d.get("action") or "",
            why=d.get("why") or "",
            owner=d.get("owner") or "",
            timing=d.get("timing") or "",
            review_required=bool(d.get("review_required", False)),
            linked_finding_id=d.get("linked_finding_id") or "",
            created_at=d.get("created_at") or "",
        )


@dataclass
class Finding:
    """One detected issue. Inside FindingsBundle."""

    finding_id:   str
    title:        str
    severity:     str          # FindingSeverity value
    why:          str
    evidence_ids: list[str]
    source:       str          # FindingSource value
    status:       str          # FindingStatus value
    created_at:   str

    def to_dict(self) -> dict:
        return {
            "finding_id":   self.finding_id,
            "title":        self.title,
            "severity":     self.severity,
            "why":          self.why,
            "evidence_ids": self.evidence_ids,
            "source":       self.source,
            "status":       self.status,
            "created_at":   self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(
            finding_id=d.get("finding_id") or "",
            title=d.get("title") or "",
            severity=d.get("severity") or "",
            why=d.get("why") or "",
            evidence_ids=d.get("evidence_ids") or [],
            source=d.get("source") or "",
            status=d.get("status") or "",
            created_at=d.get("created_at") or "",
        )


@dataclass
class FindingsBundle:
    """Complete findings output. Produced by finding_engine."""

    vendor_id:     str
    findings:      list[Finding]
    gaps:          list[ANGap]
    nba:           NBA | None
    top_findings:  list[Finding]   # top 3 by severity — always populated
    triage_tasks:  list[dict]
    generated_at:  str

    def to_dict(self) -> dict:
        return {
            "vendor_id":    self.vendor_id,
            "findings":     [f.to_dict() for f in self.findings],
            "gaps":         [g.to_dict() for g in self.gaps],
            "nba":          self.nba.to_dict() if self.nba is not None else None,
            "top_findings": [f.to_dict() for f in self.top_findings],
            "triage_tasks": self.triage_tasks,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FindingsBundle":
        nba_raw = d.get("nba")
        return cls(
            vendor_id=d.get("vendor_id") or "",
            findings=[Finding.from_dict(f) for f in (d.get("findings") or [])],
            gaps=[ANGap.from_dict(g) for g in (d.get("gaps") or [])],
            nba=NBA.from_dict(nba_raw) if nba_raw is not None else None,
            top_findings=[Finding.from_dict(f) for f in (d.get("top_findings") or [])],
            triage_tasks=d.get("triage_tasks") or [],
            generated_at=d.get("generated_at") or "",
        )


@dataclass
class FindingNarrative:
    """Prose narrative for one finding. Inside NarrativeBundle."""

    finding_id:       str
    narrative_text:   str
    tone:             str
    evidence_summary: str
    redaction_flag:   bool

    def to_dict(self) -> dict:
        return {
            "finding_id":       self.finding_id,
            "narrative_text":   self.narrative_text,
            "tone":             self.tone,
            "evidence_summary": self.evidence_summary,
            "redaction_flag":   self.redaction_flag,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FindingNarrative":
        return cls(
            finding_id=d.get("finding_id") or "",
            narrative_text=d.get("narrative_text") or "",
            tone=d.get("tone") or "",
            evidence_summary=d.get("evidence_summary") or "",
            redaction_flag=bool(d.get("redaction_flag", False)),
        )


@dataclass
class QASummary:
    """Prose summary for one Q&A pair. Inside NarrativeBundle."""

    question_id:   str
    question:      str
    prose_summary: str

    def to_dict(self) -> dict:
        return {
            "question_id":   self.question_id,
            "question":      self.question,
            "prose_summary": self.prose_summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QASummary":
        return cls(
            question_id=d.get("question_id") or "",
            question=d.get("question") or "",
            prose_summary=d.get("prose_summary") or "",
        )


@dataclass
class NarrativeBundle:
    """Complete narrative output. Produced by narrative_engine."""

    vendor_id:            str
    vendor_summary:       str
    finding_narratives:   list[FindingNarrative]
    commercial_summary:   str | None
    qa_summaries:         list[QASummary]
    evidence_citations:   list[str]   # formatted display strings
    redaction_flags:      list[str]   # finding_ids needing review before external send
    generated_at:         str

    def to_dict(self) -> dict:
        return {
            "vendor_id":            self.vendor_id,
            "vendor_summary":       self.vendor_summary,
            "finding_narratives":   [fn.to_dict() for fn in self.finding_narratives],
            "commercial_summary":   self.commercial_summary,
            "qa_summaries":         [qs.to_dict() for qs in self.qa_summaries],
            "evidence_citations":   self.evidence_citations,
            "redaction_flags":      self.redaction_flags,
            "generated_at":         self.generated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NarrativeBundle":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            vendor_summary=d.get("vendor_summary") or "",
            finding_narratives=[
                FindingNarrative.from_dict(fn) for fn in (d.get("finding_narratives") or [])
            ],
            commercial_summary=d.get("commercial_summary"),
            qa_summaries=[
                QASummary.from_dict(qs) for qs in (d.get("qa_summaries") or [])
            ],
            evidence_citations=d.get("evidence_citations") or [],
            redaction_flags=d.get("redaction_flags") or [],
            generated_at=d.get("generated_at") or "",
        )


# ---------------------------------------------------------------------------
# Historical state types
# ---------------------------------------------------------------------------

@dataclass
class HistoricalEvidenceState:
    """Snapshot of evidence state for historical comparison."""

    vendor_id:          str
    prior_assembly_at:  str
    fact_snapshot:      dict   # field_name -> {value, quality_score, validated_at}

    def to_dict(self) -> dict:
        return {
            "vendor_id":         self.vendor_id,
            "prior_assembly_at": self.prior_assembly_at,
            "fact_snapshot":     self.fact_snapshot,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HistoricalEvidenceState":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            prior_assembly_at=d.get("prior_assembly_at") or "",
            fact_snapshot=d.get("fact_snapshot") or {},
        )


@dataclass
class HistoricalScoreState:
    """Historical CRI score runs for trend computation."""

    vendor_id: str
    runs:      list[dict]   # [{run_at, cri_score, health_band, dimension_scores: dict}]

    def to_dict(self) -> dict:
        return {
            "vendor_id": self.vendor_id,
            "runs":      self.runs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HistoricalScoreState":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            runs=d.get("runs") or [],
        )


@dataclass
class HistoricalQAState:
    """Historical Q&A pairs for answer evolution tracking."""

    vendor_id:   str
    prior_pairs: list[dict]  # [{question_id, answer_text, confidence, answered_at}]

    def to_dict(self) -> dict:
        return {
            "vendor_id":   self.vendor_id,
            "prior_pairs": self.prior_pairs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HistoricalQAState":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            prior_pairs=d.get("prior_pairs") or [],
        )


@dataclass
class HistoricalCommercialState:
    """Previous commercial analysis snapshot for delta detection."""

    vendor_id:           str
    prior_analysis_at:   str
    prior_contract_type: str | None
    prior_risk_level:    str | None

    def to_dict(self) -> dict:
        return {
            "vendor_id":           self.vendor_id,
            "prior_analysis_at":   self.prior_analysis_at,
            "prior_contract_type": self.prior_contract_type,
            "prior_risk_level":    self.prior_risk_level,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HistoricalCommercialState":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            prior_analysis_at=d.get("prior_analysis_at") or "",
            prior_contract_type=d.get("prior_contract_type"),
            prior_risk_level=d.get("prior_risk_level"),
        )


@dataclass
class ActionOutcomeHistory:
    """History of actions taken and their CRI impact."""

    vendor_id: str
    actions:   list[dict]  # [{action_type, taken_at, before_cri, after_cri, delta}]

    def to_dict(self) -> dict:
        return {
            "vendor_id": self.vendor_id,
            "actions":   self.actions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ActionOutcomeHistory":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            actions=d.get("actions") or [],
        )


# ---------------------------------------------------------------------------
# Config types
# ---------------------------------------------------------------------------

@dataclass
class QuestionSetItem:
    """One question in the P4 question set."""

    question_id:    str
    question:       str
    tier:           int
    dimension:      str
    contract_types: list[str]   # empty list = applies to all contract types

    def to_dict(self) -> dict:
        return {
            "question_id":    self.question_id,
            "question":       self.question,
            "tier":           self.tier,
            "dimension":      self.dimension,
            "contract_types": self.contract_types,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionSetItem":
        return cls(
            question_id=d.get("question_id") or "",
            question=d.get("question") or "",
            tier=d.get("tier") or 1,
            dimension=d.get("dimension") or "",
            contract_types=d.get("contract_types") or [],
        )


@dataclass
class ScoringConfig:
    """Weights and thresholds for the scoring_engine."""

    dimension_weights:       dict   # dimension -> float, must sum to 1.0
    health_band_thresholds:  dict   # HEALTHY/WATCH/AT_RISK/CRITICAL -> min_score
    tier_cri_thresholds:     dict   # relationship_type -> min_cri
    spike_multiplier:        float

    def to_dict(self) -> dict:
        return {
            "dimension_weights":      self.dimension_weights,
            "health_band_thresholds": self.health_band_thresholds,
            "tier_cri_thresholds":    self.tier_cri_thresholds,
            "spike_multiplier":       self.spike_multiplier,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScoringConfig":
        return cls(
            dimension_weights=d.get("dimension_weights") or {},
            health_band_thresholds=d.get("health_band_thresholds") or {},
            tier_cri_thresholds=d.get("tier_cri_thresholds") or {},
            spike_multiplier=d.get("spike_multiplier") or 1.0,
        )


@dataclass
class ANRunResult:
    """Result of one P4 analysis run."""

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

    def to_dict(self) -> dict:
        return {
            "vendor_id":     self.vendor_id,
            "programme_id":  self.programme_id,
            "status":        self.status,
            "cri_score":     self.cri_score,
            "health_band":   self.health_band,
            "finding_count": self.finding_count,
            "nba_action":    self.nba_action,
            "pcs_before":    self.pcs_before,
            "pcs_after":     self.pcs_after,
            "tools_run":     self.tools_run,
            "skip_reason":   self.skip_reason,
            "error":         self.error,
            "analysed_at":   self.analysed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ANRunResult":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            programme_id=d.get("programme_id") or "",
            status=d.get("status") or "FAILED",
            cri_score=d.get("cri_score"),
            health_band=d.get("health_band"),
            finding_count=d.get("finding_count") or 0,
            nba_action=d.get("nba_action"),
            pcs_before=d.get("pcs_before"),
            pcs_after=d.get("pcs_after"),
            tools_run=d.get("tools_run") or [],
            skip_reason=d.get("skip_reason"),
            error=d.get("error"),
            analysed_at=d.get("analysed_at") or "",
        )
