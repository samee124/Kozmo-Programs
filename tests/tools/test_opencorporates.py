"""Tests for cobalt.core.opencorporates — OpenCorporates API wrapper."""

from __future__ import annotations

import json
import urllib.error

import pytest

from cobalt.core.opencorporates import opencorporates_search


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

_OC_COMPANY_1 = {
    "name": "ANTHOLOGY GMBH",
    "company_number": "HRB123456",
    "jurisdiction_code": "de",
    "incorporation_date": "2015-03-12",
    "dissolution_date": None,
    "company_type": "Gesellschaft mit beschränkter Haftung",
    "registry_url": "https://www.handelsregister.de/rp_web/...",
    "branch_status": None,
    "inactive": False,
    "current_status": "Active",
    "opencorporates_url": "https://opencorporates.com/companies/de/HRB123456",
    "registered_address_in_full": "Berliner Str. 1, 10115 Berlin",
    "registered_address": {
        "street_address": "Berliner Str. 1",
        "locality": "Berlin",
        "region": None,
        "postal_code": "10115",
        "country": "Germany",
    },
}

_OC_COMPANY_2 = {
    "name": "ANTHOLOGY LIMITED",
    "company_number": "12345678",
    "jurisdiction_code": "gb",
    "incorporation_date": "2010-05-01",
    "dissolution_date": None,
    "company_type": "Ltd",
    "registry_url": "",
    "branch_status": None,
    "inactive": False,
    "current_status": "Active",
    "opencorporates_url": "https://opencorporates.com/companies/gb/12345678",
    "registered_address": None,
}

_OC_SEARCH_RESPONSE = {
    "api_version": "0.4",
    "results": {
        "page": 1,
        "per_page": 5,
        "total_pages": 1,
        "total_count": 2,
        "companies": [
            {"company": _OC_COMPANY_1},
            {"company": _OC_COMPANY_2},
        ],
    },
}


@pytest.fixture
def mock_oc_api(monkeypatch, tmp_path):
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCORPORATES_API_TOKEN", "fake-token")
    responses: dict = {}
    calls: list[str] = []
    last_req: list = [None]

    class FakeResponse:
        def __init__(self, body):
            self._body = body.encode() if isinstance(body, str) else body
            self.headers: dict = {}

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        last_req[0] = req
        for pattern, body in responses.items():
            if pattern in url:
                if body == "__404__":
                    raise urllib.error.HTTPError(url, 404, "not found", {}, None)
                if body == "__401__":
                    raise urllib.error.HTTPError(url, 401, "unauthorized", {}, None)
                if body == "__429__":
                    raise urllib.error.HTTPError(url, 429, "rate limit", {}, None)
                payload = json.dumps(body) if not isinstance(body, str) else body
                return FakeResponse(payload)
        raise urllib.error.HTTPError(url, 404, "default not found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return {"responses": responses, "calls": calls, "last_req": last_req}


# ---------------------------------------------------------------------------
# Tests 1–10
# ---------------------------------------------------------------------------

def test_search_returns_parsed_companies(mock_oc_api):
    """Test 1: Valid OC response with 2 companies → 2 normalised dicts returned."""
    mock_oc_api["responses"]["companies/search"] = _OC_SEARCH_RESPONSE
    results = opencorporates_search("Anthology", limit=5)
    assert len(results) == 2
    assert results[0]["name"] == "ANTHOLOGY GMBH"
    assert results[0]["jurisdiction_code"] == "de"
    assert results[0]["country"] == "Germany"
    assert isinstance(results[0]["address"], dict)
    # Second company had no registered_address — address should be empty dict
    assert results[1]["name"] == "ANTHOLOGY LIMITED"
    assert results[1]["address"]["country"] == ""


def test_search_empty_token_returns_empty(monkeypatch, tmp_path):
    """Test 2: OPENCORPORATES_API_TOKEN not set → returns [] without HTTP call."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENCORPORATES_API_TOKEN", raising=False)
    http_calls: list[str] = []

    def _boom(req, timeout=None):
        http_calls.append(req.full_url)
        raise AssertionError("should not be called")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    results = opencorporates_search("Anthology")
    assert results == []
    assert http_calls == []


def test_search_404_returns_empty(mock_oc_api):
    """Test 3: OC returns 404 → returns [] without raising."""
    mock_oc_api["responses"]["companies/search"] = "__404__"
    results = opencorporates_search("Anthology")
    assert results == []


def test_search_401_returns_empty(mock_oc_api):
    """Test 4: OC returns 401 auth failure → OpenCorporatesError swallowed, returns []."""
    mock_oc_api["responses"]["companies/search"] = "__401__"
    results = opencorporates_search("Anthology")
    assert results == []


def test_search_429_returns_empty(mock_oc_api):
    """Test 5: OC returns 429 rate limit → OpenCorporatesError swallowed, returns []."""
    mock_oc_api["responses"]["companies/search"] = "__429__"
    results = opencorporates_search("Anthology")
    assert results == []


def test_search_jurisdiction_filter_in_url(mock_oc_api):
    """Test 6: jurisdiction_code passed → appears as query param in the request URL."""
    mock_oc_api["responses"]["companies/search"] = _OC_SEARCH_RESPONSE
    opencorporates_search("Anthology", jurisdiction_code="de")
    assert len(mock_oc_api["calls"]) == 1
    assert "jurisdiction_code=de" in mock_oc_api["calls"][0]


def test_search_api_token_in_url(mock_oc_api):
    """Test 7: api_token always appended to request URL."""
    mock_oc_api["responses"]["companies/search"] = _OC_SEARCH_RESPONSE
    opencorporates_search("Anthology")
    assert len(mock_oc_api["calls"]) == 1
    assert "api_token=fake-token" in mock_oc_api["calls"][0]


def test_normalisation_handles_missing_address(mock_oc_api):
    """Test 8: Company with no registered_address → empty address sub-dict, no crash."""
    resp = {
        "api_version": "0.4",
        "results": {
            "page": 1, "per_page": 5, "total_pages": 1, "total_count": 1,
            "companies": [
                {"company": {
                    "name": "BARE BONES LTD",
                    "company_number": "X999",
                    "jurisdiction_code": "gb",
                    "inactive": False,
                    "current_status": "Active",
                    # no registered_address key at all
                }}
            ],
        },
    }
    mock_oc_api["responses"]["companies/search"] = resp
    results = opencorporates_search("Bare Bones")
    assert len(results) == 1
    r = results[0]
    assert r["name"] == "BARE BONES LTD"
    assert r["address"] == {
        "street_address": "", "locality": "", "region": "",
        "postal_code": "", "country": "",
    }
    assert r["country"] == ""


def test_cache_layer_prevents_second_http_call(mock_oc_api):
    """Test 9: Two identical calls → only one HTTP request (second hits shelve cache)."""
    mock_oc_api["responses"]["companies/search"] = _OC_SEARCH_RESPONSE
    opencorporates_search("Anthology", limit=5)
    opencorporates_search("Anthology", limit=5)
    assert len(mock_oc_api["calls"]) == 1


def test_user_agent_header_set(mock_oc_api):
    """Test 10: User-Agent header is set on the outgoing request."""
    mock_oc_api["responses"]["companies/search"] = _OC_SEARCH_RESPONSE
    opencorporates_search("Anthology")
    req = mock_oc_api["last_req"][0]
    assert req is not None
    ua = req.get_header("User-agent")
    assert ua is not None
    assert "Cobalt" in ua
