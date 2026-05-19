"""Process 3 (Relationship & Spend) data models.

All dataclasses for P3. Every class implements to_dict() / from_dict() for
RuntimeEngine snapshot compatibility. Confidence values are plain string
literals throughout — no ConfidenceLevel enum.

GapReport is defined here (not in gap_analyzer.py) to avoid circular imports.
gap_analyzer.py imports GapReport from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ArrivalMode(str, Enum):
    CONNECTOR   = "CONNECTOR"
    FILE_UPLOAD = "FILE_UPLOAD"
    CHECK_IN    = "CHECK_IN"


class TrustLevel(str, Enum):
    OFFICIAL        = "OFFICIAL"
    SYSTEM_EXPORT   = "SYSTEM_EXPORT"
    USER_SUBMITTED  = "USER_SUBMITTED"


class DocumentType(str, Enum):
    CONTRACT    = "CONTRACT"
    INVOICE     = "INVOICE"
    SOW         = "SOW"
    AMENDMENT   = "AMENDMENT"
    QBR         = "QBR"
    COMPLIANCE  = "COMPLIANCE"
    OTHER       = "OTHER"


class RelationshipType(str, Enum):
    STRATEGIC     = "STRATEGIC"
    PREFERRED     = "PREFERRED"
    TRANSACTIONAL = "TRANSACTIONAL"
    INCIDENTAL    = "INCIDENTAL"
    UNKNOWN       = "UNKNOWN"


class DependencyTier(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"
    LOW      = "LOW"


class ContractCoverage(str, Enum):
    FULLY_COVERED     = "FULLY_COVERED"
    PARTIALLY_COVERED = "PARTIALLY_COVERED"
    UNCOVERED         = "UNCOVERED"


class RenewalUrgency(str, Enum):
    URGENT  = "URGENT"
    WATCH   = "WATCH"
    OK      = "OK"
    UNKNOWN = "UNKNOWN"


class DataCompleteness(str, Enum):
    FULL    = "FULL"
    PARTIAL = "PARTIAL"
    SPARSE  = "SPARSE"
    NONE    = "NONE"


class ProfileStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL  = "PARTIAL"
    MINIMAL  = "MINIMAL"
    FAILED   = "FAILED"


class RSRunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    SKIPPED   = "SKIPPED"
    BLOCKED   = "BLOCKED"
    FAILED    = "FAILED"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RawSpendRecord:
    """One AP line from any data source. Produced by structured_data_collector."""

    source_id:            str
    arrival_mode:         str
    trust_level:          str
    period_start:         str | None
    period_end:           str | None
    amount_raw:           str
    currency_raw:         str | None
    amount_usd:           float | None
    category_raw:         str | None
    cost_centre:          str | None
    po_number:            str | None
    invoice_ref:          str | None
    matched_vendor_id:    str | None
    match_confidence:     str
    payment_terms_days:   int | None = None

    def to_dict(self) -> dict:
        return {
            "source_id":          self.source_id,
            "arrival_mode":       self.arrival_mode,
            "trust_level":        self.trust_level,
            "period_start":       self.period_start,
            "period_end":         self.period_end,
            "amount_raw":         self.amount_raw,
            "currency_raw":       self.currency_raw,
            "amount_usd":         self.amount_usd,
            "category_raw":       self.category_raw,
            "cost_centre":        self.cost_centre,
            "po_number":          self.po_number,
            "invoice_ref":        self.invoice_ref,
            "matched_vendor_id":  self.matched_vendor_id,
            "match_confidence":   self.match_confidence,
            "payment_terms_days": self.payment_terms_days,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RawSpendRecord":
        return cls(
            source_id=d.get("source_id") or "",
            arrival_mode=d.get("arrival_mode") or "",
            trust_level=d.get("trust_level") or "",
            period_start=d.get("period_start"),
            period_end=d.get("period_end"),
            amount_raw=d.get("amount_raw") or "",
            currency_raw=d.get("currency_raw"),
            amount_usd=d.get("amount_usd"),
            category_raw=d.get("category_raw"),
            cost_centre=d.get("cost_centre"),
            po_number=d.get("po_number"),
            invoice_ref=d.get("invoice_ref"),
            matched_vendor_id=d.get("matched_vendor_id"),
            match_confidence=d.get("match_confidence") or "UNMATCHED",
            payment_terms_days=d.get("payment_terms_days"),
        )


@dataclass
class ContractTerms:
    """Structured data extracted from one document by document_intelligence."""

    document_id:            str
    document_type:          str
    effective_date:         str | None
    expiry_date:            str | None
    auto_renews:            bool | None
    notice_period_days:     int | None
    total_value:            float | None
    currency:               str | None
    payment_terms_days:     int | None
    governing_law:          str | None
    termination_clauses:    list[str] = field(default_factory=list)
    key_obligations:        list[str] = field(default_factory=list)
    sla_summary:            str | None = None
    extraction_confidence:  str = "LOW"

    def to_dict(self) -> dict:
        return {
            "document_id":           self.document_id,
            "document_type":         self.document_type,
            "effective_date":        self.effective_date,
            "expiry_date":           self.expiry_date,
            "auto_renews":           self.auto_renews,
            "notice_period_days":    self.notice_period_days,
            "total_value":           self.total_value,
            "currency":              self.currency,
            "payment_terms_days":    self.payment_terms_days,
            "governing_law":         self.governing_law,
            "termination_clauses":   self.termination_clauses,
            "key_obligations":       self.key_obligations,
            "sla_summary":           self.sla_summary,
            "extraction_confidence": self.extraction_confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ContractTerms":
        return cls(
            document_id=d.get("document_id") or "",
            document_type=d.get("document_type") or "OTHER",
            effective_date=d.get("effective_date"),
            expiry_date=d.get("expiry_date"),
            auto_renews=d.get("auto_renews"),
            notice_period_days=d.get("notice_period_days"),
            total_value=d.get("total_value"),
            currency=d.get("currency"),
            payment_terms_days=d.get("payment_terms_days"),
            governing_law=d.get("governing_law"),
            termination_clauses=d.get("termination_clauses") or [],
            key_obligations=d.get("key_obligations") or [],
            sla_summary=d.get("sla_summary"),
            extraction_confidence=d.get("extraction_confidence") or "LOW",
        )


@dataclass
class StructuredDataBundle:
    """Complete output of structured_data_collector. Passed to Tools 2 and 3."""

    vendor_id:           str
    programme_id:        str
    collected_at:        str
    arrival_modes_used:  list[str]
    raw_spend_records:   list[RawSpendRecord]
    connector_metadata:  dict
    upload_metadata:     dict
    checkin_metadata:    dict
    collection_warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "vendor_id":           self.vendor_id,
            "programme_id":        self.programme_id,
            "collected_at":        self.collected_at,
            "arrival_modes_used":  self.arrival_modes_used,
            "raw_spend_records":   [r.to_dict() for r in self.raw_spend_records],
            "connector_metadata":  self.connector_metadata,
            "upload_metadata":     self.upload_metadata,
            "checkin_metadata":    self.checkin_metadata,
            "collection_warnings": self.collection_warnings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StructuredDataBundle":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            programme_id=d.get("programme_id") or "",
            collected_at=d.get("collected_at") or "",
            arrival_modes_used=d.get("arrival_modes_used") or [],
            raw_spend_records=[
                RawSpendRecord.from_dict(r) for r in (d.get("raw_spend_records") or [])
            ],
            connector_metadata=d.get("connector_metadata") or {},
            upload_metadata=d.get("upload_metadata") or {},
            checkin_metadata=d.get("checkin_metadata") or {},
            collection_warnings=d.get("collection_warnings") or [],
        )


@dataclass
class DocumentIntelligenceResult:
    """Complete output of document_intelligence. Passed to Tool 3 and Tool 5."""

    vendor_id:            str
    documents_processed:  int
    documents_skipped:    int
    extracted_contracts:  list[ContractTerms]
    extraction_warnings:  list[str]

    def to_dict(self) -> dict:
        return {
            "vendor_id":           self.vendor_id,
            "documents_processed": self.documents_processed,
            "documents_skipped":   self.documents_skipped,
            "extracted_contracts": [c.to_dict() for c in self.extracted_contracts],
            "extraction_warnings": self.extraction_warnings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DocumentIntelligenceResult":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            documents_processed=d.get("documents_processed") or 0,
            documents_skipped=d.get("documents_skipped") or 0,
            extracted_contracts=[
                ContractTerms.from_dict(c) for c in (d.get("extracted_contracts") or [])
            ],
            extraction_warnings=d.get("extraction_warnings") or [],
        )


@dataclass
class SpendSummary:
    """Aggregated spend view. Produced by spend_aggregator."""

    total_usd_all_time:         float | None
    total_usd_ttm:              float | None
    total_usd_ytd:              float | None
    by_period:                  dict
    by_category:                dict
    by_cost_centre:             dict
    invoice_count:              int
    po_count:                   int
    payment_terms_days_avg:     int | None
    data_completeness:          str
    confidence:                 str

    def to_dict(self) -> dict:
        return {
            "total_usd_all_time":         self.total_usd_all_time,
            "total_usd_ttm":              self.total_usd_ttm,
            "total_usd_ytd":              self.total_usd_ytd,
            "by_period":                  self.by_period,
            "by_category":                self.by_category,
            "by_cost_centre":             self.by_cost_centre,
            "invoice_count":              self.invoice_count,
            "po_count":                   self.po_count,
            "payment_terms_days_avg":     self.payment_terms_days_avg,
            "data_completeness":          self.data_completeness,
            "confidence":                 self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpendSummary":
        return cls(
            total_usd_all_time=d.get("total_usd_all_time"),
            total_usd_ttm=d.get("total_usd_ttm"),
            total_usd_ytd=d.get("total_usd_ytd"),
            by_period=d.get("by_period") or {},
            by_category=d.get("by_category") or {},
            by_cost_centre=d.get("by_cost_centre") or {},
            invoice_count=d.get("invoice_count") or 0,
            po_count=d.get("po_count") or 0,
            payment_terms_days_avg=d.get("payment_terms_days_avg"),
            data_completeness=d.get("data_completeness") or "NONE",
            confidence=d.get("confidence") or "NONE",
        )


@dataclass
class SpendAggregationResult:
    """Complete output of spend_aggregator."""

    vendor_id:          str
    summary:            SpendSummary
    anomalies:          list[dict]
    data_quality_flags: list[str]
    aggregated_at:      str

    def to_dict(self) -> dict:
        return {
            "vendor_id":          self.vendor_id,
            "summary":            self.summary.to_dict(),
            "anomalies":          self.anomalies,
            "data_quality_flags": self.data_quality_flags,
            "aggregated_at":      self.aggregated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SpendAggregationResult":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            summary=SpendSummary.from_dict(d.get("summary") or {}),
            anomalies=d.get("anomalies") or [],
            data_quality_flags=d.get("data_quality_flags") or [],
            aggregated_at=d.get("aggregated_at") or "",
        )


@dataclass
class RelationshipClassification:
    """Complete output of relationship_classifier."""

    vendor_id:                  str
    relationship_type:          str
    dependency_score:           float
    dependency_tier:            str | None
    single_source_risk:         bool
    contract_coverage:          str
    relationship_age_days:      int | None
    renewal_urgency:            str
    classification_confidence:  str
    llm_used:                   bool
    reasoning:                  str | None

    def to_dict(self) -> dict:
        return {
            "vendor_id":                 self.vendor_id,
            "relationship_type":         self.relationship_type,
            "dependency_score":          self.dependency_score,
            "dependency_tier":           self.dependency_tier,
            "single_source_risk":        self.single_source_risk,
            "contract_coverage":         self.contract_coverage,
            "relationship_age_days":     self.relationship_age_days,
            "renewal_urgency":           self.renewal_urgency,
            "classification_confidence": self.classification_confidence,
            "llm_used":                  self.llm_used,
            "reasoning":                 self.reasoning,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RelationshipClassification":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            relationship_type=d.get("relationship_type") or "UNKNOWN",
            dependency_score=d.get("dependency_score") or 0.0,
            dependency_tier=d.get("dependency_tier"),
            single_source_risk=d.get("single_source_risk") or False,
            contract_coverage=d.get("contract_coverage") or "UNCOVERED",
            relationship_age_days=d.get("relationship_age_days"),
            renewal_urgency=d.get("renewal_urgency") or "UNKNOWN",
            classification_confidence=d.get("classification_confidence") or "LOW",
            llm_used=d.get("llm_used") or False,
            reasoning=d.get("reasoning"),
        )


@dataclass
class GapReport:
    """Gap analysis result. Defined here to avoid circular imports with gap_analyzer."""

    missing_fields:        list[str]
    low_confidence_fields: list[str]
    stale_fields:          list[str]
    gap_severity:          str  # MAJOR / MINOR / NONE only — never CRITICAL from gap_analyzer
    recommended_actions:   list[str]

    def to_dict(self) -> dict:
        return {
            "missing_fields":        self.missing_fields,
            "low_confidence_fields": self.low_confidence_fields,
            "stale_fields":          self.stale_fields,
            "gap_severity":          self.gap_severity,
            "recommended_actions":   self.recommended_actions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GapReport":
        return cls(
            missing_fields=d.get("missing_fields") or [],
            low_confidence_fields=d.get("low_confidence_fields") or [],
            stale_fields=d.get("stale_fields") or [],
            gap_severity=d.get("gap_severity") or "NONE",
            recommended_actions=d.get("recommended_actions") or [],
        )


@dataclass
class RelationshipSpendProfile:
    """Master P3 profile. Written to workspace by rs_profile_assembler."""

    vendor_id:                   str
    programme_id:                str
    profile_version:             int
    profile_status:              str
    created_at:                  str
    last_updated:                str
    contract_count:              int
    spend_summary:               SpendSummary
    contract_terms:              list[ContractTerms]
    relationship_classification: RelationshipClassification
    gap_report:                  dict
    pcs_contribution:            float
    pcs_total:                   float
    flags:                       list[str]
    data_sources:                list[str]

    def to_dict(self) -> dict:
        return {
            "vendor_id":                   self.vendor_id,
            "programme_id":                self.programme_id,
            "profile_version":             self.profile_version,
            "profile_status":              self.profile_status,
            "created_at":                  self.created_at,
            "last_updated":                self.last_updated,
            "contract_count":              self.contract_count,
            "spend_summary":               self.spend_summary.to_dict(),
            "contract_terms":              [c.to_dict() for c in self.contract_terms],
            "relationship_classification": self.relationship_classification.to_dict(),
            "gap_report":                  self.gap_report,
            "pcs_contribution":            self.pcs_contribution,
            "pcs_total":                   self.pcs_total,
            "flags":                       self.flags,
            "data_sources":                self.data_sources,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RelationshipSpendProfile":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            programme_id=d.get("programme_id") or "",
            profile_version=d.get("profile_version") or 1,
            profile_status=d.get("profile_status") or "FAILED",
            created_at=d.get("created_at") or "",
            last_updated=d.get("last_updated") or "",
            contract_count=d.get("contract_count") or 0,
            spend_summary=SpendSummary.from_dict(d.get("spend_summary") or {}),
            contract_terms=[
                ContractTerms.from_dict(c) for c in (d.get("contract_terms") or [])
            ],
            relationship_classification=RelationshipClassification.from_dict(
                d.get("relationship_classification") or {}
            ),
            gap_report=d.get("gap_report") or {},
            pcs_contribution=d.get("pcs_contribution") or 0.0,
            pcs_total=d.get("pcs_total") or 0.0,
            flags=d.get("flags") or [],
            data_sources=d.get("data_sources") or [],
        )


@dataclass
class RSRunResult:
    """Orchestrator result. Mirrors EnrichmentRunResult pattern."""

    vendor_id:      str
    programme_id:   str
    status:         str
    pcs_before:     float | None
    pcs_after:      float | None
    tools_run:      list[str]
    flags_raised:   list[str]
    profile_status: str | None
    skip_reason:    str | None
    error:          str | None

    def to_dict(self) -> dict:
        return {
            "vendor_id":      self.vendor_id,
            "programme_id":   self.programme_id,
            "status":         self.status,
            "pcs_before":     self.pcs_before,
            "pcs_after":      self.pcs_after,
            "tools_run":      self.tools_run,
            "flags_raised":   self.flags_raised,
            "profile_status": self.profile_status,
            "skip_reason":    self.skip_reason,
            "error":          self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RSRunResult":
        return cls(
            vendor_id=d.get("vendor_id") or "",
            programme_id=d.get("programme_id") or "",
            status=d.get("status") or "FAILED",
            pcs_before=d.get("pcs_before"),
            pcs_after=d.get("pcs_after"),
            tools_run=d.get("tools_run") or [],
            flags_raised=d.get("flags_raised") or [],
            profile_status=d.get("profile_status"),
            skip_reason=d.get("skip_reason"),
            error=d.get("error"),
        )
