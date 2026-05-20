"""Tests for cobalt.agents.research_agent."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cobalt.agents.research_agent as ra_module
from cobalt.agents.research_agent import ResearchAgent
from cobalt.core.exceptions import BraveSearchError
from cobalt.models.schemas.signal_profile_schema import ApSignal, ErpSignal

agent = ResearchAgent()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_minimal_pdf(path: Path, text: str = "Test content") -> None:
    """Write a minimal valid PDF with extractable text content."""
    stream_bytes = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET\n".encode()
    obj1 = b"1 0 obj\n<</Type /Catalog /Pages 2 0 R>>\nendobj\n"
    obj2 = b"2 0 obj\n<</Type /Pages /Kids [3 0 R] /Count 1>>\nendobj\n"
    obj3 = (
        b"3 0 obj\n<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>>\nendobj\n"
    )
    obj4 = (
        b"4 0 obj\n<</Length " + str(len(stream_bytes)).encode() +
        b">>\nstream\n" + stream_bytes + b"endstream\nendobj\n"
    )
    obj5 = (
        b"5 0 obj\n<</Type /Font /Subtype /Type1 /BaseFont /Helvetica"
        b" /Encoding /WinAnsiEncoding>>\nendobj\n"
    )
    header = b"%PDF-1.4\n"
    off1 = len(header)
    off2 = off1 + len(obj1)
    off3 = off2 + len(obj2)
    off4 = off3 + len(obj3)
    off5 = off4 + len(obj4)
    xref_start = off5 + len(obj5)
    xref = b"xref\n0 6\n0000000000 65535 f \n"
    for off in [off1, off2, off3, off4, off5]:
        xref += f"{off:010d} 00000 n \n".encode()
    trailer = (
        b"trailer\n<</Size 6 /Root 1 0 R>>\nstartxref\n" +
        str(xref_start).encode() + b"\n%%EOF\n"
    )
    path.write_bytes(header + obj1 + obj2 + obj3 + obj4 + obj5 + xref + trailer)


def _make_brave_mock(content: str = "company result"):
    """Return a fake search_with_content function that returns a single result."""
    calls: list[str] = []

    def _fake(query, *, result_type="web", count=5, freshness=None, fetch_content=True):
        calls.append(query)
        return [{"url": "https://example.com", "content": content,
                 "title": "Result", "published_date": None, "full_content": content}]

    _fake.calls = calls
    return _fake


# ---------------------------------------------------------------------------
# web_research — tier behaviour
# ---------------------------------------------------------------------------

def test_skip_tier_returns_empty():
    result = agent.web_research("IBM", tier="SKIP")
    assert result == ""


def test_no_api_key_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))  # empty cache — no stale hits
    monkeypatch.setattr(ra_module, "DDGS", None)  # disable DDG fallback
    result = agent.web_research("IBM", tier="FAST")
    assert result == ""


def test_fast_tier_calls_brave_once(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    fake = _make_brave_mock("IBM is a tech company")
    monkeypatch.setattr(ra_module, "search_with_content", fake)

    result = agent.web_research("IBM", tier="FAST")

    assert result == "IBM is a tech company"
    assert len(fake.calls) == 1


def test_standard_tier_calls_brave_once(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    fake = _make_brave_mock("standard result")
    monkeypatch.setattr(ra_module, "search_with_content", fake)

    result = agent.web_research("Aramark", tier="STANDARD", country_hint="US")

    assert result == "standard result"
    assert len(fake.calls) == 1


def test_deep_tier_calls_brave_twice(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))
    call_count = 0

    def _fake(query, *, result_type="web", count=5, freshness=None, fetch_content=True):
        nonlocal call_count
        call_count += 1
        return [{"url": "https://example.com", "content": f"result {call_count}",
                 "title": "T", "published_date": None, "full_content": f"result {call_count}"}]

    monkeypatch.setattr(ra_module, "search_with_content", _fake)

    result = agent.web_research("IBM", tier="DEEP")

    assert call_count == 2
    assert "\n\n" in result


def test_brave_exception_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))

    def _boom(query, *, result_type="web", count=5, freshness=None, fetch_content=True):
        raise BraveSearchError("network failure")

    monkeypatch.setattr(ra_module, "search_with_content", _boom)

    result = agent.web_research("IBM", tier="FAST")
    assert result == ""


# ---------------------------------------------------------------------------
# web_research — caching
# ---------------------------------------------------------------------------

def test_caching_prevents_duplicate_brave_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    monkeypatch.setenv("SEARCH_CACHE_DIR", str(tmp_path))

    http_calls: list[str] = []

    def _fake_http(url, params, headers, timeout=10):
        http_calls.append(url)
        return json.dumps({"web": {"results": [
            {"url": "https://ibm.com", "title": "IBM",
             "description": "cached result", "page_age": None}
        ]}})

    monkeypatch.setattr("cobalt.core.search._http_get_json", _fake_http)
    monkeypatch.setattr("cobalt.core.search.fetch_url", lambda url, **kw: ("IBM is a company", 200))

    agent.web_research("IBM", tier="FAST")
    agent.web_research("IBM", tier="FAST")

    assert len(http_calls) == 1


# ---------------------------------------------------------------------------
# erp_batch_scan
# ---------------------------------------------------------------------------

def test_erp_batch_scan_no_endpoint_returns_empty(monkeypatch):
    monkeypatch.delenv("ERP_ENDPOINT", raising=False)
    result = agent.erp_batch_scan(["ibm", "aramark"])
    assert result == {}


def test_erp_batch_scan_returns_dict_not_raises(monkeypatch):
    monkeypatch.setenv("ERP_ENDPOINT", "http://erp.example.com")
    result = agent.erp_batch_scan(["ibm"])
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# fetch_pdf_text
# ---------------------------------------------------------------------------

def test_fetch_pdf_text_valid_pdf(tmp_path):
    pdf_path = tmp_path / "test.pdf"
    _write_minimal_pdf(pdf_path, "Test content")
    result = agent.fetch_pdf_text(str(pdf_path))
    assert isinstance(result, str)
    assert len(result) > 0


def test_fetch_pdf_text_missing_file():
    result = agent.fetch_pdf_text("/nonexistent/path/file.pdf")
    assert result == ""


def test_fetch_pdf_text_unreadable_file(tmp_path):
    bad_path = tmp_path / "bad.pdf"
    bad_path.write_bytes(b"not a valid pdf at all")
    result = agent.fetch_pdf_text(str(bad_path))
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# fetch_google_drive_docs
# ---------------------------------------------------------------------------

def test_fetch_google_drive_docs_local_folder(tmp_path):
    _write_minimal_pdf(tmp_path / "vendor_a.pdf", "Vendor A contract")
    _write_minimal_pdf(tmp_path / "vendor_b.pdf", "Vendor B contract")

    results = agent.fetch_google_drive_docs(str(tmp_path))

    assert len(results) == 2
    filenames = {r["filename"] for r in results}
    assert "vendor_a.pdf" in filenames
    assert "vendor_b.pdf" in filenames
    for r in results:
        assert "file_path" in r
        assert "raw_text" in r
        assert "relative_path" in r
        assert isinstance(r["raw_text"], str)


def test_fetch_google_drive_docs_empty_folder(tmp_path):
    results = agent.fetch_google_drive_docs(str(tmp_path))
    assert results == []


def test_fetch_google_drive_docs_nonexistent_folder():
    results = agent.fetch_google_drive_docs("/nonexistent/folder/path")
    assert results == []


def test_fetch_google_drive_docs_exception_returns_empty(monkeypatch):
    def _bad_exists(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "exists", _bad_exists)
    result = agent.fetch_google_drive_docs("/some/path")
    assert result == []


# ---------------------------------------------------------------------------
# ap_batch_scan
# ---------------------------------------------------------------------------

def test_ap_batch_scan_no_endpoint_returns_empty(monkeypatch):
    monkeypatch.delenv("AP_ENDPOINT", raising=False)
    result = agent.ap_batch_scan(["ibm", "aramark"])
    assert result == {}


def test_ap_batch_scan_returns_dict_not_raises(monkeypatch):
    monkeypatch.setenv("AP_ENDPOINT", "http://ap.example.com")
    result = agent.ap_batch_scan(["ibm"])
    assert isinstance(result, dict)
