"""Signal profile schema — the four sparse data dimensions per vendor candidate."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from cobalt.brain.loader import KnownVendor


class ScriptType(str, Enum):
    LATIN = "LATIN"
    CJK = "CJK"
    ARABIC = "ARABIC"
    CYRILLIC = "CYRILLIC"
    DEVANAGARI = "DEVANAGARI"
    OTHER = "OTHER"


class EntityType(str, Enum):
    COMPANY = "COMPANY"
    PERSON = "PERSON"
    INTERNAL = "INTERNAL"
    AMBIGUOUS = "AMBIGUOUS"


class IntakeStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    TRIAGE_REQUIRED = "TRIAGE_REQUIRED"
    DISCARDED = "DISCARDED"
    BLOCKED = "BLOCKED"


@dataclass
class BrainHit:
    matched: bool
    confidence: float
    canonical: str | None
    match_type: str | None  # KNOWN_VENDOR / REBRAND / ALIAS
    rebrand_match: bool
    rebrand_target: str | None
    alias_match: bool
    alias_target: str | None


@dataclass
class DedupResult:
    status: str  # UNIQUE / AUTO_MERGE / CANDIDATE
    match_key: str | None
    match_name: str | None
    similarity: float


@dataclass
class ErpSignal:
    exists: bool
    spend: Decimal | None
    category: str | None
    vendor_ids: list[str] = field(default_factory=list)


@dataclass
class ApSignal:
    invoice_count: int
    flags: list[str] = field(default_factory=list)  # ROUND_NUMBERS / THRESHOLD_AVOIDANCE
    single_approver: bool = False
    approver_id: str | None = None


@dataclass
class BatchContext:
    known_vendors: dict[str, KnownVendor]
    rebrand_map: dict[str, str]
    alias_map: dict[str, str]
    erp_hits: dict[str, ErpSignal]
    ap_counts: dict[str, ApSignal]
    all_keys: list[str]
    doc_registry: dict[str, list[str]] = field(default_factory=dict)
    candidate_hints: dict[str, dict] = field(default_factory=dict)


@dataclass
class SignalProfile:
    # Dimension 1 — Name quality
    raw: str
    cleaned: str
    script_type: ScriptType
    country_hint: str | None
    normalized: str
    comparison_key: str

    # Dimension 2 — Brain knowledge
    brain_hit: BrainHit

    # Dimension 3 — Internal connectors + documents
    erp_signal: ErpSignal
    ap_signal: ApSignal
    linked_doc_ids: list[str]
    spend_hint: Decimal | None  # from Excel column
    category_hint: str | None  # from Excel column

    # Dimension 4 — Risk + entity type
    entity_type: EntityType
    dedup_result: DedupResult

    # All other columns from the input file, preserved verbatim
    extra_fields: dict = field(default_factory=dict)
