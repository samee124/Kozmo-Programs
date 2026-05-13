# enrichment_schemas

## Overview

**File:** `src/cobalt/models/schemas/enrichment_schema.py`
**Role:** All dataclasses used across Process 2 (Data Enrichment) tools.

---

## Dataclasses

### EnrichmentReadinessResult

Output of Tool 1 (`enrichment_readiness_check`).

```python
@dataclass
class EnrichmentReadinessResult:
    vendor_id:           str
    proceed:             bool
    skip:                bool
    skip_reason:         str | None
    depth_tier:          str            # BASIC / STANDARD / DEEP / PROVISIONAL
    source_list:         list[str]      # ["web_search", "company_website", "linkedin"]
    query_count:         int
    known_facts:         KnownFacts
    confidence_floor:    float
    flags:               list[str]
```

### KnownFacts

```python
@dataclass
class KnownFacts:
    confirmed:    list[str]   # fields already populated at MEDIUM+ confidence
    gaps:         list[str]   # missing or LOW confidence fields
    conflicts:    list[str]   # fields flagged as conflicting
```

### SourceEvidenceItem

```python
@dataclass
class SourceEvidenceItem:
    content:             str
    source_type:         str       # WEB_SEARCH / COMPANY_WEBSITE / LINKEDIN / REGISTRY / FINANCIAL / NEWS
    source_url:          str
    retrieved_at:        str
    validation_status:   str       # CONFIRMED / LIKELY / UNCERTAIN / REJECTED
    quality_signal:      str       # OFFICIAL / DIRECTORY / NEWS / SOCIAL
    signal_type:         str | None = None    # for news: ACQUISITION / REBRAND / LEGAL / etc.
```

### SourceEvidenceBundle

Output of Tool 2 (`external_source_collector`).

```python
@dataclass
class SourceEvidenceBundle:
    vendor_id:                str
    depth_tier:               str
    sources:                  dict[str, list[SourceEvidenceItem]]
    disambiguation_notices:   list[dict]
    collection_flags:         list[str]
```

### ExtractedField

```python
@dataclass
class ExtractedField:
    value:        any
    confidence:   str          # HIGH / MEDIUM / LOW / INFERRED / CONFLICT / MISSING
    source:       str          # source_type that won, or INFERRED
```

### ExtractedAttributes

Output of Tool 3 (`attribute_extractor`).

```python
@dataclass
class ExtractedAttributes:
    vendor_id:           str
    fields:              dict[str, ExtractedField]
    conflicts:           list[dict]
    extraction_flags:    list[str]
```

### RelationshipMap

Output of Tool 4 (`relationship_and_lifecycle_mapper`).

```python
@dataclass
class RelationshipMap:
    vendor_id:           str
    parent_company:      dict | None        # {name, vendor_id, relationship_type, confidence}
    subsidiaries:        list[dict]
    brands:              list[dict]
    former_names:        list[dict]
```

### LifecycleSignal

```python
@dataclass
class LifecycleSignal:
    signal_type:             str       # REBRANDED / ACQUIRED / MERGED / POSSIBLY_DEFUNCT / SPUN_OFF / WENT_PUBLIC / WENT_PRIVATE / PARENT_CHANGED
    from_:                   str | None    # field is 'from' in JSON
    to:                      str | None
    date:                    str | None
    confidence:              str
    source:                  str
    brain_update_required:   bool = False
```

### BrainUpdateSuggestion

```python
@dataclass
class BrainUpdateSuggestion:
    update_type:          str        # REBRAND_MAP / ACQUISITION_MAP / ALIAS_DICTIONARY / BRAND_MAP
    from_:                str
    to:                   str
    confidence:           str
    source_url:           str
    suggested_by_vendor_id: str
    review_required:      bool = True
```

### VendorProfile

Output of Tool 5 (`enriched_profile_creator`), written to `vendor_profile.md`.

```python
@dataclass
class VendorProfile:
    vendor_id:               str
    canonical_name:          str
    profile_status:          str            # ENRICHED / PARTIALLY_ENRICHED / PROVISIONAL / FAILED_ENRICHMENT
    overall_confidence:      str            # HIGH / MEDIUM / LOW / PROVISIONAL
    enriched_at:             str

    identity:                dict
    classification:          dict
    size:                    dict
    organisation:            dict

    products_and_services:   list[dict]
    competitors:             list[dict]
    certifications:          list[dict]
    customer_segments:       list[dict]
    reputation_signals:      list[dict]
    lifecycle_signals:       list[LifecycleSignal]

    gaps:                    dict           # {blocking: [], enrichment: []}
    flags:                   list[str]
    enrichment_metadata:     dict           # {depth_tier, sources_used, pcs_before, pcs_after}
```

---

## New Brain files

### Brain/acquisition_map.json

```json
{
  "citrix": {
    "acquired_by": "Cloud Software Group",
    "acquired_by_key": "cloud software group",
    "date": "2022-09-30",
    "status": "COMPLETED"
  },
  "slack technologies": {
    "acquired_by": "Salesforce Inc",
    "acquired_by_key": "salesforce inc",
    "date": "2021-07-21",
    "status": "COMPLETED"
  }
}
```

### Brain/brand_map.json

```json
{
  "instagram": "meta platforms inc",
  "slack": "salesforce inc",
  "github": "microsoft",
  "linkedin": "microsoft",
  "google cloud": "google llc",
  "google workspace": "google llc",
  "youtube": "google llc",
  "whatsapp": "meta platforms inc",
  "facebook": "meta platforms inc"
}
```

---

## brain/loader.py updates

```python
@dataclass
class BrainData:
    known_vendors:     dict[str, KnownVendor]
    rebrand_map:       dict[str, str]
    alias_map:         dict[str, str]
    acquisition_map:   dict[str, dict]      # NEW
    brand_map:         dict[str, str]       # NEW
```

`load_brain()` now requires 5 files. If acquisition_map or brand_map are missing, log warning and return empty dict — do not raise (V1 backward compatibility).

---

## Tests required

- All schemas instantiate with required fields
- Optional fields default correctly
- JSON serialisation round trip for each schema
- Brain loader handles missing acquisition_map gracefully
- Brain loader handles missing brand_map gracefully
- All 5 Brain files load when present
