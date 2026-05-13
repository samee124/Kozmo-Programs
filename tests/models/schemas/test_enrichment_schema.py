"""Tests for enrichment_schema.py — all Process 2 dataclasses."""

from __future__ import annotations

import json

import pytest

from cobalt.core.exceptions import EnrichmentSchemaError
from cobalt.models.schemas.enrichment_schema import (
    BrainUpdateSuggestion,
    EnrichmentReadinessResult,
    ExtractedAttributes,
    ExtractedField,
    KnownFacts,
    LifecycleSignal,
    RelationshipMap,
    SourceEvidenceBundle,
    SourceEvidenceItem,
    VendorProfile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sei(**kwargs) -> SourceEvidenceItem:
    defaults = dict(
        content="Acme Corp",
        source_type="WEB_SEARCH",
        source_url="https://example.com",
        retrieved_at="2026-05-12T10:00:00Z",
        validation_status="CONFIRMED",
        quality_signal="OFFICIAL",
    )
    defaults.update(kwargs)
    return SourceEvidenceItem(**defaults)


def _lifecycle_signal(**kwargs) -> LifecycleSignal:
    defaults = dict(
        signal_type="REBRANDED",
        from_="Acme Inc",
        to="Acme Corp",
        date="2020-01-01",
        confidence="HIGH",
        source="NEWS",
    )
    defaults.update(kwargs)
    return LifecycleSignal(**defaults)


def _vendor_profile(**kwargs) -> VendorProfile:
    defaults = dict(
        vendor_id="v-001",
        canonical_name="Acme Corp",
        profile_status="ENRICHED",
        overall_confidence="HIGH",
        enriched_at="2026-05-12T10:00:00Z",
        identity={},
        classification={},
        size={},
        organisation={},
    )
    defaults.update(kwargs)
    return VendorProfile(**defaults)


# ---------------------------------------------------------------------------
# Instantiation (tests 1–8)
# ---------------------------------------------------------------------------

def test_known_facts_defaults():
    kf = KnownFacts()
    assert kf.confirmed == []
    assert kf.gaps == []
    assert kf.conflicts == []


def test_enrichment_readiness_result_valid():
    err = EnrichmentReadinessResult(
        vendor_id="v-001",
        proceed=True,
        skip=False,
        skip_reason=None,
        depth_tier="STANDARD",
        source_list=["web_search"],
        query_count=5,
        known_facts=KnownFacts(confirmed=["legal_name"]),
        confidence_floor=0.3,
        flags=[],
    )
    assert err.vendor_id == "v-001"
    assert err.depth_tier == "STANDARD"
    assert err.known_facts.confirmed == ["legal_name"]


def test_enrichment_readiness_result_invalid_depth_tier():
    with pytest.raises(EnrichmentSchemaError, match="depth_tier"):
        EnrichmentReadinessResult(
            vendor_id="v-001",
            proceed=True,
            skip=False,
            skip_reason=None,
            depth_tier="TURBO",
            source_list=[],
            query_count=0,
            known_facts=KnownFacts(),
            confidence_floor=0.0,
            flags=[],
        )


def test_source_evidence_item_valid():
    item = _sei()
    assert item.signal_type is None
    assert item.source_type == "WEB_SEARCH"


def test_source_evidence_item_invalid_source_type():
    with pytest.raises(EnrichmentSchemaError, match="source_type"):
        _sei(source_type="INVALID_TYPE")


def test_source_evidence_item_invalid_validation_status():
    with pytest.raises(EnrichmentSchemaError, match="validation_status"):
        _sei(validation_status="MAYBE")


def test_lifecycle_signal_valid_none_fields():
    sig = LifecycleSignal(
        signal_type="REBRANDED",
        from_=None,
        to=None,
        date=None,
        confidence="HIGH",
        source="WEB_SEARCH",
    )
    assert sig.brain_update_required is False
    assert sig.from_ is None


def test_vendor_profile_defaults():
    vp = _vendor_profile()
    assert vp.products_and_services == []
    assert vp.competitors == []
    assert vp.certifications == []
    assert vp.customer_segments == []
    assert vp.reputation_signals == []
    assert vp.lifecycle_signals == []
    assert vp.gaps == {"blocking": [], "enrichment": []}
    assert vp.flags == []
    assert vp.enrichment_metadata == {}


# ---------------------------------------------------------------------------
# Serialisation (tests 9–16)
# ---------------------------------------------------------------------------

def test_source_evidence_item_round_trip():
    item = _sei(signal_type="ACQUISITION")
    restored = SourceEvidenceItem.from_dict(item.to_dict())
    assert restored == item


def test_extracted_field_round_trip_preserves_int():
    ef = ExtractedField(value=2012, confidence="HIGH", source="REGISTRY")
    d = ef.to_dict()
    restored = ExtractedField.from_dict(d)
    assert restored.value == 2012
    assert isinstance(restored.value, int)


def test_lifecycle_signal_to_dict_uses_from_key():
    sig = _lifecycle_signal(from_="Old Corp", to="New Corp")
    d = sig.to_dict()
    assert "from" in d
    assert "from_" not in d
    assert d["from"] == "Old Corp"


def test_lifecycle_signal_from_dict_accepts_from_key():
    data = {
        "signal_type": "ACQUIRED",
        "from": "Old Corp",
        "to": "New Corp",
        "date": "2026-01-01",
        "confidence": "HIGH",
        "source": "NEWS",
        "brain_update_required": False,
    }
    sig = LifecycleSignal.from_dict(data)
    assert sig.from_ == "Old Corp"
    assert sig.signal_type == "ACQUIRED"


def test_brain_update_suggestion_to_dict_uses_from_key():
    bus = BrainUpdateSuggestion(
        update_type="REBRAND_MAP",
        from_="Old Name",
        to="New Name",
        confidence="HIGH",
        source_url="https://example.com",
        suggested_by_vendor_id="v-001",
    )
    d = bus.to_dict()
    assert "from" in d
    assert "from_" not in d
    assert d["from"] == "Old Name"


def test_brain_update_suggestion_from_dict_accepts_from_key():
    data = {
        "update_type": "REBRAND_MAP",
        "from": "Old Name",
        "to": "New Name",
        "confidence": "HIGH",
        "source_url": "https://example.com",
        "suggested_by_vendor_id": "v-001",
        "review_required": True,
    }
    bus = BrainUpdateSuggestion.from_dict(data)
    assert bus.from_ == "Old Name"
    assert bus.update_type == "REBRAND_MAP"


def test_vendor_profile_to_dict_lifecycle_signals_use_from_key():
    sig = _lifecycle_signal(from_="Acme Inc", to="Acme Corp")
    vp = _vendor_profile(lifecycle_signals=[sig])
    d = vp.to_dict()
    assert len(d["lifecycle_signals"]) == 1
    ls_dict = d["lifecycle_signals"][0]
    assert "from" in ls_dict
    assert "from_" not in ls_dict
    assert ls_dict["from"] == "Acme Inc"


def test_vendor_profile_full_json_round_trip():
    sig = LifecycleSignal(
        signal_type="ACQUIRED",
        from_="Old Corp",
        to="New Corp",
        date="2025-06-01",
        confidence="MEDIUM",
        source="NEWS",
        brain_update_required=True,
    )
    original = _vendor_profile(
        vendor_id="v-rtrip",
        profile_status="PARTIALLY_ENRICHED",
        overall_confidence="MEDIUM",
        identity={"legal_name": "New Corp Ltd"},
        classification={"industry": "Tech"},
        size={"employees": 500},
        organisation={"hq": "London"},
        lifecycle_signals=[sig],
        flags=["REBRAND_DETECTED"],
    )

    serialised = json.dumps(original.to_dict())
    loaded_dict = json.loads(serialised)
    restored = VendorProfile.from_dict(loaded_dict)

    assert restored.vendor_id == original.vendor_id
    assert restored.profile_status == original.profile_status
    assert restored.overall_confidence == original.overall_confidence
    assert restored.flags == ["REBRAND_DETECTED"]
    assert len(restored.lifecycle_signals) == 1
    ls = restored.lifecycle_signals[0]
    assert ls.from_ == "Old Corp"
    assert ls.signal_type == "ACQUIRED"
    assert ls.brain_update_required is True


# ---------------------------------------------------------------------------
# Validation (tests 17–20)
# ---------------------------------------------------------------------------

def test_extracted_field_invalid_confidence():
    with pytest.raises(EnrichmentSchemaError, match="confidence"):
        ExtractedField(value="something", confidence="CERTAIN", source="WEB_SEARCH")


def test_extracted_field_inferred_confidence_accepted():
    ef = ExtractedField(value="inferred value", confidence="INFERRED", source="INFERRED")
    assert ef.confidence == "INFERRED"


def test_lifecycle_signal_invalid_signal_type():
    with pytest.raises(EnrichmentSchemaError, match="signal_type"):
        LifecycleSignal(
            signal_type="DISSOLVED",
            from_=None,
            to=None,
            date=None,
            confidence="HIGH",
            source="NEWS",
        )


def test_brain_update_suggestion_invalid_update_type():
    with pytest.raises(EnrichmentSchemaError, match="update_type"):
        BrainUpdateSuggestion(
            update_type="INVALID_TYPE",
            from_="Old Name",
            to="New Name",
            confidence="HIGH",
            source_url="https://example.com",
            suggested_by_vendor_id="v-001",
        )
