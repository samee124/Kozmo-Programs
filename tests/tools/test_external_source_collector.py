"""Tests for external_source_collector — Process 2 Tool 2."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from cobalt.core.exceptions import BraveSearchError
from cobalt.core.search import brave_search, classify_url_quality
from cobalt.models.schemas.enrichment_schema import (
    EnrichmentReadinessResult,
    KnownFacts,
)
from cobalt.tools.external_source_collector import (
    _SearchContext,
    _collect_contract_evidence,
    _validate_against_entity,
    collect_sources,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VENDOR_ID    = "V-TST-001"
PROGRAMME_ID = "prog-collect"


def _readiness(source_list: list[str], depth_tier: str = "STANDARD") -> EnrichmentReadinessResult:
    return EnrichmentReadinessResult(
        vendor_id=VENDOR_ID,
        proceed=True,
        skip=False,
        skip_reason=None,
        depth_tier=depth_tier,
        source_list=source_list,
        query_count=len(source_list),
        known_facts=KnownFacts(),
        confidence_floor=0.85,
        flags=[],
    )


def _entity(
    canonical_name: str = "Acme Corp",
    domain: str | None = "acme.com",
    website: str | None = None,
    hq_country: str | None = None,
) -> dict:
    data: dict = {"canonical_name": canonical_name, "status": "ACTIVE", "confidence": 0.85}
    if domain is not None:
        data["domain"] = domain
    if website is not None:
        data["website"] = website
    if hq_country is not None:
        data["hq_country"] = hq_country
    return data


def _ctx(canonical_name: str = "Acme Corp", domain: str | None = "acme.com") -> _SearchContext:
    return _SearchContext(
        vendor_id=VENDOR_ID,
        canonical_name=canonical_name,
        domain=domain,
        website=None,
        hq_country=None,
        source_list=["web_search"],
        depth_tier="STANDARD",
    )


# ---------------------------------------------------------------------------
# Tests 1–3: web_search collector
# ---------------------------------------------------------------------------

def test_web_search_returns_source_evidence_items(monkeypatch):
    """Test 1: Brave returns 2 results → 2 items in sources['web_search']."""
    results = [
        {"url": "https://acme.com/about", "content": "Acme Corp is a technology company.",
         "title": "About Acme", "published_date": None},
        {"url": "https://example.com/profile", "content": "Acme Corp profile page.",
         "title": "Acme Profile", "published_date": None},
    ]
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.brave_search",
        lambda q, *, result_type="web", count=10, freshness=None: results,
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["web_search"]), _entity())
    assert len(bundle.sources["web_search"]) == 2
    assert all(item.source_type == "WEB_SEARCH" for item in bundle.sources["web_search"])
    assert bundle.vendor_id == VENDOR_ID
    assert bundle.depth_tier == "STANDARD"


def test_web_search_empty_results_returns_no_items(monkeypatch):
    """Test 2: Brave returns [] → sources['web_search'] is empty, no failure flag."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.brave_search",
        lambda q, *, result_type="web", count=10, freshness=None: [],
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["web_search"]), _entity())
    assert bundle.sources["web_search"] == []
    assert "WEB_SEARCH_FETCH_ERROR" not in bundle.collection_flags


def test_web_search_failure_adds_flag(monkeypatch):
    """Test 3: Brave raises Exception → WEB_SEARCH_FETCH_ERROR in collection_flags."""
    def _boom(q, *, result_type="web", count=10, freshness=None):
        raise RuntimeError("network error")

    monkeypatch.setattr("cobalt.tools.external_source_collector.brave_search", _boom)
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["web_search"]), _entity())
    assert "WEB_SEARCH_FETCH_ERROR" in bundle.collection_flags
    assert bundle.sources.get("web_search", []) == []


# ---------------------------------------------------------------------------
# Tests 4–8: news collector
# ---------------------------------------------------------------------------

def test_news_results_have_news_source_type(monkeypatch):
    """Test 4: Brave news results → source_type=NEWS."""
    results = [{"url": "https://reuters.com/article/1", "content": "Acme Corp announces expansion.",
                "title": "Acme expands", "published_date": None}]
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.brave_search",
        lambda q, *, result_type="web", count=10, freshness=None: results,
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["news"]), _entity())
    assert len(bundle.sources["news"]) == 1
    assert bundle.sources["news"][0].source_type == "NEWS"


def test_news_rebrand_keyword_sets_signal_type(monkeypatch):
    """Test 5: Content with 'rebrand' → signal_type='REBRANDED'."""
    results = [{"url": "https://techcrunch.com/rebrand", "content": "Acme Corp announces rebrand.",
                "title": "Rebrand", "published_date": None}]
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.brave_search",
        lambda q, *, result_type="web", count=10, freshness=None: results,
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["news"]), _entity())
    assert bundle.sources["news"][0].signal_type == "REBRANDED"


def test_news_acquired_keyword_sets_signal_type(monkeypatch):
    """Test 6: Content with 'acquisition' → signal_type='ACQUIRED'."""
    results = [{"url": "https://wsj.com/deal", "content": "BigCo completes acquisition of Acme Corp.",
                "title": "Deal", "published_date": None}]
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.brave_search",
        lambda q, *, result_type="web", count=10, freshness=None: results,
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["news"]), _entity())
    assert bundle.sources["news"][0].signal_type == "ACQUIRED"


def test_news_no_keyword_signal_type_is_none(monkeypatch):
    """Test 7: No lifecycle keyword in content → signal_type=None."""
    results = [{"url": "https://reuters.com/q3", "content": "Acme Corp reports Q3 earnings.",
                "title": "Q3", "published_date": None}]
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.brave_search",
        lambda q, *, result_type="web", count=10, freshness=None: results,
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["news"]), _entity())
    assert bundle.sources["news"][0].signal_type is None


def test_news_failure_adds_flag(monkeypatch):
    """Test 8: Brave raises for news → NEWS_SEARCH_FETCH_ERROR in flags."""
    def _boom(q, *, result_type="web", count=10, freshness=None):
        raise ConnectionError("timeout")

    monkeypatch.setattr("cobalt.tools.external_source_collector.brave_search", _boom)
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["news"]), _entity())
    assert "NEWS_SEARCH_FETCH_ERROR" in bundle.collection_flags


# ---------------------------------------------------------------------------
# Tests 9–12: company_website collector
# ---------------------------------------------------------------------------

def test_company_website_success_returns_official_item(monkeypatch):
    """Test 9: Successful fetch → one COMPANY_WEBSITE item with OFFICIAL quality."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.fetch_url",
        lambda url, **kw: ("Acme Corp home page", 200),
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["company_website"]), _entity())
    items = bundle.sources["company_website"]
    assert len(items) == 1
    assert items[0].source_type == "COMPANY_WEBSITE"
    assert items[0].quality_signal == "OFFICIAL"
    assert "COMPANY_WEBSITE_FETCH_FAILED" not in bundle.collection_flags


def test_company_website_strips_html_from_content(monkeypatch):
    """Test 10: fetch_url returns pre-stripped text; no HTML tags in content."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.fetch_url",
        lambda url, **kw: ("Acme Corp We build software.", 200),
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["company_website"]), _entity())
    content = bundle.sources["company_website"][0].content
    assert "<" not in content
    assert "Acme Corp" in content


def test_company_website_failure_adds_flag(monkeypatch):
    """Test 11: fetch_url returns empty → COMPANY_WEBSITE_FETCH_FAILED in flags."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.fetch_url",
        lambda url, **kw: ("", 0),
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["company_website"]), _entity())
    assert "COMPANY_WEBSITE_FETCH_FAILED" in bundle.collection_flags
    assert bundle.sources.get("company_website", []) == []


def test_company_website_skipped_when_no_domain(monkeypatch):
    """Test 12: Entity has no domain or website → no company_website items, no failure flag."""
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["company_website"]),
        _entity(domain=None, website=None),
    )
    assert bundle.sources.get("company_website", []) == []
    assert "COMPANY_WEBSITE_FETCH_FAILED" not in bundle.collection_flags


# ---------------------------------------------------------------------------
# Tests 13–15: stub collectors
# ---------------------------------------------------------------------------

def test_linkedin_stub_adds_flag():
    """Test 13: linkedin in source_list → LINKEDIN_STUBBED in flags, no items."""
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["linkedin"]), _entity())
    assert "LINKEDIN_STUBBED" in bundle.collection_flags
    assert bundle.sources.get("linkedin", []) == []


def test_registry_no_uk_jurisdiction_returns_no_record():
    """Test 14: Non-UK hq_country → NO_REGISTRY_RECORD, no API call."""
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(hq_country="US"),
    )
    assert "NO_REGISTRY_RECORD" in bundle.collection_flags
    assert "REGISTRY_STUBBED" not in bundle.collection_flags
    assert bundle.sources.get("registry", []) == []


def test_financial_no_sec_match_returns_no_public_data(monkeypatch):
    """Test 15: No EDGAR match → NO_PUBLIC_FINANCIAL_DATA in flags, no items."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.sec_search_by_name",
        lambda name, limit=5: [],
    )
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["financial"]), _entity())
    assert "NO_PUBLIC_FINANCIAL_DATA" in bundle.collection_flags
    assert bundle.sources.get("financial", []) == []


# ---------------------------------------------------------------------------
# Tests 16–19: classify_url_quality
# ---------------------------------------------------------------------------

def test_classify_url_quality_social():
    """Test 16: linkedin.com URL → SOCIAL."""
    assert classify_url_quality("https://www.linkedin.com/company/acme") == "SOCIAL"


def test_classify_url_quality_news():
    """Test 17: reuters.com URL → NEWS."""
    assert classify_url_quality("https://www.reuters.com/article/acme-corp") == "NEWS"


def test_classify_url_quality_directory():
    """Test 18: crunchbase.com URL → DIRECTORY."""
    assert classify_url_quality("https://www.crunchbase.com/organization/acme") == "DIRECTORY"


def test_classify_url_quality_official():
    """Test 19: unrecognised domain → OFFICIAL."""
    assert classify_url_quality("https://www.acme.com/about") == "OFFICIAL"


# ---------------------------------------------------------------------------
# Tests 20–22: _validate_against_entity + missing domain
# ---------------------------------------------------------------------------

def test_validate_confirmed_name_in_content():
    """Test 20: canonical_name appears in content → CONFIRMED."""
    ctx = _ctx(canonical_name="Acme Corp", domain="acme.com")
    result = _validate_against_entity("Acme Corp is a leading software vendor.", "https://other.com", ctx)
    assert result == "CONFIRMED"


def test_validate_likely_domain_in_url():
    """Test 21: domain substring in URL but name not in content → LIKELY."""
    ctx = _ctx(canonical_name="Acme Corp", domain="acme.com")
    result = _validate_against_entity("Generic software company profile.", "https://acme.com/about", ctx)
    assert result == "LIKELY"


def test_validate_uncertain_no_match():
    """Test 22: neither name in content nor domain in URL → UNCERTAIN."""
    ctx = _ctx(canonical_name="Acme Corp", domain="acme.com")
    result = _validate_against_entity("Widgets Inc. is a hardware company.", "https://widgets.com", ctx)
    assert result == "UNCERTAIN"


# ---------------------------------------------------------------------------
# Tests 23–26: Brave Search — new tests
# ---------------------------------------------------------------------------

def test_brave_search_missing_api_key_raises(monkeypatch):
    """Test 23: BRAVE_API_KEY not set → brave_search raises BraveSearchError."""
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(BraveSearchError, match="BRAVE_API_KEY not set"):
        brave_search("salesforce")


def test_brave_web_results_normalised_correctly(monkeypatch, tmp_path):
    """Test 24: Raw Brave JSON response → normalised list with url/content/title/published_date."""
    raw_response = json.dumps({
        "web": {
            "results": [
                {
                    "url": "https://salesforce.com/about",
                    "title": "About Salesforce",
                    "description": "Salesforce is a CRM platform.",
                    "page_age": "2024-06-01T00:00:00Z",
                }
            ]
        }
    })

    monkeypatch.setenv("BRAVE_API_KEY", "test-key-abc")
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "cobalt.core.search._http_get_json",
        lambda url, params, headers, timeout=10: raw_response,
    )

    results = brave_search("salesforce", result_type="web", count=5)

    assert len(results) == 1
    r = results[0]
    assert r["url"] == "https://salesforce.com/about"
    assert r["content"] == "Salesforce is a CRM platform."
    assert r["title"] == "About Salesforce"
    assert r["published_date"] == "2024-06-01T00:00:00Z"


def test_brave_news_uses_news_endpoint(monkeypatch, tmp_path):
    """Test 25: result_type='news' → URL contains 'news/search', not 'web/search'."""
    captured_urls: list[str] = []

    def _fake_http_get(url, params, headers, timeout=10):
        captured_urls.append(url)
        return json.dumps({"news": {"results": []}})

    monkeypatch.setenv("BRAVE_API_KEY", "test-key-abc")
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "cobalt.core.search._http_get_json",
        _fake_http_get,
    )

    brave_search("acme news", result_type="news", count=5)

    assert len(captured_urls) == 1
    assert "news/search" in captured_urls[0]
    assert "web/search" not in captured_urls[0]


def test_brave_http_error_becomes_collection_flag(monkeypatch):
    """Test 26: brave_search raises BraveSearchError → WEB_SEARCH_FETCH_ERROR in flags."""
    def _boom(q, *, result_type="web", count=10, freshness=None):
        raise BraveSearchError("Brave API HTTP 429: rate limited")

    monkeypatch.setattr("cobalt.tools.external_source_collector.brave_search", _boom)
    bundle = collect_sources(VENDOR_ID, PROGRAMME_ID, _readiness(["web_search"]), _entity())
    assert "WEB_SEARCH_FETCH_ERROR" in bundle.collection_flags
    assert bundle.sources.get("web_search", []) == []


# ---------------------------------------------------------------------------
# Tests 27–31: registry collector (Companies House)
# ---------------------------------------------------------------------------

_CH_SEARCH_RESULT = [
    {
        "title": "Anthology Limited",
        "company_number": "12345678",
        "company_status": "active",
        "company_type": "ltd",
        "date_of_creation": "2015-03-10",
        "address_snippet": "1 Tech Street, London",
        "links_self": "/company/12345678",
    }
]

_CH_FULL_RECORD = {
    "company_name": "Anthology Limited",
    "company_number": "12345678",
    "company_status": "active",
    "type": "ltd",
    "date_of_creation": "2015-03-10",
    "registered_office_address": {"address_line_1": "1 Tech Street", "locality": "London"},
    "sic_codes": ["62012"],
}


def test_registry_uk_match_returns_evidence_item(monkeypatch):
    """Test 27 A: UK vendor with match → one REGISTRY item, OFFICIAL quality."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_search",
        lambda name, limit=5: _CH_SEARCH_RESULT,
    )
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_get_company",
        lambda cn: _CH_FULL_RECORD,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Anthology Limited", hq_country="GB"),
    )
    items = bundle.sources.get("registry", [])
    assert len(items) == 1
    assert items[0].source_type == "REGISTRY"
    assert items[0].quality_signal == "OFFICIAL"
    assert items[0].validation_status in {"CONFIRMED", "LIKELY"}


def test_registry_uk_no_match_returns_no_record(monkeypatch):
    """Test 28 B: UK vendor but search returns no results → NO_REGISTRY_RECORD."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_search",
        lambda name, limit=5: [],
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(hq_country="GB"),
    )
    assert "NO_REGISTRY_RECORD" in bundle.collection_flags
    assert bundle.sources.get("registry", []) == []


def test_registry_multiple_close_matches_sets_flag(monkeypatch):
    """Test 29 C: Multiple close name matches → REGISTRY_MULTIPLE_MATCHES flag."""
    multi_results = [
        {"title": "Acme Corp Limited", "company_number": "11111111",
         "company_status": "active", "company_type": "ltd",
         "date_of_creation": "2010-01-01", "address_snippet": "", "links_self": ""},
        {"title": "Acme Corp Holdings Limited", "company_number": "22222222",
         "company_status": "active", "company_type": "ltd",
         "date_of_creation": "2012-01-01", "address_snippet": "", "links_self": ""},
        {"title": "Acme Corp Solutions Ltd", "company_number": "33333333",
         "company_status": "active", "company_type": "ltd",
         "date_of_creation": "2014-01-01", "address_snippet": "", "links_self": ""},
    ]
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_search",
        lambda name, limit=5: multi_results,
    )
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_get_company",
        lambda cn: {"company_name": "Acme Corp Limited", "company_number": cn,
                    "company_status": "active", "type": "ltd"},
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Acme Corp", hq_country="GB"),
    )
    assert "REGISTRY_MULTIPLE_MATCHES" in bundle.collection_flags
    assert len(bundle.sources.get("registry", [])) == 1


def test_registry_companies_house_error_returns_fetch_error(monkeypatch):
    """Test 30 D: companies_house_search raises → REGISTRY_FETCH_ERROR, no items."""
    from cobalt.core.exceptions import CompaniesHouseError

    def _boom(name, limit=5):
        raise CompaniesHouseError("rate limit hit (429)")

    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_search",
        _boom,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(hq_country="GB"),
    )
    assert "REGISTRY_FETCH_ERROR" in bundle.collection_flags
    assert bundle.sources.get("registry", []) == []


@pytest.mark.parametrize("hq_country", ["GB", "UK", "GBR"])
def test_registry_hq_country_uk_alias_works(hq_country, monkeypatch):
    """Test 31 E: hq_country values GB / UK / GBR all route to Companies House."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_search",
        lambda name, limit=5: _CH_SEARCH_RESULT,
    )
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_get_company",
        lambda cn: _CH_FULL_RECORD,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Anthology Limited", hq_country=hq_country),
    )
    assert len(bundle.sources.get("registry", [])) == 1
    assert bundle.sources["registry"][0].source_type == "REGISTRY"


# ---------------------------------------------------------------------------
# Tests 32–36: financial collector (SEC EDGAR)
# ---------------------------------------------------------------------------

_SEC_MATCH = [{"cik": "0000789019", "ticker": "MSFT", "title": "Microsoft Corp", "score": 0.9}]
_SEC_SUBMISSIONS = {
    "cik": "0000789019",
    "entityType": "operating",
    "sic": "7372",
    "sicDescription": "Prepackaged Software",
    "tickers": ["MSFT"],
    "exchanges": ["Nasdaq"],
    "stateOfIncorporation": "WA",
    "fiscalYearEnd": "0630",
    "category": "Large accelerated filer",
}


def test_financial_us_company_found_returns_evidence_item(monkeypatch):
    """Test 32 A: US company found in EDGAR → one FINANCIAL item with OFFICIAL quality."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.sec_search_by_name",
        lambda name, limit=5: _SEC_MATCH,
    )
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.sec_get_company_submissions",
        lambda cik: _SEC_SUBMISSIONS,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["financial"]),
        _entity(canonical_name="Microsoft Corp"),
    )
    items = bundle.sources.get("financial", [])
    assert len(items) == 1
    assert items[0].source_type == "FINANCIAL"
    assert items[0].quality_signal == "OFFICIAL"
    assert items[0].validation_status in {"CONFIRMED", "LIKELY"}
    content = json.loads(items[0].content)
    assert content["ticker"] == "MSFT"
    assert "NO_PUBLIC_FINANCIAL_DATA" not in bundle.collection_flags


def test_financial_no_match_returns_no_public_data(monkeypatch):
    """Test 33 B: Empty EDGAR results → NO_PUBLIC_FINANCIAL_DATA, no items."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.sec_search_by_name",
        lambda name, limit=5: [],
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["financial"]),
        _entity(canonical_name="Private Holdings LLC"),
    )
    assert "NO_PUBLIC_FINANCIAL_DATA" in bundle.collection_flags
    assert bundle.sources.get("financial", []) == []


def test_financial_non_us_hq_skips_edgar(monkeypatch):
    """Test 34 C: Non-US primary market hq_country → NO_PUBLIC_FINANCIAL_DATA, no API call."""
    calls: list[str] = []

    def _track(name, limit=5):
        calls.append(name)
        return []

    monkeypatch.setattr("cobalt.tools.external_source_collector.sec_search_by_name", _track)
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["financial"]),
        _entity(hq_country="CN"),
    )
    assert "NO_PUBLIC_FINANCIAL_DATA" in bundle.collection_flags
    assert bundle.sources.get("financial", []) == []
    assert calls == []


def test_financial_edgar_error_returns_fetch_error(monkeypatch):
    """Test 35 D: SecEdgarError from submissions fetch → FINANCIAL_FETCH_ERROR, no items."""
    from cobalt.core.exceptions import SecEdgarError as _SecEdgarError

    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.sec_search_by_name",
        lambda name, limit=5: _SEC_MATCH,
    )

    def _boom(cik):
        raise _SecEdgarError("HTTP 500: internal server error")

    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.sec_get_company_submissions",
        _boom,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["financial"]),
        _entity(canonical_name="Microsoft Corp"),
    )
    assert "FINANCIAL_FETCH_ERROR" in bundle.collection_flags
    assert bundle.sources.get("financial", []) == []


def test_financial_low_confidence_match_treated_as_no_data(monkeypatch):
    """Test 36 E: Best match score < 0.6 → NO_PUBLIC_FINANCIAL_DATA (private/ambiguous company)."""
    low_score_match = [{"cik": "0000789019", "ticker": "MSFT", "title": "Unrelated Corp", "score": 0.3}]
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.sec_search_by_name",
        lambda name, limit=5: low_score_match,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["financial"]),
        _entity(canonical_name="Private Company XYZ"),
    )
    assert "NO_PUBLIC_FINANCIAL_DATA" in bundle.collection_flags
    assert bundle.sources.get("financial", []) == []


# ---------------------------------------------------------------------------
# Tests 37–41: wikidata collector
# ---------------------------------------------------------------------------

_WD_APPLE_MATCH = [
    {
        "qid":          "Q312",
        "label":        "Apple Inc.",
        "description":  "American technology company",
        "inception":    "1976-04-01",
        "employee_count": 164000,
        "website":      "https://www.apple.com",
        "country":      "United States",
        "country_code": "US",
        "hq_city":      "Cupertino",
        "industries":   ["Consumer electronics"],
        "parents":      [],
        "legal_form":   "",
        "ticker":       "AAPL",
    }
]


def test_wikidata_match_returns_evidence_item(monkeypatch):
    """Test 37 A: Wikidata returns a matching entity → one WIKIDATA item, DIRECTORY quality."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.wikidata_lookup_by_name",
        lambda name, limit=5: _WD_APPLE_MATCH,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["wikidata"]),
        _entity(canonical_name="Apple Inc."),
    )
    items = bundle.sources.get("wikidata", [])
    assert len(items) == 1
    assert items[0].source_type == "WIKIDATA"
    assert items[0].quality_signal == "DIRECTORY"
    assert items[0].validation_status in {"CONFIRMED", "LIKELY"}
    assert "NO_WIKIDATA_RECORD" not in bundle.collection_flags


def test_wikidata_no_match_returns_no_record_flag(monkeypatch):
    """Test 38 B: Wikidata returns empty list → NO_WIKIDATA_RECORD flag, no items."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.wikidata_lookup_by_name",
        lambda name, limit=5: [],
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["wikidata"]),
        _entity(canonical_name="ObscurePrivateCo LLC"),
    )
    assert "NO_WIKIDATA_RECORD" in bundle.collection_flags
    assert bundle.sources.get("wikidata", []) == []


def test_wikidata_exception_returns_fetch_error(monkeypatch):
    """Test 39 C: wikidata_lookup_by_name raises → WIKIDATA_FETCH_ERROR flag, no items."""
    def _boom(name, limit=5):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.wikidata_lookup_by_name",
        _boom,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["wikidata"]),
        _entity(canonical_name="Acme Corp"),
    )
    assert "WIKIDATA_FETCH_ERROR" in bundle.collection_flags
    assert bundle.sources.get("wikidata", []) == []


def test_wikidata_multiple_close_matches_sets_flag(monkeypatch):
    """Test 40 D: Multiple close name matches → WIKIDATA_MULTIPLE_MATCHES flag."""
    multi = [
        {"qid": "Q100", "label": "Acme Corp",          "description": "technology company"},
        {"qid": "Q101", "label": "Acme Corp Limited",   "description": "UK company"},
        {"qid": "Q102", "label": "Acme Corp Solutions", "description": "software firm"},
    ]
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.wikidata_lookup_by_name",
        lambda name, limit=5: multi,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["wikidata"]),
        _entity(canonical_name="Acme Corp"),
    )
    assert "WIKIDATA_MULTIPLE_MATCHES" in bundle.collection_flags
    assert len(bundle.sources.get("wikidata", [])) == 1


def test_wikidata_source_url_contains_qid(monkeypatch):
    """Test 41 E: WIKIDATA item source_url contains the matched QID."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.wikidata_lookup_by_name",
        lambda name, limit=5: _WD_APPLE_MATCH,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["wikidata"]),
        _entity(canonical_name="Apple Inc."),
    )
    items = bundle.sources.get("wikidata", [])
    assert len(items) == 1
    assert "Q312" in items[0].source_url


# ---------------------------------------------------------------------------
# Tests 46–52: registry collector (OpenCorporates cascade)
# ---------------------------------------------------------------------------

_OC_DE_MATCH = {
    "name": "Anthology GmbH",
    "company_number": "HRB123456",
    "jurisdiction_code": "de",
    "incorporation_date": "2015-03-12",
    "dissolution_date": "",
    "company_type": "GmbH",
    "current_status": "Active",
    "inactive": False,
    "registry_url": "https://www.handelsregister.de/...",
    "opencorporates_url": "https://opencorporates.com/companies/de/HRB123456",
    "address": {
        "street_address": "Berliner Str. 1",
        "locality": "Berlin",
        "region": "",
        "postal_code": "10115",
        "country": "Germany",
    },
    "country": "Germany",
}

_OC_GB_MATCH = {
    "name": "Anthology Limited",
    "company_number": "99999999",
    "jurisdiction_code": "gb",
    "incorporation_date": "2015-01-01",
    "dissolution_date": "",
    "company_type": "Ltd",
    "current_status": "Active",
    "inactive": False,
    "registry_url": "",
    "opencorporates_url": "https://opencorporates.com/companies/gb/99999999",
    "address": {
        "street_address": "",
        "locality": "London",
        "region": "",
        "postal_code": "",
        "country": "United Kingdom",
    },
    "country": "United Kingdom",
}


def test_registry_non_uk_uses_opencorporates(monkeypatch):
    """Test 46: Non-UK vendor → OC consulted directly, CH never called."""
    ch_calls: list[str] = []
    oc_calls: list[str] = []

    def _ch_search(name, limit=5):
        ch_calls.append(name)
        return []

    def _oc_search(name, *, jurisdiction_code=None, limit=5, **kw):
        oc_calls.append(name)
        return [_OC_DE_MATCH]

    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_search",
        _ch_search,
    )
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.opencorporates_search",
        _oc_search,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Anthology GmbH", hq_country="DE"),
    )
    items = bundle.sources.get("registry", [])
    assert len(items) == 1
    assert items[0].source_type == "REGISTRY"
    assert ch_calls == []
    assert len(oc_calls) >= 1


def test_registry_uk_no_ch_match_falls_through_to_oc(monkeypatch):
    """Test 47: UK vendor, CH finds nothing → OC fallback returns item."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_search",
        lambda name, limit=5: [],
    )
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.opencorporates_search",
        lambda name, **kw: [_OC_GB_MATCH],
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Anthology Limited", hq_country="GB"),
    )
    items = bundle.sources.get("registry", [])
    assert len(items) == 1
    assert items[0].source_type == "REGISTRY"
    assert "opencorporates.com" in items[0].source_url


def test_registry_uk_ch_match_does_not_consult_oc(monkeypatch):
    """Test 48: UK vendor with CH match → OC never called (CH short-circuits)."""
    oc_calls: list[str] = []

    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_search",
        lambda name, limit=5: _CH_SEARCH_RESULT,
    )
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.companies_house_get_company",
        lambda cn: _CH_FULL_RECORD,
    )
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.opencorporates_search",
        lambda name, **kw: oc_calls.append(name) or [],
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Anthology Limited", hq_country="GB"),
    )
    assert len(bundle.sources.get("registry", [])) == 1
    assert oc_calls == []


def test_registry_oc_jurisdiction_filter_then_fallback(monkeypatch):
    """Test 49: OC with jurisdiction returns empty → retried without filter (2 OC calls)."""
    oc_calls: list = []

    def _oc_search(name, *, jurisdiction_code=None, limit=5, **kw):
        oc_calls.append(jurisdiction_code)
        if jurisdiction_code is not None:
            return []
        return [_OC_DE_MATCH]

    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.opencorporates_search",
        _oc_search,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Anthology GmbH", hq_country="DE"),
    )
    assert len(oc_calls) == 2
    assert len(bundle.sources.get("registry", [])) == 1


def test_registry_no_oc_token_non_uk_returns_no_record(monkeypatch):
    """Test 50: Non-UK vendor, no OC token → OC returns [] gracefully, NO_REGISTRY_RECORD."""
    monkeypatch.delenv("OPENCORPORATES_API_TOKEN", raising=False)
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Société Acme", hq_country="FR"),
    )
    assert "NO_REGISTRY_RECORD" in bundle.collection_flags
    assert bundle.sources.get("registry", []) == []


def test_registry_oc_error_returns_fetch_error(monkeypatch):
    """Test 51: OC raises exception → REGISTRY_FETCH_ERROR flag, no items."""
    from cobalt.core.exceptions import OpenCorporatesError

    def _boom(name, **kw):
        raise OpenCorporatesError("rate limit hit (429)")

    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.opencorporates_search",
        _boom,
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Acme España SL", hq_country="ES"),
    )
    assert "REGISTRY_FETCH_ERROR" in bundle.collection_flags


def test_registry_oc_country_corroboration_selects_de_match(monkeypatch):
    """Test 52: Two OC matches — one GB, one DE — hq_country=DE → DE match selected."""
    monkeypatch.setattr(
        "cobalt.tools.external_source_collector.opencorporates_search",
        lambda name, **kw: [_OC_GB_MATCH, _OC_DE_MATCH],
    )
    bundle = collect_sources(
        VENDOR_ID, PROGRAMME_ID,
        _readiness(["registry"]),
        _entity(canonical_name="Anthology GmbH", hq_country="DE"),
    )
    items = bundle.sources.get("registry", [])
    assert len(items) == 1
    content = json.loads(items[0].content)
    assert content["jurisdiction_code"] == "de"


# ---------------------------------------------------------------------------
# Tests 53–57: contract evidence collection
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_collect_contract_evidence_from_rs_extracted():
    """Test 53: rs_extracted item with DE fields → SourceEvidenceItem with CONTRACT/OFFICIAL."""
    contract_evidence = [
        {
            "source": "rs_extracted",
            "document_type": "MSA",
            "counterparty_legal_name": "Acme Holdings Ltd",
            "counterparty_jurisdiction": "GB",
            "contract_type": "MSA",
        }
    ]
    ctx = _ctx()
    items = _collect_contract_evidence(ctx, contract_evidence, _now_iso())
    assert len(items) == 1
    assert items[0].source_type == "CONTRACT"
    assert items[0].quality_signal == "OFFICIAL"
    assert items[0].validation_status == "CONFIRMED"
    assert "counterparty_legal_name: Acme Holdings Ltd" in items[0].content
    assert "counterparty_jurisdiction: GB" in items[0].content


def test_collect_contract_evidence_empty_when_no_contracts():
    """Test 54: Empty contract_evidence → returns []."""
    ctx = _ctx()
    items = _collect_contract_evidence(ctx, [], _now_iso())
    assert items == []


def test_collect_contract_evidence_skips_rs_fields():
    """Test 55: rs_extracted item with RS fields → RS fields absent from content."""
    contract_evidence = [
        {
            "source": "rs_extracted",
            "document_type": "MSA",
            "counterparty_legal_name": "Acme Ltd",
            "contract_value": "500000",       # RS field — must be excluded
            "renewal_date": "2027-01-01",     # RS field — must be excluded
            "auto_renewal": True,             # RS field — must be excluded
            "sla_terms": "99.9% uptime",      # RS field — must be excluded
            "notice_period": "30 days",       # RS field — must be excluded
        }
    ]
    ctx = _ctx()
    items = _collect_contract_evidence(ctx, contract_evidence, _now_iso())
    assert len(items) == 1
    content = items[0].content
    assert "counterparty_legal_name: Acme Ltd" in content
    assert "contract_value" not in content
    assert "renewal_date" not in content
    assert "auto_renewal" not in content
    assert "sla_terms" not in content
    assert "notice_period" not in content


def test_readiness_check_detects_contract_files(tmp_path):
    """Test 56: vendor workspace with a contract PDF in evidence/ → contract_evidence non-empty
    and CONTRACT_EVIDENCE_FOUND in flags."""
    from cobalt.tools.enrichment_readiness_check import check_enrichment_readiness

    # Build a minimal vendor workspace
    prog = "prog-test"
    vendor = "test-vendor"
    vendor_path = tmp_path / prog / vendor
    vendor_path.mkdir(parents=True)

    # Write a minimal vendor .md file so entity_data loads
    md_content = (
        "---\n"
        "vendor_id: test-vendor\n"
        "canonical_name: Test Vendor\n"
        "intake:\n"
        "  confidence: 0.85\n"
        "  data_class: CLASS_D\n"
        "---\n\n# Test Vendor\n"
    )
    (vendor_path / "test_vendor.md").write_text(md_content, encoding="utf-8")

    # Create evidence/ dir with a contract PDF
    evidence_dir = vendor_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "msa_acme_2025.pdf").write_bytes(b"%PDF fake")

    result = check_enrichment_readiness(
        vendor_id=vendor,
        programme_id=prog,
        workspace_root=tmp_path,
    )

    assert len(result.contract_evidence) > 0
    assert result.contract_evidence[0]["source"] == "uploaded_file"
    assert "CONTRACT_EVIDENCE_FOUND" in result.flags
    assert "contract" in result.source_list


def test_readiness_check_no_contracts_returns_empty(tmp_path):
    """Test 57: vendor workspace with no contract files → contract_evidence == []."""
    from cobalt.tools.enrichment_readiness_check import check_enrichment_readiness

    prog = "prog-test"
    vendor = "test-vendor-clean"
    vendor_path = tmp_path / prog / vendor
    vendor_path.mkdir(parents=True)

    md_content = (
        "---\n"
        "vendor_id: test-vendor-clean\n"
        "canonical_name: Clean Vendor\n"
        "intake:\n"
        "  confidence: 0.85\n"
        "  data_class: CLASS_D\n"
        "---\n\n# Clean Vendor\n"
    )
    (vendor_path / "clean_vendor.md").write_text(md_content, encoding="utf-8")

    result = check_enrichment_readiness(
        vendor_id=vendor,
        programme_id=prog,
        workspace_root=tmp_path,
    )

    assert result.contract_evidence == []
    assert "CONTRACT_EVIDENCE_FOUND" not in result.flags
    assert "contract" not in result.source_list
