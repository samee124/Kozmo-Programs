"""Tests for cobalt.core.wikidata — Wikidata SPARQL wrapper."""

from __future__ import annotations

import json
import urllib.error

import pytest


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_wd_api(monkeypatch, tmp_path):
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    responses: dict[str, str] = {}
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, body: str | bytes):
            self._body = body.encode("utf-8") if isinstance(body, str) else body
            self.headers: dict = {}

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=20):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for pattern, body in responses.items():
            if pattern in url:
                calls.append(url)
                if body == "__404__":
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                if body == "__429__":
                    raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
                return FakeResponse(body)
        calls.append(url)
        return FakeResponse(json.dumps({"results": {"bindings": []}}))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return {"responses": responses, "calls": calls}


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_SEARCH_RESP_3 = json.dumps({
    "search": [
        {"id": "Q312",    "label": "Apple Inc.",     "description": "American technology company"},
        {"id": "Q312345", "label": "Apple Corp",     "description": "UK music company"},
        {"id": "Q999",    "label": "Apple Computer", "description": "former name"},
    ]
})

_SPARQL_APPLE_RESP = json.dumps({
    "results": {"bindings": [
        {
            "item":          {"value": "http://www.wikidata.org/entity/Q312"},
            "itemLabel":     {"value": "Apple Inc."},
            "inception":     {"value": "1976-04-01T00:00:00Z"},
            "countryLabel":  {"value": "United States"},
            "countryCode":   {"value": "us"},
            "industryLabel": {"value": "Consumer electronics"},
        }
    ]}
})


# ---------------------------------------------------------------------------
# Tests 1–4: wikidata_search_entities
# ---------------------------------------------------------------------------

def test_search_entities_returns_matching_hits(mock_wd_api):
    """Test 1: Search API returns 3 results → list of 3 dicts with qid/label/description."""
    from cobalt.core.wikidata import wikidata_search_entities
    mock_wd_api["responses"]["wikidata.org/w/api.php"] = _SEARCH_RESP_3
    results = wikidata_search_entities("Apple", limit=3)
    assert len(results) == 3
    assert results[0]["qid"] == "Q312"
    assert results[0]["label"] == "Apple Inc."
    assert results[0]["description"] == "American technology company"


def test_search_entities_caches_result(mock_wd_api):
    """Test 2: Second call with same name + limit returns cached result — no second HTTP call."""
    from cobalt.core.wikidata import wikidata_search_entities
    mock_wd_api["responses"]["wikidata.org/w/api.php"] = _SEARCH_RESP_3
    wikidata_search_entities("Apple", limit=3)
    wikidata_search_entities("Apple", limit=3)
    assert len(mock_wd_api["calls"]) == 1


def test_search_entities_empty_name_returns_empty(mock_wd_api):
    """Test 3: Empty name → returns [] without making any HTTP call."""
    from cobalt.core.wikidata import wikidata_search_entities
    results = wikidata_search_entities("")
    assert results == []
    assert mock_wd_api["calls"] == []


def test_search_entities_rate_limit_swallowed(mock_wd_api):
    """Test 4: 429 rate limit from search API → WikidataError swallowed, returns []."""
    from cobalt.core.wikidata import wikidata_search_entities
    mock_wd_api["responses"]["wikidata.org/w/api.php"] = "__429__"
    results = wikidata_search_entities("Apple")
    assert results == []


# ---------------------------------------------------------------------------
# Tests 5–9: wikidata_get_company_facts
# ---------------------------------------------------------------------------

def test_get_company_facts_calls_sparql_with_qid(mock_wd_api):
    """Test 5: SPARQL endpoint called and Q-ID appears in the encoded query URL."""
    from cobalt.core.wikidata import wikidata_get_company_facts
    mock_wd_api["responses"]["query.wikidata.org/sparql"] = _SPARQL_APPLE_RESP
    wikidata_get_company_facts(["Q312"])
    sparql_calls = [c for c in mock_wd_api["calls"] if "sparql" in c]
    assert len(sparql_calls) == 1
    assert "Q312" in sparql_calls[0]


def test_get_company_facts_coalesces_multiple_industries(mock_wd_api):
    """Test 6: Two rows for same QID with different industryLabel → industries list has both."""
    from cobalt.core.wikidata import wikidata_get_company_facts
    resp = json.dumps({
        "results": {"bindings": [
            {
                "item":          {"value": "http://www.wikidata.org/entity/Q312"},
                "itemLabel":     {"value": "Apple Inc."},
                "industryLabel": {"value": "Consumer electronics"},
            },
            {
                "item":          {"value": "http://www.wikidata.org/entity/Q312"},
                "itemLabel":     {"value": "Apple Inc."},
                "industryLabel": {"value": "Software"},
            },
        ]}
    })
    mock_wd_api["responses"]["query.wikidata.org/sparql"] = resp
    facts = wikidata_get_company_facts(["Q312"])
    assert "Q312" in facts
    assert set(facts["Q312"]["industries"]) == {"Consumer electronics", "Software"}


def test_get_company_facts_drops_empty_records(mock_wd_api):
    """Test 7: QID with no label, no inception, no industries → dropped from output dict."""
    from cobalt.core.wikidata import wikidata_get_company_facts
    resp = json.dumps({
        "results": {"bindings": [
            {
                "item":      {"value": "http://www.wikidata.org/entity/Q999"},
                "itemLabel": {"value": ""},
            }
        ]}
    })
    mock_wd_api["responses"]["query.wikidata.org/sparql"] = resp
    facts = wikidata_get_company_facts(["Q999"])
    assert "Q999" not in facts


def test_get_company_facts_rate_limit_returns_empty(mock_wd_api):
    """Test 8: 429 from SPARQL endpoint → WikidataError swallowed, returns {}."""
    from cobalt.core.wikidata import wikidata_get_company_facts
    mock_wd_api["responses"]["query.wikidata.org/sparql"] = "__429__"
    facts = wikidata_get_company_facts(["Q312"])
    assert facts == {}


def test_get_company_facts_caches_result(mock_wd_api):
    """Test 9: Second call with same QIDs returns cached result — one SPARQL HTTP call only."""
    from cobalt.core.wikidata import wikidata_get_company_facts
    mock_wd_api["responses"]["query.wikidata.org/sparql"] = _SPARQL_APPLE_RESP
    wikidata_get_company_facts(["Q312"])
    wikidata_get_company_facts(["Q312"])
    sparql_calls = [c for c in mock_wd_api["calls"] if "sparql" in c]
    assert len(sparql_calls) == 1


# ---------------------------------------------------------------------------
# Tests 10–12: wikidata_lookup_by_name
# ---------------------------------------------------------------------------

def test_lookup_by_name_returns_facts_augmented_with_description(mock_wd_api):
    """Test 10: search + SPARQL both succeed → facts dict augmented with description from search."""
    from cobalt.core.wikidata import wikidata_lookup_by_name
    mock_wd_api["responses"]["wikidata.org/w/api.php"] = json.dumps({
        "search": [{"id": "Q312", "label": "Apple Inc.", "description": "American technology company"}]
    })
    mock_wd_api["responses"]["query.wikidata.org/sparql"] = _SPARQL_APPLE_RESP
    results = wikidata_lookup_by_name("Apple Inc.", limit=1)
    assert len(results) == 1
    assert results[0]["qid"] == "Q312"
    assert results[0]["description"] == "American technology company"
    assert results[0]["label"] == "Apple Inc."


def test_lookup_by_name_empty_search_skips_sparql(mock_wd_api):
    """Test 11: Search returns no results → returns [] without calling SPARQL endpoint."""
    from cobalt.core.wikidata import wikidata_lookup_by_name
    mock_wd_api["responses"]["wikidata.org/w/api.php"] = json.dumps({"search": []})
    results = wikidata_lookup_by_name("NoSuchCompanyXYZ999", limit=3)
    assert results == []
    sparql_calls = [c for c in mock_wd_api["calls"] if "sparql" in c]
    assert sparql_calls == []


def test_lookup_by_name_sparql_empty_still_returns_search_metadata(mock_wd_api):
    """Test 12: SPARQL returns no matching facts → entity returned with label/description from search."""
    from cobalt.core.wikidata import wikidata_lookup_by_name
    mock_wd_api["responses"]["wikidata.org/w/api.php"] = json.dumps({
        "search": [{"id": "Q312", "label": "Apple Inc.", "description": "tech co"}]
    })
    mock_wd_api["responses"]["query.wikidata.org/sparql"] = json.dumps({
        "results": {"bindings": []}
    })
    results = wikidata_lookup_by_name("Apple Inc.", limit=1)
    assert len(results) == 1
    assert results[0]["qid"] == "Q312"
    assert results[0]["label"] == "Apple Inc."
    assert results[0]["description"] == "tech co"


# ---------------------------------------------------------------------------
# Test 13: inception date formatting
# ---------------------------------------------------------------------------

def test_inception_date_strips_time_portion(mock_wd_api):
    """Test 13: inception '1976-04-01T00:00:00Z' in SPARQL → record['inception'] = '1976-04-01'."""
    from cobalt.core.wikidata import wikidata_get_company_facts
    mock_wd_api["responses"]["query.wikidata.org/sparql"] = _SPARQL_APPLE_RESP
    facts = wikidata_get_company_facts(["Q312"])
    assert facts["Q312"]["inception"] == "1976-04-01"
