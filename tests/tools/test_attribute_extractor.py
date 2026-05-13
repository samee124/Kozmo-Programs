"""Tests for attribute_extractor — Process 2 Tool 3."""

from __future__ import annotations

import pytest

from cobalt.core.exceptions import LLMCallFailure
from cobalt.models.schemas.enrichment_schema import (
    KnownFacts,
    SourceEvidenceBundle,
    SourceEvidenceItem,
)
from cobalt.tools.attribute_extractor import (
    _filter_marketing_language,
    extract_attributes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VENDOR_ID = "V-AX-001"
_NOW = "2026-01-01T00:00:00+00:00"


def _site_item(content: str, quality: str = "OFFICIAL") -> SourceEvidenceItem:
    return SourceEvidenceItem(
        content=content,
        source_type="COMPANY_WEBSITE",
        source_url="https://acme.com",
        retrieved_at=_NOW,
        validation_status="CONFIRMED",
        quality_signal=quality,
        signal_type=None,
    )


def _web_item(content: str) -> SourceEvidenceItem:
    return SourceEvidenceItem(
        content=content,
        source_type="WEB_SEARCH",
        source_url="https://example.com/search",
        retrieved_at=_NOW,
        validation_status="CONFIRMED",
        quality_signal="OFFICIAL",
        signal_type=None,
    )


def _bundle(
    site_content: str | None = None,
    web_content: str | None = None,
    collection_flags: list[str] | None = None,
) -> SourceEvidenceBundle:
    sources: dict[str, list[SourceEvidenceItem]] = {}
    if site_content is not None:
        sources["company_website"] = [_site_item(site_content)]
    if web_content is not None:
        sources["web_search"] = [_web_item(web_content)]
    return SourceEvidenceBundle(
        vendor_id=VENDOR_ID,
        depth_tier="STANDARD",
        sources=sources,
        disambiguation_notices=[],
        collection_flags=collection_flags or [],
    )


def _empty_bundle() -> SourceEvidenceBundle:
    return SourceEvidenceBundle(
        vendor_id=VENDOR_ID,
        depth_tier="STANDARD",
        sources={},
        disambiguation_notices=[],
        collection_flags=[],
    )


def _llm_resp(**field_values) -> dict:
    """Build a minimal valid LLM extraction response."""
    fields = {
        name: {"value": val, "confidence": "MEDIUM", "source": "web_search"}
        for name, val in field_values.items()
    }
    return {
        "fields": fields,
        "products_and_services": [],
        "competitors": [],
        "certifications": [],
        "customer_segments": [],
        "reputation_signals": [],
        "conflicts": [],
    }


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []
    response = {"value": {}}

    def fake_llm_call(prompt, system=None, model="gpt-4o", **kw):
        calls.append({"prompt": prompt, "system": system, "model": model, "kw": kw})
        return response["value"]

    monkeypatch.setattr("cobalt.core.llm_call.llm_call", fake_llm_call)
    monkeypatch.setattr("cobalt.tools.attribute_extractor.llm_call", fake_llm_call, raising=False)
    return calls, response


# ---------------------------------------------------------------------------
# Marketing filter — tests 1–5
# ---------------------------------------------------------------------------

def test_worlds_leading_replaced():
    """Test 1: "world's leading" replaced with [MARKETING_CLAIM_REMOVED]."""
    content = "We are the world's leading provider of cloud solutions."
    filtered = _filter_marketing_language(_bundle(site_content=content))
    item = filtered.sources["company_website"][0]
    assert "[MARKETING_CLAIM_REMOVED]" in item.content
    assert "world's leading" not in item.content.lower()


def test_cutting_edge_replaced():
    """Test 2: "cutting-edge" replaced with [MARKETING_CLAIM_REMOVED]."""
    content = "Our cutting-edge platform enables digital work."
    filtered = _filter_marketing_language(_bundle(site_content=content))
    item = filtered.sources["company_website"][0]
    assert "[MARKETING_CLAIM_REMOVED]" in item.content
    assert "cutting-edge" not in item.content.lower()


def test_puffery_sentence_dropped():
    """Test 3: Sentence with "our mission is to transform" is dropped entirely."""
    content = "Our mission is to transform how enterprises operate at scale. We make software."
    filtered = _filter_marketing_language(_bundle(site_content=content))
    item = filtered.sources["company_website"][0]
    assert "our mission is to transform" not in item.content.lower()
    assert "We make software" in item.content


def test_marketing_heavy_source_flag():
    """Test 4: >30% of content is marketing language → MARKETING_HEAVY_SOURCE flag."""
    content = "innovative cutting-edge world's leading best-in-class seamless revolutionary platform"
    filtered = _filter_marketing_language(_bundle(site_content=content))
    assert "MARKETING_HEAVY_SOURCE" in filtered.extraction_flags


def test_original_bundle_content_unchanged():
    """Test 5: Original bundle.sources content is unmodified after filter (deep copy verified)."""
    content = "world's leading innovative platform"
    b = _bundle(site_content=content)
    original = b.sources["company_website"][0].content
    filtered = _filter_marketing_language(b)
    # Original unchanged
    assert b.sources["company_website"][0].content == original
    # Filtered copy is different
    assert filtered.sources["company_website"][0].content != original


# ---------------------------------------------------------------------------
# LLM extraction happy paths — tests 6–9
# ---------------------------------------------------------------------------

def test_five_fields_stored(fake_llm):
    """Test 6: LLM returns 5 fields → ExtractedAttributes.fields has those 5 keys."""
    calls, response = fake_llm
    response["value"] = {
        "fields": {
            "website":     {"value": "https://acme.com", "confidence": "HIGH",   "source": "company_website"},
            "description": {"value": "Acme Corp builds software.", "confidence": "HIGH", "source": "company_website"},
            "hq_city":     {"value": "London",  "confidence": "MEDIUM", "source": "web_search"},
            "hq_country":  {"value": "GB",      "confidence": "MEDIUM", "source": "web_search"},
            "category":    {"value": "IT_SOFTWARE", "confidence": "HIGH", "source": "company_website"},
        },
        "products_and_services": [], "competitors": [], "certifications": [],
        "customer_segments": [], "reputation_signals": [], "conflicts": [],
    }
    result = extract_attributes(_bundle(site_content="Acme makes software."), KnownFacts())
    for key in ("website", "description", "hq_city", "hq_country", "category"):
        assert key in result.fields, f"Missing field: {key}"
    assert len(calls) == 1


def test_high_confidence_description_stored(fake_llm):
    """Test 7: LLM returns description with confidence=HIGH → stored with HIGH."""
    calls, response = fake_llm
    response["value"] = _llm_resp(
        description="Acme Corp provides cloud services.",
        category="IT_SOFTWARE",
    )
    response["value"]["fields"]["description"]["confidence"] = "HIGH"
    result = extract_attributes(_bundle(web_content="Acme Corp cloud."), KnownFacts())
    assert result.fields["description"].confidence == "HIGH"


def test_category_value_stored(fake_llm):
    """Test 8: LLM returns category=IT_SOFTWARE → fields["category"].value matches."""
    calls, response = fake_llm
    response["value"] = _llm_resp(category="IT_SOFTWARE", description="Software company.")
    result = extract_attributes(_bundle(web_content="Software."), KnownFacts())
    assert result.fields["category"].value == "IT_SOFTWARE"


def test_prompt_contains_all_gaps(fake_llm):
    """Test 9: Prompt sent to LLM includes every known_facts.gaps entry verbatim."""
    calls, response = fake_llm
    response["value"] = _llm_resp(description="A company.", category="IT_SOFTWARE")
    gaps = ["description", "hq_city", "founding_year"]
    kf = KnownFacts(confirmed=["canonical_name"], gaps=gaps)
    extract_attributes(_bundle(web_content="Company info."), kf)
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    for gap in gaps:
        assert gap in prompt, f"Gap '{gap}' not found in prompt"
    assert "PRIORITY GAPS" in prompt


# ---------------------------------------------------------------------------
# Inference rules — tests 10–13
# ---------------------------------------------------------------------------

def test_size_band_inferred_from_employee_count(fake_llm):
    """Test 10: employee=501-1000, no size_band → company_size_band inferred as MID_MARKET."""
    calls, response = fake_llm
    response["value"] = {
        "fields": {
            "employee_count_range": {"value": "501-1000", "confidence": "MEDIUM", "source": "web_search"},
            "company_size_band":    {"value": None,       "confidence": "MISSING", "source": ""},
            "description":          {"value": "A company.", "confidence": "MEDIUM", "source": "web_search"},
            "category":             {"value": "IT_SOFTWARE", "confidence": "MEDIUM", "source": "web_search"},
        },
        "products_and_services": [], "competitors": [], "certifications": [],
        "customer_segments": [], "reputation_signals": [], "conflicts": [],
    }
    result = extract_attributes(_bundle(web_content="Company info."), KnownFacts())
    assert result.fields["company_size_band"].value == "MID_MARKET"
    assert result.fields["company_size_band"].confidence == "INFERRED"


def test_size_signals_conflict_employee_vs_revenue(fake_llm):
    """Test 11: employee=51-200 (SMB) + revenue=$1B-$10B (ENTERPRISE) → SIZE_SIGNALS_CONFLICT."""
    calls, response = fake_llm
    response["value"] = {
        "fields": {
            "employee_count_range": {"value": "51-200",   "confidence": "MEDIUM", "source": "linkedin"},
            "revenue_range":        {"value": "$1B-$10B", "confidence": "MEDIUM", "source": "financial"},
            "company_size_band":    {"value": None,       "confidence": "MISSING", "source": ""},
            "description":          {"value": "A company.", "confidence": "MEDIUM", "source": "web_search"},
            "category":             {"value": "IT_SOFTWARE", "confidence": "MEDIUM", "source": "web_search"},
        },
        "products_and_services": [], "competitors": [], "certifications": [],
        "customer_segments": [], "reputation_signals": [], "conflicts": [],
    }
    result = extract_attributes(_bundle(web_content="Company info."), KnownFacts())
    assert "SIZE_SIGNALS_CONFLICT" in result.extraction_flags
    conflict = next(
        (c for c in result.conflicts if c.get("field") == "company_size_band"), None
    )
    assert conflict is not None
    assert conflict["source_a"]["value"] == "SMB"
    assert conflict["source_b"]["value"] == "ENTERPRISE"


def test_explicit_size_band_not_overwritten(fake_llm):
    """Test 12: LLM returns both employee and explicit size_band → no inference overwrites."""
    calls, response = fake_llm
    response["value"] = {
        "fields": {
            "employee_count_range": {"value": "501-1000",  "confidence": "MEDIUM", "source": "web_search"},
            "company_size_band":    {"value": "ENTERPRISE", "confidence": "HIGH",   "source": "financial"},
            "description":          {"value": "A company.", "confidence": "MEDIUM", "source": "web_search"},
            "category":             {"value": "IT_SOFTWARE", "confidence": "MEDIUM", "source": "web_search"},
        },
        "products_and_services": [], "competitors": [], "certifications": [],
        "customer_segments": [], "reputation_signals": [], "conflicts": [],
    }
    result = extract_attributes(_bundle(web_content="Company."), KnownFacts())
    assert result.fields["company_size_band"].value == "ENTERPRISE"
    assert result.fields["company_size_band"].confidence == "HIGH"


def test_size_band_present_no_employee_no_inference(fake_llm):
    """Test 13: LLM returns size_band but no employee → no inference, no flag."""
    calls, response = fake_llm
    response["value"] = _llm_resp(
        company_size_band="MID_MARKET",
        description="A company.",
        category="IT_SOFTWARE",
    )
    result = extract_attributes(_bundle(web_content="Company."), KnownFacts())
    assert result.fields["company_size_band"].value == "MID_MARKET"
    assert result.fields["company_size_band"].confidence == "MEDIUM"
    assert "SIZE_SIGNALS_CONFLICT" not in result.extraction_flags


# ---------------------------------------------------------------------------
# Conflict handling — tests 14–16
# ---------------------------------------------------------------------------

def test_llm_conflict_passed_through(fake_llm):
    """Test 14: LLM includes conflict → conflicts list populated, field confidence=CONFLICT."""
    calls, response = fake_llm
    response["value"] = {
        "fields": {
            "employee_count_range": {"value": "501-1000", "confidence": "CONFLICT", "source": "linkedin"},
            "description":          {"value": "A company.", "confidence": "MEDIUM",  "source": "web_search"},
            "category":             {"value": "IT_SOFTWARE","confidence": "MEDIUM",  "source": "web_search"},
        },
        "products_and_services": [], "competitors": [], "certifications": [],
        "customer_segments": [], "reputation_signals": [],
        "conflicts": [
            {
                "field":    "employee_count_range",
                "source_a": {"value": "501-1000", "source_type": "linkedin"},
                "source_b": {"value": "51-200",   "source_type": "registry"},
            }
        ],
    }
    result = extract_attributes(_bundle(web_content="Company."), KnownFacts())
    conflict = next(
        (c for c in result.conflicts if c.get("field") == "employee_count_range"), None
    )
    assert conflict is not None
    assert result.fields["employee_count_range"].confidence == "CONFLICT"


def test_conflict_field_confidence_fixed_by_postprocessing(fake_llm):
    """Test 15: LLM records conflict but sets field confidence=MEDIUM → post-process fixes to CONFLICT."""
    calls, response = fake_llm
    response["value"] = {
        "fields": {
            "employee_count_range": {"value": "501-1000", "confidence": "MEDIUM", "source": "linkedin"},
            "description":          {"value": "A company.", "confidence": "MEDIUM", "source": "web_search"},
            "category":             {"value": "IT_SOFTWARE","confidence": "MEDIUM", "source": "web_search"},
        },
        "products_and_services": [], "competitors": [], "certifications": [],
        "customer_segments": [], "reputation_signals": [],
        "conflicts": [
            {
                "field":    "employee_count_range",
                "source_a": {"value": "501-1000", "source_type": "linkedin"},
                "source_b": {"value": "51-200",   "source_type": "registry"},
            }
        ],
    }
    result = extract_attributes(_bundle(web_content="Company."), KnownFacts())
    assert result.fields["employee_count_range"].confidence == "CONFLICT"


def test_no_conflicts_when_llm_reports_none(fake_llm):
    """Test 16: LLM reports no conflicts and no size signal conflict → conflicts is empty."""
    calls, response = fake_llm
    response["value"] = _llm_resp(description="A company.", category="IT_SOFTWARE")
    result = extract_attributes(_bundle(web_content="Company."), KnownFacts())
    assert result.conflicts == []


# ---------------------------------------------------------------------------
# Flags — tests 17–20
# ---------------------------------------------------------------------------

def test_missing_category_flag(fake_llm):
    """Test 17: LLM returns no category → MISSING_CATEGORY flag."""
    calls, response = fake_llm
    response["value"] = _llm_resp(description="A company.")
    result = extract_attributes(_bundle(web_content="Company."), KnownFacts())
    assert "MISSING_CATEGORY" in result.extraction_flags


def test_no_description_extractable_flag(fake_llm):
    """Test 18: LLM returns description="" → NO_DESCRIPTION_EXTRACTABLE flag."""
    calls, response = fake_llm
    response["value"] = _llm_resp(category="IT_SOFTWARE", description="")
    result = extract_attributes(_bundle(web_content="Company."), KnownFacts())
    assert "NO_DESCRIPTION_EXTRACTABLE" in result.extraction_flags


def test_invalid_confidence_coerced_to_low(fake_llm):
    """Test 19: LLM returns confidence="EXTREMELY_HIGH" → coerced to LOW, flag set."""
    calls, response = fake_llm
    response["value"] = {
        "fields": {
            "website":     {"value": "https://acme.com", "confidence": "EXTREMELY_HIGH", "source": "company_website"},
            "description": {"value": "A company.",       "confidence": "MEDIUM",          "source": "web_search"},
            "category":    {"value": "IT_SOFTWARE",      "confidence": "MEDIUM",          "source": "web_search"},
        },
        "products_and_services": [], "competitors": [], "certifications": [],
        "customer_segments": [], "reputation_signals": [], "conflicts": [],
    }
    result = extract_attributes(_bundle(web_content="Company."), KnownFacts())
    assert result.fields["website"].confidence == "LOW"
    assert "LLM_INVALID_CONFIDENCE_website" in result.extraction_flags


def test_category_conflict_flag_when_confirmed_and_extracted(fake_llm):
    """Test 20: known_facts.confirmed=["category"] + LLM extracts category → CATEGORY_CONFLICT."""
    calls, response = fake_llm
    response["value"] = _llm_resp(category="FINANCE", description="A finance company.")
    kf = KnownFacts(confirmed=["category"])
    result = extract_attributes(_bundle(web_content="Finance."), kf)
    assert "CATEGORY_CONFLICT" in result.extraction_flags


# ---------------------------------------------------------------------------
# Edge cases — tests 21–22
# ---------------------------------------------------------------------------

def test_empty_bundle_no_llm_call(fake_llm):
    """Test 21: Empty bundle → no LLM call, NO_EVIDENCE_TO_EXTRACT flag."""
    calls, response = fake_llm
    result = extract_attributes(_empty_bundle(), KnownFacts())
    assert "NO_EVIDENCE_TO_EXTRACT" in result.extraction_flags
    assert len(calls) == 0
    assert result.fields == {}


def test_llm_exception_returns_extraction_failed(monkeypatch):
    """Test 22: LLM raises Exception → EXTRACTION_FAILED flag, no crash."""
    def _boom(prompt, system, **kw):
        raise LLMCallFailure("network error")

    monkeypatch.setattr("cobalt.tools.attribute_extractor.llm_call", _boom)
    result = extract_attributes(_bundle(web_content="Company info."), KnownFacts())
    assert "EXTRACTION_FAILED" in result.extraction_flags
    assert result.fields == {}


# ---------------------------------------------------------------------------
# Wikidata note in system prompt — test 23 (global 45)
# ---------------------------------------------------------------------------

def test_wikidata_note_in_system_prompt(fake_llm):
    """Test 23 (global 45): WIKIDATA note appears in system prompt passed to LLM."""
    calls, response = fake_llm
    response["value"] = _llm_resp(description="A company.", category="IT_SOFTWARE")

    wd_item = SourceEvidenceItem(
        content='{"qid":"Q312","label":"Acme Corp","industries":["Software"]}',
        source_type="WIKIDATA",
        source_url="https://www.wikidata.org/wiki/Q312",
        retrieved_at=_NOW,
        validation_status="LIKELY",
        quality_signal="DIRECTORY",
        signal_type=None,
    )
    b = SourceEvidenceBundle(
        vendor_id=VENDOR_ID,
        depth_tier="STANDARD",
        sources={"wikidata": [wd_item]},
        disambiguation_notices=[],
        collection_flags=[],
    )
    extract_attributes(b, KnownFacts())
    assert len(calls) == 1
    system_prompt = calls[0]["system"] or ""
    assert "WIKIDATA" in system_prompt
