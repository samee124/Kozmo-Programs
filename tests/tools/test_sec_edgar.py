"""Tests for cobalt.core.sec_edgar."""

from __future__ import annotations

import json
import urllib.error

import pytest

import cobalt.core.sec_edgar as edgar_module
from cobalt.core.sec_edgar import (
    sec_get_company_submissions,
    sec_search_by_name,
    sec_search_by_ticker,
)

# ---------------------------------------------------------------------------
# Shared fixtures and data
# ---------------------------------------------------------------------------

_TICKERS_DATA = {
    "0": {"cik_str": 789019,  "ticker": "MSFT", "title": "Microsoft Corp"},
    "1": {"cik_str": 320193,  "ticker": "AAPL", "title": "Apple Inc"},
    "2": {"cik_str": 1018724, "ticker": "AMZN", "title": "Amazon.com Inc"},
}

_SUBMISSIONS_RESPONSE = {
    "cik": "0000789019",
    "entityType": "operating",
    "sic": "7372",
    "sicDescription": "Prepackaged Software",
    "name": "Microsoft Corp",
    "tickers": ["MSFT"],
    "exchanges": ["Nasdaq"],
    "stateOfIncorporation": "WA",
    "fiscalYearEnd": "0630",
    "category": "Large accelerated filer",
}


@pytest.fixture(autouse=True)
def reset_sec_caches():
    """Reset module-level in-memory ticker cache between tests."""
    edgar_module._tickers_cache = None
    edgar_module._tickers_cache_time = 0.0
    yield
    edgar_module._tickers_cache = None
    edgar_module._tickers_cache_time = 0.0


@pytest.fixture
def mock_edgar_api(monkeypatch, tmp_path):
    """Intercept urllib.request.urlopen for EDGAR endpoints."""
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
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))

    class Ctx:
        def __init__(self):
            self.responses = responses
            self.call_count = call_count

        @property
        def calls(self) -> int:
            return call_count[0]

    return Ctx()


# ---------------------------------------------------------------------------
# Test 1: exact name match
# ---------------------------------------------------------------------------

def test_search_by_name_exact_match_returns_result(mock_edgar_api):
    """Exact name after suffix stripping → score 1.0, correct ticker returned."""
    mock_edgar_api.responses["company_tickers"] = (json.dumps(_TICKERS_DATA), 200)

    results = sec_search_by_name("Microsoft Corp", limit=5)

    assert len(results) >= 1
    msft = next(r for r in results if r["ticker"] == "MSFT")
    assert msft["score"] == 1.0
    assert msft["cik"] == "0000789019"


# ---------------------------------------------------------------------------
# Test 2: partial name match above threshold
# ---------------------------------------------------------------------------

def test_search_by_name_partial_match_above_threshold(mock_edgar_api):
    """Partial name overlap above 0.4 threshold → included in results."""
    mock_edgar_api.responses["company_tickers"] = (json.dumps(_TICKERS_DATA), 200)

    # "Amazon" partially matches "Amazon.com Inc" (substring match → score 0.7)
    results = sec_search_by_name("Amazon", limit=5)

    assert any(r["ticker"] == "AMZN" for r in results)
    amzn = next(r for r in results if r["ticker"] == "AMZN")
    assert amzn["score"] >= 0.4


# ---------------------------------------------------------------------------
# Test 3: name below threshold excluded
# ---------------------------------------------------------------------------

def test_search_by_name_below_threshold_excluded(mock_edgar_api):
    """Name with no token overlap against any ticker → empty results."""
    mock_edgar_api.responses["company_tickers"] = (json.dumps(_TICKERS_DATA), 200)

    results = sec_search_by_name("Quantum Robotics Unlimited", limit=5)

    assert results == []


# ---------------------------------------------------------------------------
# Test 4: API failure returns empty (graceful no-op)
# ---------------------------------------------------------------------------

def test_search_by_name_returns_empty_on_api_failure(monkeypatch, tmp_path):
    """Transport error loading tickers → sec_search_by_name returns [] without raising."""
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))

    def _fail(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _fail)

    results = sec_search_by_name("Microsoft Corp")
    assert results == []


# ---------------------------------------------------------------------------
# Test 5: ticker lookup — found
# ---------------------------------------------------------------------------

def test_search_by_ticker_found(mock_edgar_api):
    """Exact ticker match returns dict with cik/ticker/title."""
    mock_edgar_api.responses["company_tickers"] = (json.dumps(_TICKERS_DATA), 200)

    result = sec_search_by_ticker("MSFT")

    assert result is not None
    assert result["ticker"] == "MSFT"
    assert result["title"] == "Microsoft Corp"
    assert result["cik"] == "0000789019"


# ---------------------------------------------------------------------------
# Test 6: ticker lookup — case insensitive
# ---------------------------------------------------------------------------

def test_search_by_ticker_case_insensitive(mock_edgar_api):
    """Lowercase ticker still matches uppercase entry in tickers list."""
    mock_edgar_api.responses["company_tickers"] = (json.dumps(_TICKERS_DATA), 200)

    result = sec_search_by_ticker("msft")

    assert result is not None
    assert result["ticker"] == "MSFT"


# ---------------------------------------------------------------------------
# Test 7: ticker lookup — not found
# ---------------------------------------------------------------------------

def test_search_by_ticker_not_found_returns_none(mock_edgar_api):
    """Ticker not present in tickers list → None (not an exception)."""
    mock_edgar_api.responses["company_tickers"] = (json.dumps(_TICKERS_DATA), 200)

    result = sec_search_by_ticker("ZZZZZ")

    assert result is None


# ---------------------------------------------------------------------------
# Test 8: submissions — happy path
# ---------------------------------------------------------------------------

def test_get_company_submissions_returns_parsed_record(mock_edgar_api):
    """Full submissions record returned with expected top-level fields."""
    mock_edgar_api.responses["CIK0000789019"] = (json.dumps(_SUBMISSIONS_RESPONSE), 200)

    result = sec_get_company_submissions("0000789019")

    assert result is not None
    assert result["cik"] == "0000789019"
    assert result["entityType"] == "operating"
    assert result["sic"] == "7372"
    assert "MSFT" in result.get("tickers", [])


# ---------------------------------------------------------------------------
# Test 9: submissions — 404 returns None
# ---------------------------------------------------------------------------

def test_get_company_submissions_404_returns_none(mock_edgar_api):
    """404 from submissions endpoint → None (not an exception)."""
    # No matching pattern → fake_urlopen raises 404
    result = sec_get_company_submissions("9999999999")

    assert result is None


# ---------------------------------------------------------------------------
# Test 10: submissions disk cache prevents duplicate calls
# ---------------------------------------------------------------------------

def test_submissions_disk_cache_prevents_duplicate_calls(mock_edgar_api):
    """Second submissions fetch for same CIK hits disk cache; urlopen count stays at 1."""
    mock_edgar_api.responses["CIK0000789019"] = (json.dumps(_SUBMISSIONS_RESPONSE), 200)

    sec_get_company_submissions("0000789019")
    sec_get_company_submissions("0000789019")

    assert mock_edgar_api.calls == 1


# ---------------------------------------------------------------------------
# Test 11: tickers in-memory cache prevents duplicate API calls
# ---------------------------------------------------------------------------

def test_tickers_in_memory_cache_prevents_duplicate_api_calls(mock_edgar_api):
    """Two name searches reuse the in-memory tickers cache; only one API fetch."""
    mock_edgar_api.responses["company_tickers"] = (json.dumps(_TICKERS_DATA), 200)

    sec_search_by_name("Microsoft Corp")
    sec_search_by_name("Apple")

    assert mock_edgar_api.calls == 1
