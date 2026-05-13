"""Tests for cobalt.core.gleif — GLEIF LEI registry wrapper."""

from __future__ import annotations

import json
import urllib.error

import pytest

from cobalt.core.exceptions import GleifError
from cobalt.core.gleif import (
    gleif_get_direct_parent,
    gleif_get_ultimate_parent,
    gleif_search_by_name,
)

# ---------------------------------------------------------------------------
# Shared response data
# ---------------------------------------------------------------------------

_APPLE_LEI = "5493001KJTIIGC8Y1R12"
_ALPHABET_LEI = "54930016113PD33V1H31"

_SEARCH_RESPONSE = {
    "data": [
        {
            "type": "lei-records",
            "id": _APPLE_LEI,
            "attributes": {
                "lei": _APPLE_LEI,
                "entity": {
                    "legalName": {"name": "Apple Inc.", "language": "en"},
                    "otherNames": [{"name": "Apple Computer Inc.", "language": "en"}],
                    "legalAddress": {"country": "US"},
                    "jurisdiction": "US-CA",
                    "status": "ACTIVE",
                    "registeredAs": "C0806592",
                    "creationDate": "1977-01-03T00:00:00Z",
                    "legalForm": {"id": "JHN5"},
                },
            },
            "relationships": {
                "direct-parent": {
                    "links": {
                        "related": f"https://api.gleif.org/api/v1/lei-records/{_APPLE_LEI}/direct-parent"
                    }
                },
                "ultimate-parent": {
                    "links": {
                        "related": f"https://api.gleif.org/api/v1/lei-records/{_APPLE_LEI}/ultimate-parent"
                    }
                },
            },
        }
    ],
    "meta": {"pagination": {"total": 1}},
}

_ALPHABET_RECORD = {
    "type": "lei-records",
    "id": _ALPHABET_LEI,
    "attributes": {
        "lei": _ALPHABET_LEI,
        "entity": {
            "legalName": {"name": "Alphabet Inc.", "language": "en"},
            "otherNames": [],
            "legalAddress": {"country": "US"},
            "jurisdiction": "US-DE",
            "status": "ACTIVE",
            "registeredAs": "7288699",
            "creationDate": "2015-10-02T00:00:00Z",
            "legalForm": {"id": "8888"},
        },
    },
    "relationships": {
        "direct-parent": {"links": {}},
        "ultimate-parent": {"links": {}},
    },
}

_PARENT_RESPONSE = {"data": _ALPHABET_RECORD}

_GOOGLE_LEI = "549300S4BKPGSMKDAS28"
_GOOGLE_RECORD = {
    "type": "lei-records",
    "id": _GOOGLE_LEI,
    "attributes": {
        "lei": _GOOGLE_LEI,
        "entity": {
            "legalName": {"name": "Google LLC", "language": "en"},
            "otherNames": [],
            "legalAddress": {"country": "US"},
            "jurisdiction": "US-DE",
            "status": "ACTIVE",
            "registeredAs": "201727810",
            "creationDate": "1998-09-04T00:00:00Z",
            "legalForm": {"id": "8888"},
        },
    },
    "relationships": {
        "direct-parent": {
            "links": {
                "related": f"https://api.gleif.org/api/v1/lei-records/{_GOOGLE_LEI}/direct-parent"
            }
        },
        "ultimate-parent": {
            "links": {
                "related": f"https://api.gleif.org/api/v1/lei-records/{_GOOGLE_LEI}/ultimate-parent"
            }
        },
    },
}


# ---------------------------------------------------------------------------
# Mock fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_gleif_api(monkeypatch, tmp_path):
    """Intercept urllib.request.urlopen for GLEIF endpoints."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    responses: dict[str, object] = {}
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, body: str):
            self._body = body.encode() if isinstance(body, str) else body
            self.headers = {}

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        for pattern, body in responses.items():
            if pattern in url:
                if body == "__404__":
                    raise urllib.error.HTTPError(url, 404, "not found", {}, None)
                if body == "__429__":
                    raise urllib.error.HTTPError(url, 429, "rate limit", {}, None)
                payload = json.dumps(body) if not isinstance(body, str) else body
                return FakeResponse(payload)
        raise urllib.error.HTTPError(url, 404, "default not found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return {"responses": responses, "calls": calls}


# ---------------------------------------------------------------------------
# Test 1: search returns parsed records
# ---------------------------------------------------------------------------

def test_search_returns_parsed_records(mock_gleif_api):
    """Mock /lei-records search with one match → normalised list with expected fields."""
    mock_gleif_api["responses"]["lei-records"] = _SEARCH_RESPONSE

    results = gleif_search_by_name("Apple Inc.", limit=5)

    assert len(results) == 1
    r = results[0]
    assert r["lei"] == _APPLE_LEI
    assert r["legal_name"] == "Apple Inc."
    assert r["country"] == "US"
    assert r["jurisdiction"] == "US-CA"
    assert r["status"] == "ACTIVE"
    assert "Apple Computer Inc." in r["other_names"]
    assert r["has_direct_parent_link"] is True


# ---------------------------------------------------------------------------
# Test 2: empty data list returns empty list
# ---------------------------------------------------------------------------

def test_search_empty_results_returns_empty_list(mock_gleif_api):
    """Mock returns {"data": []} → returned list is []."""
    mock_gleif_api["responses"]["lei-records"] = {"data": [], "meta": {"pagination": {"total": 0}}}

    results = gleif_search_by_name("Nonexistent Corp")

    assert results == []


# ---------------------------------------------------------------------------
# Test 3: 404 returns empty list (graceful)
# ---------------------------------------------------------------------------

def test_search_404_returns_empty_list(mock_gleif_api):
    """404 from GLEIF search → returns [] without raising."""
    mock_gleif_api["responses"]["lei-records"] = "__404__"

    results = gleif_search_by_name("Apple Inc.")

    assert results == []


# ---------------------------------------------------------------------------
# Test 4: 429 returns empty list (graceful — search catches GleifError)
# ---------------------------------------------------------------------------

def test_search_429_returns_empty_list(mock_gleif_api):
    """429 rate limit from search endpoint → returns [] without raising."""
    mock_gleif_api["responses"]["lei-records"] = "__429__"

    results = gleif_search_by_name("Apple Inc.")

    assert results == []


# ---------------------------------------------------------------------------
# Test 5: get_direct_parent returns parent record
# ---------------------------------------------------------------------------

def test_get_direct_parent_returns_parent_record(mock_gleif_api):
    """Mock direct-parent endpoint with valid response → normalised parent dict."""
    mock_gleif_api["responses"]["direct-parent"] = _PARENT_RESPONSE

    result = gleif_get_direct_parent(_APPLE_LEI)

    assert result is not None
    assert result["legal_name"] == "Alphabet Inc."
    assert result["lei"] == _ALPHABET_LEI
    assert result["country"] == "US"
    assert result["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Test 6: get_direct_parent 404 returns None
# ---------------------------------------------------------------------------

def test_get_direct_parent_404_returns_none(mock_gleif_api):
    """404 from direct-parent endpoint → None (the 'no parent' signal, not an error)."""
    mock_gleif_api["responses"]["direct-parent"] = "__404__"

    result = gleif_get_direct_parent(_APPLE_LEI)

    assert result is None


# ---------------------------------------------------------------------------
# Test 7: get_direct_parent 429 raises GleifError
# ---------------------------------------------------------------------------

def test_get_direct_parent_429_raises(mock_gleif_api):
    """429 from parent endpoint → GleifError propagated (not swallowed)."""
    mock_gleif_api["responses"]["direct-parent"] = "__429__"

    with pytest.raises(GleifError, match="rate limit"):
        gleif_get_direct_parent(_APPLE_LEI)


# ---------------------------------------------------------------------------
# Test 8: ultimate parent independent from direct parent
# ---------------------------------------------------------------------------

def test_get_ultimate_parent_independent_of_direct(mock_gleif_api):
    """direct-parent and ultimate-parent can return different records."""
    direct_response = {"data": _ALPHABET_RECORD}
    ultimate_record = dict(_ALPHABET_RECORD)
    ultimate_record["id"] = "ULTIMATE00000000000001"
    ultimate_record["attributes"] = dict(_ALPHABET_RECORD["attributes"])
    ultimate_record["attributes"]["lei"] = "ULTIMATE00000000000001"
    ultimate_response = {"data": ultimate_record}

    mock_gleif_api["responses"]["direct-parent"] = direct_response
    mock_gleif_api["responses"]["ultimate-parent"] = ultimate_response

    direct = gleif_get_direct_parent(_GOOGLE_LEI)
    ultimate = gleif_get_ultimate_parent(_GOOGLE_LEI)

    assert direct is not None
    assert ultimate is not None
    assert direct["lei"] != ultimate["lei"]


# ---------------------------------------------------------------------------
# Test 9: search cache layer prevents duplicate urlopen calls
# ---------------------------------------------------------------------------

def test_search_cache_layer(mock_gleif_api):
    """Second identical search hits cache; urlopen call count stays at 1."""
    mock_gleif_api["responses"]["lei-records"] = _SEARCH_RESPONSE

    gleif_search_by_name("Apple Inc.", limit=5)
    gleif_search_by_name("Apple Inc.", limit=5)

    assert len(mock_gleif_api["calls"]) == 1


# ---------------------------------------------------------------------------
# Test 10: parent 404 cached as sentinel — second call doesn't hit urlopen
# ---------------------------------------------------------------------------

def test_parent_404_cached_as_no_parent_sentinel(mock_gleif_api):
    """First direct-parent call returns 404 (None). Second call reads sentinel from cache."""
    mock_gleif_api["responses"]["direct-parent"] = "__404__"

    first = gleif_get_direct_parent(_APPLE_LEI)
    second = gleif_get_direct_parent(_APPLE_LEI)

    assert first is None
    assert second is None
    # Sentinel cached after first call; second call should not hit urlopen
    assert len(mock_gleif_api["calls"]) == 1


# ---------------------------------------------------------------------------
# Test 11: User-Agent header set in request
# ---------------------------------------------------------------------------

def test_user_agent_header_set(monkeypatch, tmp_path):
    """Request includes User-Agent header containing 'Cobalt' (or custom GLEIF_USER_AGENT)."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("GLEIF_USER_AGENT", raising=False)

    captured: list = []

    class FakeResp:
        headers = {}

        def read(self):
            return json.dumps({"data": [], "meta": {}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    gleif_search_by_name("TestCorp")

    assert len(captured) == 1
    ua = captured[0].get_header("User-agent")
    assert ua is not None
    assert "Cobalt" in ua


# ---------------------------------------------------------------------------
# Test 12: normalise handles missing / partial attributes without crashing
# ---------------------------------------------------------------------------

def test_normalise_handles_missing_fields(mock_gleif_api):
    """LEI record with minimal attributes → empty strings for missing fields, no crash."""
    sparse_response = {
        "data": [
            {
                "type": "lei-records",
                "id": "SPARSE00000000000001",
                "attributes": {
                    "lei": "SPARSE00000000000001",
                    "entity": {},
                },
                "relationships": {},
            }
        ],
        "meta": {"pagination": {"total": 1}},
    }
    mock_gleif_api["responses"]["lei-records"] = sparse_response

    results = gleif_search_by_name("Sparse Corp")

    assert len(results) == 1
    r = results[0]
    assert r["lei"] == "SPARSE00000000000001"
    assert r["legal_name"] == ""
    assert r["country"] == ""
    assert r["has_direct_parent_link"] is False
    assert r["has_ultimate_parent_link"] is False
