"""Enrichment schemas — all dataclasses for Process 2 (Data Enrichment)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from cobalt.core.exceptions import EnrichmentSchemaError

# ---------------------------------------------------------------------------
# Validation frozensets
# ---------------------------------------------------------------------------

DEPTH_TIERS = frozenset({"BASIC", "STANDARD", "DEEP", "PROVISIONAL"})
SOURCE_TYPES = frozenset({
    "WEB_SEARCH", "COMPANY_WEBSITE", "LINKEDIN", "REGISTRY", "FINANCIAL", "NEWS", "WIKIDATA",
    "GLEIF", "SANCTIONS", "WIKIPEDIA",
})
VALIDATION_STATUSES = frozenset({"CONFIRMED", "LIKELY", "UNCERTAIN", "REJECTED"})
QUALITY_SIGNALS = frozenset({"OFFICIAL", "DIRECTORY", "NEWS", "SOCIAL"})
CONFIDENCE_LEVELS = frozenset({"HIGH", "MEDIUM", "LOW", "INFERRED", "CONFLICT", "MISSING"})
LIFECYCLE_SIGNALS = frozenset({
    "REBRANDED", "ACQUIRED", "MERGED", "POSSIBLY_DEFUNCT",
    "SPUN_OFF", "WENT_PUBLIC", "WENT_PRIVATE", "PARENT_CHANGED",
})
PROFILE_STATUSES = frozenset({
    "ENRICHED", "PARTIALLY_ENRICHED", "PROVISIONAL", "FAILED_ENRICHMENT",
})
OVERALL_CONFIDENCE = frozenset({"HIGH", "MEDIUM", "LOW", "PROVISIONAL"})
UPDATE_TYPES = frozenset({
    "REBRAND_MAP", "ACQUISITION_MAP", "ALIAS_DICTIONARY", "BRAND_MAP",
})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class KnownFacts:
    confirmed: list[str] = field(default_factory=list)
    gaps:      list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> KnownFacts:
        return cls(**data)


@dataclass
class EnrichmentReadinessResult:
    vendor_id:        str
    proceed:          bool
    skip:             bool
    skip_reason:      str | None
    depth_tier:       str
    source_list:      list[str]
    query_count:      int
    known_facts:      KnownFacts
    confidence_floor: float
    flags:            list[str]

    def __post_init__(self) -> None:
        if self.depth_tier not in DEPTH_TIERS:
            raise EnrichmentSchemaError(
                f"depth_tier must be one of {sorted(DEPTH_TIERS)!r}, got {self.depth_tier!r}"
            )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> EnrichmentReadinessResult:
        d = dict(data)
        if isinstance(d.get("known_facts"), dict):
            d["known_facts"] = KnownFacts.from_dict(d["known_facts"])
        return cls(**d)


@dataclass
class SourceEvidenceItem:
    content:           str
    source_type:       str
    source_url:        str
    retrieved_at:      str
    validation_status: str
    quality_signal:    str
    signal_type:       str | None = None

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise EnrichmentSchemaError(
                f"source_type must be one of {sorted(SOURCE_TYPES)!r}, got {self.source_type!r}"
            )
        if self.validation_status not in VALIDATION_STATUSES:
            raise EnrichmentSchemaError(
                f"validation_status must be one of {sorted(VALIDATION_STATUSES)!r}, "
                f"got {self.validation_status!r}"
            )
        if self.quality_signal not in QUALITY_SIGNALS:
            raise EnrichmentSchemaError(
                f"quality_signal must be one of {sorted(QUALITY_SIGNALS)!r}, "
                f"got {self.quality_signal!r}"
            )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SourceEvidenceItem:
        return cls(**data)


@dataclass
class SourceEvidenceBundle:
    vendor_id:              str
    depth_tier:             str
    sources:                dict[str, list[SourceEvidenceItem]]
    disambiguation_notices: list[dict]
    collection_flags:       list[str]

    def to_dict(self) -> dict:
        return {
            "vendor_id":              self.vendor_id,
            "depth_tier":             self.depth_tier,
            "sources": {
                k: [item.to_dict() for item in v]
                for k, v in self.sources.items()
            },
            "disambiguation_notices": self.disambiguation_notices,
            "collection_flags":       self.collection_flags,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SourceEvidenceBundle:
        d = dict(data)
        if isinstance(d.get("sources"), dict):
            d["sources"] = {
                k: [
                    SourceEvidenceItem.from_dict(item) if isinstance(item, dict) else item
                    for item in v
                ]
                for k, v in d["sources"].items()
            }
        return cls(**d)


@dataclass
class ExtractedField:
    value:      Any
    confidence: str
    source:     str

    def __post_init__(self) -> None:
        if self.confidence not in CONFIDENCE_LEVELS:
            raise EnrichmentSchemaError(
                f"confidence must be one of {sorted(CONFIDENCE_LEVELS)!r}, got {self.confidence!r}"
            )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ExtractedField:
        return cls(**data)


@dataclass
class ExtractedAttributes:
    vendor_id:        str
    fields:           dict[str, ExtractedField]
    conflicts:        list[dict]
    extraction_flags: list[str]

    def to_dict(self) -> dict:
        return {
            "vendor_id":        self.vendor_id,
            "fields":           {k: v.to_dict() for k, v in self.fields.items()},
            "conflicts":        self.conflicts,
            "extraction_flags": self.extraction_flags,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ExtractedAttributes:
        d = dict(data)
        if isinstance(d.get("fields"), dict):
            d["fields"] = {
                k: ExtractedField.from_dict(v) if isinstance(v, dict) else v
                for k, v in d["fields"].items()
            }
        return cls(**d)


@dataclass
class RelationshipMap:
    vendor_id:      str
    parent_company: dict | None
    subsidiaries:   list[dict]
    brands:         list[dict]
    former_names:   list[dict]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RelationshipMap:
        return cls(**data)


@dataclass
class LifecycleSignal:
    signal_type:           str
    from_:                 str | None
    to:                    str | None
    date:                  str | None
    confidence:            str
    source:                str
    brain_update_required: bool = False

    def __post_init__(self) -> None:
        if self.signal_type not in LIFECYCLE_SIGNALS:
            raise EnrichmentSchemaError(
                f"signal_type must be one of {sorted(LIFECYCLE_SIGNALS)!r}, "
                f"got {self.signal_type!r}"
            )
        if self.confidence not in CONFIDENCE_LEVELS:
            raise EnrichmentSchemaError(
                f"confidence must be one of {sorted(CONFIDENCE_LEVELS)!r}, "
                f"got {self.confidence!r}"
            )

    def to_dict(self) -> dict:
        return {
            "signal_type":           self.signal_type,
            "from":                  self.from_,
            "to":                    self.to,
            "date":                  self.date,
            "confidence":            self.confidence,
            "source":                self.source,
            "brain_update_required": self.brain_update_required,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LifecycleSignal:
        return cls(
            signal_type=data["signal_type"],
            from_=data.get("from"),
            to=data.get("to"),
            date=data.get("date"),
            confidence=data["confidence"],
            source=data["source"],
            brain_update_required=data.get("brain_update_required", False),
        )


@dataclass
class BrainUpdateSuggestion:
    update_type:            str
    from_:                  str
    to:                     str
    confidence:             str
    source_url:             str
    suggested_by_vendor_id: str
    review_required:        bool = True

    def __post_init__(self) -> None:
        if self.update_type not in UPDATE_TYPES:
            raise EnrichmentSchemaError(
                f"update_type must be one of {sorted(UPDATE_TYPES)!r}, "
                f"got {self.update_type!r}"
            )
        if self.confidence not in CONFIDENCE_LEVELS:
            raise EnrichmentSchemaError(
                f"confidence must be one of {sorted(CONFIDENCE_LEVELS)!r}, "
                f"got {self.confidence!r}"
            )

    def to_dict(self) -> dict:
        return {
            "update_type":            self.update_type,
            "from":                   self.from_,
            "to":                     self.to,
            "confidence":             self.confidence,
            "source_url":             self.source_url,
            "suggested_by_vendor_id": self.suggested_by_vendor_id,
            "review_required":        self.review_required,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BrainUpdateSuggestion:
        return cls(
            update_type=data["update_type"],
            from_=data["from"],
            to=data["to"],
            confidence=data["confidence"],
            source_url=data["source_url"],
            suggested_by_vendor_id=data["suggested_by_vendor_id"],
            review_required=data.get("review_required", True),
        )


@dataclass
class VendorProfile:
    vendor_id:             str
    canonical_name:        str
    profile_status:        str
    overall_confidence:    str
    enriched_at:           str
    identity:              dict
    classification:        dict
    size:                  dict
    organisation:          dict
    products_and_services: list[dict]          = field(default_factory=list)
    competitors:           list[dict]          = field(default_factory=list)
    certifications:        list[dict]          = field(default_factory=list)
    customer_segments:     list[dict]          = field(default_factory=list)
    reputation_signals:    list[dict]          = field(default_factory=list)
    key_people:            list[dict]          = field(default_factory=list)
    lifecycle_signals:     list[LifecycleSignal] = field(default_factory=list)
    gaps:                  dict               = field(
        default_factory=lambda: {"blocking": [], "enrichment": []}
    )
    flags:                 list[str]           = field(default_factory=list)
    enrichment_metadata:   dict               = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.profile_status not in PROFILE_STATUSES:
            raise EnrichmentSchemaError(
                f"profile_status must be one of {sorted(PROFILE_STATUSES)!r}, "
                f"got {self.profile_status!r}"
            )
        if self.overall_confidence not in OVERALL_CONFIDENCE:
            raise EnrichmentSchemaError(
                f"overall_confidence must be one of {sorted(OVERALL_CONFIDENCE)!r}, "
                f"got {self.overall_confidence!r}"
            )

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        # dataclasses.asdict() converts LifecycleSignal with key "from_" (not "from").
        # Replace with correctly-keyed dicts using LifecycleSignal.to_dict().
        d["lifecycle_signals"] = [sig.to_dict() for sig in self.lifecycle_signals]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> VendorProfile:
        d = dict(data)
        if "lifecycle_signals" in d:
            d["lifecycle_signals"] = [
                LifecycleSignal.from_dict(s) if isinstance(s, dict) else s
                for s in d["lifecycle_signals"]
            ]
        return cls(**d)


@dataclass
class EnrichedProfileResult:
    vendor_id:                  str
    profile_status:             str        # ENRICHED / PARTIALLY_ENRICHED / PROVISIONAL / FAILED_ENRICHMENT
    overall_confidence:         str        # HIGH / MEDIUM / LOW / PROVISIONAL
    profile_path:               str | None
    pcs_before:                 float
    pcs_after:                  float
    flags:                      list[str]
    triage_tasks:               list[dict]
    brain_update_suggestions:   list[BrainUpdateSuggestion]
    enriched_at:                str
    error:                      str | None

    def to_dict(self) -> dict:
        return {
            "vendor_id":               self.vendor_id,
            "profile_status":          self.profile_status,
            "overall_confidence":      self.overall_confidence,
            "profile_path":            self.profile_path,
            "pcs_before":              self.pcs_before,
            "pcs_after":               self.pcs_after,
            "flags":                   self.flags,
            "triage_tasks":            self.triage_tasks,
            "brain_update_suggestions": [s.to_dict() for s in self.brain_update_suggestions],
            "enriched_at":             self.enriched_at,
            "error":                   self.error,
        }


@dataclass
class EnrichmentRunResult:
    """Result returned by enrichment_orchestrator.run_enrichment()."""
    vendor_id:                str
    workflow_id:              str | None
    status:                   str        # COMPLETED / SKIPPED / BLOCKED / FAILED / PARTIAL
    profile_status:           str | None
    overall_confidence:       str | None
    flags:                    list[str]
    triage_tasks:             list[dict]
    brain_update_suggestions: list[BrainUpdateSuggestion]
    pcs_before:               float
    pcs_after:                float
    error:                    str | None
    reason:                   str | None

    def to_dict(self) -> dict:
        return {
            "vendor_id":               self.vendor_id,
            "workflow_id":             self.workflow_id,
            "status":                  self.status,
            "profile_status":          self.profile_status,
            "overall_confidence":      self.overall_confidence,
            "flags":                   self.flags,
            "triage_tasks":            self.triage_tasks,
            "brain_update_suggestions": [s.to_dict() for s in self.brain_update_suggestions],
            "pcs_before":              self.pcs_before,
            "pcs_after":               self.pcs_after,
            "error":                   self.error,
            "reason":                  self.reason,
        }


@dataclass
class RelationshipLifecycleResult:
    relationship_map:          RelationshipMap
    lifecycle_signals:         list[LifecycleSignal]
    brain_update_suggestions:  list[BrainUpdateSuggestion]
    flags:                     list[str]

    def to_dict(self) -> dict:
        return {
            "relationship_map":         self.relationship_map.to_dict(),
            "lifecycle_signals":        [s.to_dict() for s in self.lifecycle_signals],
            "brain_update_suggestions": [s.to_dict() for s in self.brain_update_suggestions],
            "flags":                    self.flags,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RelationshipLifecycleResult:
        return cls(
            relationship_map=RelationshipMap.from_dict(data["relationship_map"]),
            lifecycle_signals=[
                LifecycleSignal.from_dict(s) for s in data.get("lifecycle_signals", [])
            ],
            brain_update_suggestions=[
                BrainUpdateSuggestion.from_dict(s) for s in data.get("brain_update_suggestions", [])
            ],
            flags=data.get("flags", []),
        )
