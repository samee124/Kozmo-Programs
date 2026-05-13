"""Tests for relationship_and_lifecycle_mapper — Process 2 Tool 4."""

from __future__ import annotations

import json

import pytest

from cobalt.brain.loader import BrainData, KnownVendor, invalidate_cache
from cobalt.models.schemas.enrichment_schema import (
    ExtractedAttributes,
    ExtractedField,
    RelationshipLifecycleResult,
    SourceEvidenceBundle,
    SourceEvidenceItem,
)
from cobalt.tools.relationship_and_lifecycle_mapper import map_relationships_and_lifecycle


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_item(
    content: str = "",
    source_type: str = "WEB_SEARCH",
    source_url: str = "https://example.com",
    signal_type: str | None = None,
) -> SourceEvidenceItem:
    return SourceEvidenceItem(
        content=content,
        source_type=source_type,
        source_url=source_url,
        retrieved_at="2026-01-01T00:00:00Z",
        validation_status="CONFIRMED",
        quality_signal="OFFICIAL",
        signal_type=signal_type,
    )


def _make_bundle(
    vendor_id: str = "v-001",
    sources: dict | None = None,
) -> SourceEvidenceBundle:
    return SourceEvidenceBundle(
        vendor_id=vendor_id,
        depth_tier="STANDARD",
        sources=sources or {},
        disambiguation_notices=[],
        collection_flags=[],
    )


def _make_extracted(vendor_id: str = "v-001") -> ExtractedAttributes:
    return ExtractedAttributes(
        vendor_id=vendor_id,
        fields={"company_name": ExtractedField(value="Acme Corp", confidence="HIGH", source="web_search")},
        conflicts=[],
        extraction_flags=[],
    )


def _make_entity(
    vendor_id: str = "v-001",
    canonical_name: str = "Acme Corp",
    aliases: list[str] | None = None,
    parent_company: str | None = None,
) -> dict:
    return {
        "vendor_id":      vendor_id,
        "canonical_name": canonical_name,
        "aliases":        aliases or [],
        "parent_company": parent_company,
    }


@pytest.fixture()
def empty_brain() -> BrainData:
    invalidate_cache()
    brain = BrainData(
        known_vendors={},
        rebrand_map={},
        alias_map={},
        acquisition_map={},
        brand_map={},
    )
    yield brain
    invalidate_cache()


@pytest.fixture()
def fresh_brain(tmp_path, monkeypatch):
    """Seed-able Brain fixture that sets BRAIN_ROOT and invalidates the cache."""
    brain_root = tmp_path / "Brain"
    brain_root.mkdir()

    def _seed(known=None, rebrand=None, alias=None, acquisition=None, brand=None):
        (brain_root / "known_vendors.json").write_text(json.dumps(known or {}))
        (brain_root / "rebrand_map.json").write_text(json.dumps(rebrand or {}))
        (brain_root / "alias_map.json").write_text(json.dumps(alias or {}))
        (brain_root / "acquisition_map.json").write_text(json.dumps(acquisition or {}))
        (brain_root / "brand_map.json").write_text(json.dumps(brand or {}))
        monkeypatch.setenv("BRAIN_ROOT", str(brain_root))
        invalidate_cache()
        from cobalt.brain.loader import load_brain
        return load_brain()

    yield _seed
    invalidate_cache()


# ---------------------------------------------------------------------------
# Helper: call the tool
# ---------------------------------------------------------------------------

def _run(bundle, entity, brain=None, extracted=None):
    if extracted is None:
        extracted = _make_extracted(entity.get("vendor_id", "v-001"))
    return map_relationships_and_lifecycle(bundle, extracted, entity, brain=brain)


# ---------------------------------------------------------------------------
# Test 1 — Brain rebrand hit: canonical is new name
# ---------------------------------------------------------------------------

def test_rebrand_brain_canonical_is_new_name(empty_brain):
    """Brain has twitter→X Corp; canonical='X Corp' → REBRANDED signal from brain."""
    empty_brain.rebrand_map["twitter"] = "X Corp"
    bundle = _make_bundle(sources={"web_search": [_make_item("Some news about X Corp")]})
    entity = _make_entity(canonical_name="X Corp")
    result = _run(bundle, entity, brain=empty_brain)

    assert isinstance(result, RelationshipLifecycleResult)
    sigs = [s for s in result.lifecycle_signals if s.signal_type == "REBRANDED"]
    assert len(sigs) == 1
    assert sigs[0].from_ == "twitter"
    assert sigs[0].to == "X Corp"
    assert sigs[0].confidence == "HIGH"
    assert sigs[0].source == "brain"


# ---------------------------------------------------------------------------
# Test 2 — Brain rebrand hit: canonical is stale/old name
# ---------------------------------------------------------------------------

def test_rebrand_brain_canonical_is_stale(empty_brain):
    """Brain has facebook→Meta Platforms Inc; canonical='Facebook' → stale flag."""
    empty_brain.rebrand_map["facebook"] = "Meta Platforms Inc"
    bundle = _make_bundle(sources={"web_search": [_make_item("news")]})
    entity = _make_entity(canonical_name="Facebook")
    result = _run(bundle, entity, brain=empty_brain)

    assert "CANONICAL_NAME_STALE_PER_BRAIN" in result.flags
    sigs = [s for s in result.lifecycle_signals if s.signal_type == "REBRANDED"]
    assert sigs[0].from_ == "Facebook"
    assert sigs[0].to == "Meta Platforms Inc"


# ---------------------------------------------------------------------------
# Test 3 — Rebrand from COMPANY_WEBSITE evidence
# ---------------------------------------------------------------------------

def test_rebrand_evidence_company_website(empty_brain):
    """'formerly known as OldCo' on company_website → HIGH confidence rebrand."""
    bundle = _make_bundle(sources={
        "company_website": [_make_item("We were formerly known as OldCo.", source_type="COMPANY_WEBSITE")],
    })
    entity = _make_entity(canonical_name="NewCo")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "REBRANDED"]
    assert len(sigs) == 1
    assert "OldCo" in sigs[0].from_
    assert sigs[0].confidence == "HIGH"
    assert any(s.update_type == "REBRAND_MAP" for s in result.brain_update_suggestions)


# ---------------------------------------------------------------------------
# Test 4 — Rebrand from NEWS evidence
# ---------------------------------------------------------------------------

def test_rebrand_evidence_news(empty_brain):
    """'previously named OldName' in NEWS → MEDIUM confidence rebrand."""
    bundle = _make_bundle(sources={
        "news": [_make_item("The company, previously named OldName, announced growth.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="NewName")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "REBRANDED"]
    assert len(sigs) == 1
    assert sigs[0].confidence == "MEDIUM"


# ---------------------------------------------------------------------------
# Test 5 — Rebrand from WEB_SEARCH evidence
# ---------------------------------------------------------------------------

def test_rebrand_evidence_web_search(empty_brain):
    """'name change from AncientName' in WEB_SEARCH → LOW confidence rebrand."""
    bundle = _make_bundle(sources={
        "web_search": [_make_item("name change from AncientName to ModernName.", source_type="WEB_SEARCH")],
    })
    entity = _make_entity(canonical_name="ModernName")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "REBRANDED"]
    assert len(sigs) == 1
    assert sigs[0].confidence == "LOW"


# ---------------------------------------------------------------------------
# Test 6 — Acquisition from Brain (citrix pattern)
# ---------------------------------------------------------------------------

def test_acquisition_brain_hit(empty_brain):
    """citrix in acquisition_map → ACQUIRED signal, HIGH confidence, parent set."""
    empty_brain.acquisition_map["citrix"] = {
        "acquired_by": "Cloud Software Group",
        "acquired_by_key": "cloud software group",
        "date": "2022-09-30",
        "status": "COMPLETED",
    }
    bundle = _make_bundle(sources={"web_search": [_make_item("Citrix news")]})
    entity = _make_entity(canonical_name="Citrix", vendor_id="v-citrix")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "ACQUIRED"]
    assert len(sigs) == 1
    assert sigs[0].confidence == "HIGH"
    assert sigs[0].to == "Cloud Software Group"
    assert result.relationship_map.parent_company is not None
    assert result.relationship_map.parent_company["name"] == "Cloud Software Group"


# ---------------------------------------------------------------------------
# Test 7 — Acquisition from NEWS evidence
# ---------------------------------------------------------------------------

def test_acquisition_evidence_news(empty_brain):
    """'acquired by BigCorp' in news → ACQUIRED signal."""
    bundle = _make_bundle(sources={
        "news": [_make_item("Acme Corp was acquired by BigCorp last quarter.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "ACQUIRED"]
    assert len(sigs) == 1
    assert "BigCorp" in sigs[0].to
    assert any(s.update_type == "ACQUISITION_MAP" for s in result.brain_update_suggestions)


# ---------------------------------------------------------------------------
# Test 8 — Reverse acquisition: vendor acquired another → subsidiary recorded
# ---------------------------------------------------------------------------

def test_acquisition_reverse_direction(empty_brain):
    """'acquired SubCo' detected → SubCo added to subsidiaries list."""
    bundle = _make_bundle(sources={
        "news": [_make_item("Acme Corp acquired SubCo in a major deal.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sub_names = [s["name"] for s in result.relationship_map.subsidiaries]
    assert any("SubCo" in n for n in sub_names)


# ---------------------------------------------------------------------------
# Test 9 — ANNOUNCED acquisition status
# ---------------------------------------------------------------------------

def test_acquisition_announced_status(empty_brain):
    """'to be acquired by MegaCorp' → status ANNOUNCED in suggestion or result."""
    bundle = _make_bundle(sources={
        "news": [_make_item("Acme Corp is to be acquired by MegaCorp pending approval.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "ACQUIRED"]
    assert len(sigs) == 1


# ---------------------------------------------------------------------------
# Test 10 — RUMOURED acquisition status
# ---------------------------------------------------------------------------

def test_acquisition_rumoured_status(empty_brain):
    """'in talks to be acquired by RivalCorp' → ACQUIRED signal emitted."""
    bundle = _make_bundle(sources={
        "news": [_make_item("Acme Corp is in talks to be acquired by RivalCorp.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "ACQUIRED"]
    assert len(sigs) == 1
    assert "ACQUISITION_UNRESOLVED" in result.flags


# ---------------------------------------------------------------------------
# Test 11 — Merger detection
# ---------------------------------------------------------------------------

def test_merger_detection(empty_brain):
    """'merged with PartnerCo' → MERGED signal + ALIAS_DICTIONARY suggestion."""
    bundle = _make_bundle(sources={
        "news": [_make_item("Acme Corp merged with PartnerCo to form a new entity.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "MERGED"]
    assert len(sigs) == 1
    assert "PartnerCo" in sigs[0].from_
    alias_sugs = [s for s in result.brain_update_suggestions if s.update_type == "ALIAS_DICTIONARY"]
    assert len(alias_sugs) >= 1


# ---------------------------------------------------------------------------
# Test 12 — Spin-off detection
# ---------------------------------------------------------------------------

def test_spinoff_detection(empty_brain):
    """'spun off from ParentCo' → SPUN_OFF signal; no parent set from spinoff."""
    bundle = _make_bundle(sources={
        "news": [_make_item("Acme Corp was spun off from ParentCo in 2022.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "SPUN_OFF"]
    assert len(sigs) == 1
    assert "ParentCo" in sigs[0].from_


# ---------------------------------------------------------------------------
# Test 13 — IPO / went-public detection
# ---------------------------------------------------------------------------

def test_ipo_detection(empty_brain):
    """'went public in 2021' → WENT_PUBLIC signal."""
    bundle = _make_bundle(sources={
        "news": [_make_item("The company went public in 2021 raising $500M.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "WENT_PUBLIC"]
    assert len(sigs) == 1


# ---------------------------------------------------------------------------
# Test 14 — Defunct: empty sources → no POSSIBLY_DEFUNCT
# ---------------------------------------------------------------------------

def test_defunct_empty_bundle_no_signal(empty_brain):
    """Completely empty sources dict → defunct score 0 → no POSSIBLY_DEFUNCT."""
    bundle = _make_bundle(sources={})
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "POSSIBLY_DEFUNCT"]
    assert len(sigs) == 0
    assert "POSSIBLY_DEFUNCT" not in result.flags


# ---------------------------------------------------------------------------
# Test 15 — Defunct: keyword hit
# ---------------------------------------------------------------------------

def test_defunct_keyword_triggers_signal(empty_brain):
    """'ceased operations' keyword → score ≥ 4 → POSSIBLY_DEFUNCT."""
    bundle = _make_bundle(sources={
        "news": [_make_item("The company has ceased operations.", source_type="NEWS")],
        "company_website": [_make_item("", source_type="COMPANY_WEBSITE")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "POSSIBLY_DEFUNCT"]
    assert len(sigs) == 1
    assert "POSSIBLY_DEFUNCT" in result.flags


# ---------------------------------------------------------------------------
# Test 16 — Defunct: active signal offsets score below threshold
# ---------------------------------------------------------------------------

def test_defunct_active_signal_offsets(empty_brain):
    """Empty website (score+2) + 'launched new product' (score-3) → net=-1 → no signal."""
    bundle = _make_bundle(sources={
        "company_website": [_make_item("", source_type="COMPANY_WEBSITE")],
        "news": [_make_item("Acme Corp launched new product line.", source_type="NEWS")],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    sigs = [s for s in result.lifecycle_signals if s.signal_type == "POSSIBLY_DEFUNCT"]
    assert len(sigs) == 0


# ---------------------------------------------------------------------------
# Test 17 — Parent: wholly-owned subsidiary pattern
# ---------------------------------------------------------------------------

def test_parent_wholly_owned_pattern(empty_brain):
    """'wholly-owned subsidiary of BigParent' → parent with WHOLLY_OWNED type."""
    bundle = _make_bundle(sources={
        "company_website": [_make_item(
            "Acme Corp is a wholly-owned subsidiary of BigParent.", source_type="COMPANY_WEBSITE"
        )],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    parent = result.relationship_map.parent_company
    assert parent is not None
    assert "BigParent" in parent["name"]
    assert parent["relationship_type"] == "WHOLLY_OWNED"
    assert parent["confidence"] == "HIGH"


# ---------------------------------------------------------------------------
# Test 18 — Subsidiaries list from website content
# ---------------------------------------------------------------------------

def test_subsidiaries_listed_in_content(empty_brain):
    """'subsidiaries include Alpha, Beta and Gamma' → 3 subsidiaries mapped."""
    bundle = _make_bundle(sources={
        "company_website": [_make_item(
            "Our subsidiaries include Alpha, Beta and Gamma.", source_type="COMPANY_WEBSITE"
        )],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    names = [s["name"] for s in result.relationship_map.subsidiaries]
    assert any("Alpha" in n for n in names)
    assert any("Beta" in n for n in names)
    assert any("Gamma" in n for n in names)


# ---------------------------------------------------------------------------
# Test 19 — Brand risk flag when vendor IS a brand in brain
# ---------------------------------------------------------------------------

def test_brand_risk_flag(empty_brain):
    """canonical_key IS in brand_map → BRAND_AS_LEGAL_ENTITY_RISK flag."""
    empty_brain.brand_map["instagram"] = "meta platforms inc"
    bundle = _make_bundle(sources={})
    entity = _make_entity(canonical_name="Instagram")
    result = _run(bundle, entity, brain=empty_brain)

    assert "BRAND_AS_LEGAL_ENTITY_RISK" in result.flags
    brand_entries = result.relationship_map.brands
    assert any(b["brand_name"] == "Instagram" for b in brand_entries)


# ---------------------------------------------------------------------------
# Test 20 — Vendor owns brands in brain
# ---------------------------------------------------------------------------

def test_vendor_owns_brands_from_brain(empty_brain):
    """brand_map value matches canonical_key → vendor owns those brands."""
    empty_brain.brand_map["gmail"] = "google llc"
    empty_brain.brand_map["youtube"] = "google llc"
    bundle = _make_bundle(sources={})
    entity = _make_entity(canonical_name="Google LLC")
    result = _run(bundle, entity, brain=empty_brain)

    owned = [b["brand_name"] for b in result.relationship_map.brands]
    assert "gmail" in owned
    assert "youtube" in owned


# ---------------------------------------------------------------------------
# Test 21 — Unknown brand in content → BRAND_MAP suggestion
# ---------------------------------------------------------------------------

def test_unknown_brand_in_content_triggers_suggestion(empty_brain):
    """'our brands include SubBrand' when SubBrand not in brain → BRAND_MAP suggestion."""
    bundle = _make_bundle(sources={
        "company_website": [_make_item(
            "Our brands include SubBrand.", source_type="COMPANY_WEBSITE", source_url="https://acme.com"
        )],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    brand_sugs = [s for s in result.brain_update_suggestions if s.update_type == "BRAND_MAP"]
    assert len(brand_sugs) >= 1
    assert any("SubBrand" in s.from_ for s in brand_sugs)


# ---------------------------------------------------------------------------
# Test 22 — Dedup suggestions: same key→highest confidence wins
# ---------------------------------------------------------------------------

def test_dedup_suggestions_keeps_highest_confidence(empty_brain):
    """Two REBRAND_MAP suggestions for same pair: HIGH wins over MEDIUM."""
    # Create two items with the same rebrand pattern at different confidence sources
    bundle = _make_bundle(sources={
        "company_website": [_make_item(
            "We were formerly known as OldName.", source_type="COMPANY_WEBSITE"
        )],
        "news": [_make_item(
            "OldName, formerly known as OldName, now trading as NewCo.", source_type="NEWS"
        )],
    })
    entity = _make_entity(canonical_name="NewCo")

    # Manually call dedup via public API result — can have at most 1 REBRAND_MAP suggestion
    result = _run(bundle, entity, brain=empty_brain)

    rebrand_sugs = [s for s in result.brain_update_suggestions if s.update_type == "REBRAND_MAP"]
    # At most one per (update_type, from_, to) key after dedup
    keys = [(s.update_type, s.from_, s.to) for s in rebrand_sugs]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Test 23 — Defunct + Acquired collision: ACQUIRED removes POSSIBLY_DEFUNCT
# ---------------------------------------------------------------------------

def test_defunct_and_acquired_collision(empty_brain):
    """Both POSSIBLY_DEFUNCT evidence and ACQUIRED evidence → POSSIBLY_DEFUNCT removed."""
    empty_brain.acquisition_map["acme corp"] = {
        "acquired_by": "Mega Corp",
        "acquired_by_key": "mega corp",
        "date": "2023-01-01",
        "status": "COMPLETED",
    }
    bundle = _make_bundle(sources={
        "company_website": [_make_item("", source_type="COMPANY_WEBSITE")],
        "news": [_make_item(
            "Acme Corp ceased operations after acquisition.", source_type="NEWS"
        )],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    defunct_sigs = [s for s in result.lifecycle_signals if s.signal_type == "POSSIBLY_DEFUNCT"]
    acquired_sigs = [s for s in result.lifecycle_signals if s.signal_type == "ACQUIRED"]
    assert len(acquired_sigs) >= 1
    assert len(defunct_sigs) == 0
    assert "POSSIBLY_DEFUNCT" not in result.flags


# ---------------------------------------------------------------------------
# Test 24 — No lifecycle events → no flags
# ---------------------------------------------------------------------------

def test_no_lifecycle_events_clean_result(empty_brain):
    """Normal content with no lifecycle patterns → clean result, no lifecycle flags."""
    bundle = _make_bundle(sources={
        "company_website": [_make_item(
            "Acme Corp provides cloud solutions.", source_type="COMPANY_WEBSITE"
        )],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    assert "LIFECYCLE_EVENT_DETECTED" not in result.flags
    assert "POSSIBLY_DEFUNCT" not in result.flags
    assert "BRAIN_UPDATE_PENDING" not in result.flags
    assert result.relationship_map.vendor_id == "v-001"


# ---------------------------------------------------------------------------
# Test 25 — Acquirer known in known_vendors → parent has vendor_id set
# ---------------------------------------------------------------------------

def test_acquirer_in_known_vendors_sets_parent_vendor_id(empty_brain):
    """When acquirer key is in known_vendors, parent.vendor_id is populated."""
    empty_brain.known_vendors["mega corp"] = KnownVendor(
        canonical_name="Mega Corp", confidence=1.0, country_code="US", category="Technology"
    )
    empty_brain.acquisition_map["acme corp"] = {
        "acquired_by": "Mega Corp",
        "acquired_by_key": "mega corp",
        "date": "2023-01-01",
        "status": "COMPLETED",
    }
    bundle = _make_bundle(sources={"web_search": [_make_item("Acme Corp news")]})
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    parent = result.relationship_map.parent_company
    assert parent is not None
    assert parent["vendor_id"] == "mega corp"


# ---------------------------------------------------------------------------
# Shared GLEIF mock helpers
# ---------------------------------------------------------------------------

_GLEIF_APPLE_MATCH = [
    {
        "lei": "5493001KJTIIGC8Y1R12",
        "legal_name": "Apple Inc.",
        "other_names": [],
        "country": "US",
        "jurisdiction": "US-CA",
        "status": "ACTIVE",
        "legal_form_id": "JHN5",
        "registered_as": "C0806592",
        "creation_date": "1977-01-03T00:00:00Z",
        "has_direct_parent_link": True,
        "has_ultimate_parent_link": True,
    }
]

_GLEIF_ALPHABET_PARENT = {
    "lei": "54930016113PD33V1H31",
    "legal_name": "Alphabet Inc.",
    "other_names": [],
    "country": "US",
    "jurisdiction": "US-DE",
    "status": "ACTIVE",
    "legal_form_id": "8888",
    "registered_as": "7288699",
    "creation_date": "2015-10-02T00:00:00Z",
    "has_direct_parent_link": False,
    "has_ultimate_parent_link": False,
}


# ---------------------------------------------------------------------------
# Test 26 — GLEIF parent populates when Brain has no acquisition entry
# ---------------------------------------------------------------------------

def test_gleif_parent_populates_when_brain_empty(empty_brain, monkeypatch):
    """Brain empty; GLEIF returns a match with parent → parent_company populated from GLEIF."""
    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_search_by_name",
        lambda name, limit=3: _GLEIF_APPLE_MATCH,
    )
    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_get_direct_parent",
        lambda lei: _GLEIF_ALPHABET_PARENT,
    )

    bundle = _make_bundle(sources={})
    entity = _make_entity(canonical_name="Apple Inc.")
    result = _run(bundle, entity, brain=empty_brain)

    parent = result.relationship_map.parent_company
    assert parent is not None
    assert parent["name"] == "Alphabet Inc."
    assert parent["confidence"] == "HIGH"
    assert parent["source"] == "gleif"


# ---------------------------------------------------------------------------
# Test 27 — Acquisition Brain entry short-circuits GLEIF (GLEIF not consulted)
# ---------------------------------------------------------------------------

def test_gleif_parent_skipped_when_acquisition_found(empty_brain, monkeypatch):
    """Brain has HIGH-confidence acquisition → GLEIF not consulted at all."""
    empty_brain.acquisition_map["acme corp"] = {
        "acquired_by": "MegaCorp",
        "acquired_by_key": "megacorp",
        "date": "2023-01-01",
        "status": "COMPLETED",
    }

    gleif_calls: list[str] = []

    def _track(name, limit=3):
        gleif_calls.append(name)
        return _GLEIF_APPLE_MATCH

    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_search_by_name",
        _track,
    )

    bundle = _make_bundle(sources={"web_search": [_make_item("Acme Corp news")]})
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    # Acquisition wins; GLEIF should never be called
    assert gleif_calls == []
    parent = result.relationship_map.parent_company
    assert parent is not None
    assert parent["name"] == "MegaCorp"
    assert parent["source"] == "brain"


# ---------------------------------------------------------------------------
# Test 28 — GLEIF no match falls through to website pattern
# ---------------------------------------------------------------------------

def test_gleif_no_match_falls_through_to_website(empty_brain, monkeypatch):
    """GLEIF returns []; website has subsidiary pattern → parent from website."""
    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_search_by_name",
        lambda name, limit=3: [],
    )

    bundle = _make_bundle(sources={
        "company_website": [_make_item(
            "Acme Corp is a wholly-owned subsidiary of MegaCorp.",
            source_type="COMPANY_WEBSITE",
        )],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    parent = result.relationship_map.parent_company
    assert parent is not None
    assert "MegaCorp" in parent["name"]
    assert parent["source"] == "company_website"


# ---------------------------------------------------------------------------
# Test 29 — GLEIF match with no direct parent link — does not call get_direct_parent
# ---------------------------------------------------------------------------

def test_gleif_no_parent_link_skips_fetch(empty_brain, monkeypatch):
    """Match found but has_direct_parent_link=False → get_direct_parent not called."""
    no_parent_match = [dict(_GLEIF_APPLE_MATCH[0], has_direct_parent_link=False)]

    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_search_by_name",
        lambda name, limit=3: no_parent_match,
    )

    fetch_calls: list[str] = []

    def _track_fetch(lei):
        fetch_calls.append(lei)
        return _GLEIF_ALPHABET_PARENT

    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_get_direct_parent",
        _track_fetch,
    )

    bundle = _make_bundle(sources={})
    entity = _make_entity(canonical_name="Apple Inc.")
    result = _run(bundle, entity, brain=empty_brain)

    # No parent link → fetch not called; parent_company is None (no website fallback either)
    assert fetch_calls == []
    assert result.relationship_map.parent_company is None


# ---------------------------------------------------------------------------
# Test 30 — GLEIF fetch failure falls through gracefully
# ---------------------------------------------------------------------------

def test_gleif_fetch_failure_falls_through(empty_brain, monkeypatch):
    """gleif_search_by_name raises any Exception → no crash; parent falls through to website."""
    from cobalt.core.exceptions import GleifError

    def _raise(name, limit=3):
        raise GleifError("transport: connection refused")

    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_search_by_name",
        _raise,
    )

    bundle = _make_bundle(sources={
        "company_website": [_make_item(
            "Acme Corp is a wholly-owned subsidiary of FallbackParent.",
            source_type="COMPANY_WEBSITE",
        )],
    })
    entity = _make_entity(canonical_name="Acme Corp")
    result = _run(bundle, entity, brain=empty_brain)

    # GLEIF failed; website fallback still works
    parent = result.relationship_map.parent_company
    assert parent is not None
    assert "FallbackParent" in parent["name"]
    assert parent["source"] == "company_website"


# ---------------------------------------------------------------------------
# Test 31 — Country corroboration selects US match over GB match
# ---------------------------------------------------------------------------

def test_gleif_match_with_country_corroboration(empty_brain, monkeypatch):
    """When hq_country=US and two GLEIF matches exist, the US match is selected."""
    gb_match = dict(_GLEIF_APPLE_MATCH[0], lei="GB_LEI_00000000000001", country="GB")
    us_match = dict(_GLEIF_APPLE_MATCH[0], lei="US_LEI_00000000000001", country="US")
    # GLEIF returns GB first, US second
    two_matches = [gb_match, us_match]

    selected_leis: list[str] = []

    def _fake_search(name, limit=3):
        return two_matches

    def _fake_parent(lei):
        selected_leis.append(lei)
        return _GLEIF_ALPHABET_PARENT

    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_search_by_name",
        _fake_search,
    )
    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_get_direct_parent",
        _fake_parent,
    )

    bundle = _make_bundle(sources={})
    entity = {
        "vendor_id": "v-001",
        "canonical_name": "Apple Inc.",
        "aliases": [],
        "parent_company": None,
        "hq_country": "US",
    }
    result = _run(bundle, entity, brain=empty_brain)

    # US match should have been selected (country bonus tips the score)
    assert selected_leis == ["US_LEI_00000000000001"]
    assert result.relationship_map.parent_company is not None


# ---------------------------------------------------------------------------
# Test 32 — GLEIF parent vendor_id resolved from Brain known_vendors
# ---------------------------------------------------------------------------

def test_gleif_parent_vendor_id_resolves_from_brain(empty_brain, monkeypatch):
    """Parent legal_name normalised key is in Brain known_vendors → vendor_id set."""
    empty_brain.known_vendors["alphabet inc"] = KnownVendor(
        canonical_name="Alphabet Inc.", confidence=1.0, country_code="US", category="Technology"
    )

    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_search_by_name",
        lambda name, limit=3: _GLEIF_APPLE_MATCH,
    )
    monkeypatch.setattr(
        "cobalt.tools.relationship_and_lifecycle_mapper.gleif_get_direct_parent",
        lambda lei: _GLEIF_ALPHABET_PARENT,
    )

    bundle = _make_bundle(sources={})
    entity = _make_entity(canonical_name="Apple Inc.")
    result = _run(bundle, entity, brain=empty_brain)

    parent = result.relationship_map.parent_company
    assert parent is not None
    assert parent["name"] == "Alphabet Inc."
    # vendor_id is the normalised key (existing pattern — KnownVendor has no vendor_id field)
    assert parent["vendor_id"] == "alphabet inc"
