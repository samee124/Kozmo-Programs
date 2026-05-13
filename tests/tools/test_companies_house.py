"""Tests for cobalt.core.companies_house."""

from __future__ import annotations

import base64
import json
import urllib.error

import pytest

import cobalt.core.companies_house as ch_module
from cobalt.core.companies_house import (
    companies_house_get_company,
    companies_house_search,
)
from cobalt.core.exceptions import CompaniesHouseError

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

_SEARCH_RESPONSE = json.dumps({
    "items": [
        {
            "title": "Anthology Limited",
            "company_number": "12345678",
            "company_status": "active",
            "company_type": "ltd",
            "date_of_creation": "2015-03-10",
            "address_snippet": "1 Tech Street, London, EC1A 1BB",
            "links": {"self": "/company/12345678"},
        }
    ],
    "total_results": 1,
})

_COMPANY_RESPONSE = json.dumps({
    "company_name": "Anthology Limited",
    "company_number": "12345678",
    "company_status": "active",
    "type": "ltd",
    "date_of_creation": "2015-03-10",
    "registered_office_address": {
        "address_line_1": "1 Tech Street",
        "locality": "London",
        "postal_code": "EC1A 1BB",
        "country": "England",
    },
    "sic_codes": ["62012"],
    "has_been_liquidated": False,
    "has_charges": False,
    "jurisdiction": "england-wales",
})


@pytest.fixture
def mock_ch_api(monkeypatch):
    """Fixture that intercepts urllib.request.urlopen with a pattern-match dict."""
    responses: dict[str, tuple[str, int]] = {}
    call_count = [0]

    class FakeResponse:
        def __init__(self, body: str, status: int = 200):
            self._body = body.encode()
            self.status = status

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=None):
        call_count[0] += 1
        url = req.full_url
        for pattern, (body, status) in responses.items():
            if pattern in url:
                if status >= 400:
                    raise urllib.error.HTTPError(url, status, "error", {}, None)
                return FakeResponse(body, status)
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "fake-key")

    class Ctx:
        def __init__(self):
            self.responses = responses
            self.call_count = call_count

        @property
        def calls(self) -> int:
            return call_count[0]

    return Ctx()


# ---------------------------------------------------------------------------
# Test 1: search returns parsed results
# ---------------------------------------------------------------------------

def test_companies_house_search_returns_parsed_results(mock_ch_api):
    """Search returns normalised list of dicts with expected keys."""
    mock_ch_api.responses["/search/companies"] = (_SEARCH_RESPONSE, 200)

    results = companies_house_search("anthology", limit=5)

    assert len(results) == 1
    r = results[0]
    assert r["title"] == "Anthology Limited"
    assert r["company_number"] == "12345678"
    assert r["company_status"] == "active"
    assert r["company_type"] == "ltd"
    assert r["date_of_creation"] == "2015-03-10"
    assert "address_snippet" in r
    assert "links_self" in r


# ---------------------------------------------------------------------------
# Test 2: no API key → returns []
# ---------------------------------------------------------------------------

def test_companies_house_search_no_api_key_returns_empty(monkeypatch):
    """Missing COMPANIES_HOUSE_API_KEY → [] with no exception."""
    monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
    result = companies_house_search("anthology")
    assert result == []


# ---------------------------------------------------------------------------
# Test 3: 404 response → returns []
# ---------------------------------------------------------------------------

def test_companies_house_search_404_returns_empty(mock_ch_api, tmp_path, monkeypatch):
    """When the API path returns 404, search returns []."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    # No matching pattern in responses → fake_urlopen raises 404

    result = companies_house_search("nonexistent corp xyz", limit=3)
    assert result == []


# ---------------------------------------------------------------------------
# Test 4: 401 raises CompaniesHouseError
# ---------------------------------------------------------------------------

def test_companies_house_search_401_raises(mock_ch_api, tmp_path, monkeypatch):
    """Auth failure (401) propagates as CompaniesHouseError from _get_json,
    which companies_house_search catches and returns []."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    mock_ch_api.responses["/search/companies"] = ("", 401)

    # companies_house_search catches CompaniesHouseError and returns []
    result = companies_house_search("anthology")
    assert result == []


# ---------------------------------------------------------------------------
# Test 5: get_company parses full record
# ---------------------------------------------------------------------------

def test_companies_house_get_company_parses_full_record(mock_ch_api, tmp_path, monkeypatch):
    """Full company record is returned with expected fields."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    mock_ch_api.responses["/company/12345678"] = (_COMPANY_RESPONSE, 200)

    result = companies_house_get_company("12345678")

    assert result is not None
    assert result["company_name"] == "Anthology Limited"
    assert result["company_number"] == "12345678"
    assert "registered_office_address" in result
    assert "sic_codes" in result
    assert result["sic_codes"] == ["62012"]


# ---------------------------------------------------------------------------
# Test 6: get_company returns None on 404
# ---------------------------------------------------------------------------

def test_companies_house_get_company_404_returns_none(mock_ch_api, tmp_path, monkeypatch):
    """404 from the company endpoint → None (not an exception)."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    # No matching pattern → fake_urlopen raises 404

    result = companies_house_get_company("99999999")
    assert result is None


# ---------------------------------------------------------------------------
# Test 7: auth header is Basic with key as username, empty password
# ---------------------------------------------------------------------------

def test_auth_header_is_basic_with_key_and_empty_password(monkeypatch, tmp_path):
    """Auth header encodes 'fake-key:' as Base64 Basic auth."""
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "fake-key")
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))

    captured: list = []

    class FakeResp:
        status = 200
        def read(self): return json.dumps({"items": []}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    companies_house_search("anthology")

    assert len(captured) == 1
    auth = captured[0].get_header("Authorization")
    assert auth is not None
    assert auth.startswith("Basic ")
    token = auth[len("Basic "):]
    decoded = base64.b64decode(token).decode()
    assert decoded == "fake-key:"


# ---------------------------------------------------------------------------
# Test 8: cache layer — second call does not hit urlopen
# ---------------------------------------------------------------------------

def test_cache_prevents_duplicate_api_calls(mock_ch_api, tmp_path, monkeypatch):
    """Second search with same query hits cache; urlopen call count stays at 1."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    mock_ch_api.responses["/search/companies"] = (_SEARCH_RESPONSE, 200)

    companies_house_search("anthology", limit=5)
    companies_house_search("anthology", limit=5)

    assert mock_ch_api.calls == 1
