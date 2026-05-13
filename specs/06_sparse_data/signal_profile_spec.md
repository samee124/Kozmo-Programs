# Signal Profile Specification

## Purpose
Output of the ANALYZE phase. Everything knowable
from a candidate without external calls.

## Location
src/Cobalt/models/schemas/signal_profile_schema.py

## Dataclasses

@dataclass
class BrainHit:
    matched:         bool
    confidence:      float
    canonical:       str | None
    match_type:      str | None     # KNOWN_VENDOR / REBRAND / ALIAS
    rebrand_match:   bool
    rebrand_target:  str | None
    alias_match:     bool
    alias_target:    str | None

@dataclass
class DedupResult:
    status:      str    # UNIQUE / AUTO_MERGE / CANDIDATE
    match_key:   str | None
    match_name:  str | None
    similarity:  float

@dataclass
class ErpSignal:
    exists:     bool
    spend:      Decimal | None
    category:   str | None
    vendor_ids: list[str]

@dataclass
class ApSignal:
    invoice_count:   int
    flags:           list[str]   # ROUND_NUMBERS / THRESHOLD_AVOIDANCE
    single_approver: bool
    approver_id:     str | None

@dataclass
class BatchContext:
    known_vendors: dict[str, KnownVendor]
    rebrand_map:   dict[str, str]
    alias_map:     dict[str, str]
    erp_hits:      dict[str, ErpSignal]
    ap_counts:     dict[str, ApSignal]
    all_keys:      list[str]

class ScriptType(str, Enum):
    LATIN      = "LATIN"
    CJK        = "CJK"
    ARABIC     = "ARABIC"
    CYRILLIC   = "CYRILLIC"
    DEVANAGARI = "DEVANAGARI"
    OTHER      = "OTHER"

class EntityType(str, Enum):
    COMPANY   = "COMPANY"
    PERSON    = "PERSON"
    INTERNAL  = "INTERNAL"
    AMBIGUOUS = "AMBIGUOUS"

class IntakeStatus(str, Enum):
    CONFIRMED       = "CONFIRMED"
    TRIAGE_REQUIRED = "TRIAGE_REQUIRED"
    DISCARDED       = "DISCARDED"
    BLOCKED         = "BLOCKED"

@dataclass
class SignalProfile:
    raw:              str
    cleaned:          str
    script_type:      ScriptType
    country_hint:     str | None
    normalized:       str
    comparison_key:   str
    brain_hit:        BrainHit
    dedup_result:     DedupResult
    entity_type:      EntityType
    erp_signal:       ErpSignal
    ap_signal:        ApSignal
    linked_doc_ids:   list[str]      # NEW — from source_processor
    spend_hint:       Decimal | None # NEW — from Excel column
    category_hint:    str | None     # NEW — from Excel column
