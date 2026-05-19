"""Tests for document_intelligence.py (Tool 2 P3)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from cobalt.tools.document_intelligence import (
    _classify_document_type,
    _fetch_document_text,
    _is_duplicate,
    _score_confidence,
    process_documents,
)
from cobalt.models.schemas.rs_schema import ContractTerms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_terms(
    document_id: str = "doc1",
    document_type: str = "CONTRACT",
    effective_date: str | None = "2024-01-01",
    expiry_date: str | None = "2025-01-01",
    auto_renews: bool | None = True,
    notice_period_days: int | None = 30,
    total_value: float | None = 100_000.0,
    currency: str | None = "USD",
    payment_terms_days: int | None = 30,
    governing_law: str | None = "English law",
    termination_clauses: list[str] | None = None,
    key_obligations: list[str] | None = None,
    sla_summary: str | None = "99.9% uptime",
    extraction_confidence: str = "HIGH",
) -> ContractTerms:
    return ContractTerms(
        document_id=document_id,
        document_type=document_type,
        effective_date=effective_date,
        expiry_date=expiry_date,
        auto_renews=auto_renews,
        notice_period_days=notice_period_days,
        total_value=total_value,
        currency=currency,
        payment_terms_days=payment_terms_days,
        governing_law=governing_law,
        termination_clauses=termination_clauses or ["30 days written notice"],
        key_obligations=key_obligations or ["Deliver monthly reports"],
        sla_summary=sla_summary,
        extraction_confidence=extraction_confidence,
    )


_LLM_RESPONSE = json.dumps({
    "effective_date": "2024-01-01",
    "expiry_date": "2025-01-01",
    "auto_renews": True,
    "notice_period_days": 30,
    "total_value": 100000.0,
    "currency": "USD",
    "payment_terms_days": 30,
    "governing_law": "English law",
    "termination_clauses": ["30 days written notice"],
    "key_obligations": ["Deliver monthly reports"],
    "sla_summary": "99.9% uptime",
})


# ---------------------------------------------------------------------------
# _classify_document_type
# ---------------------------------------------------------------------------

def test_classify_contract_from_filename():
    assert _classify_document_type("master_service_agreement.pdf", "") == "CONTRACT"


def test_classify_sow_from_content():
    assert _classify_document_type("doc.pdf", "This Statement of Work covers...") == "SOW"


def test_classify_invoice_from_filename():
    assert _classify_document_type("inv-2024-001.pdf", "") == "INVOICE"


def test_classify_qbr_from_content():
    result = _classify_document_type("meeting_notes.txt", "Quarterly Business Review Q3 2024")
    assert result == "QBR"


def test_classify_amendment_from_filename():
    assert _classify_document_type("amendment_2.pdf", "") == "AMENDMENT"


def test_classify_compliance_from_content():
    result = _classify_document_type("cert.pdf", "ISO 27001 certificate of compliance")
    assert result == "COMPLIANCE"


def test_classify_other_when_no_match():
    assert _classify_document_type("random_file.txt", "Some random text here") == "OTHER"


def test_classify_contract_takes_priority_over_sow():
    """When both contract and SOW patterns match, CONTRACT wins (precedence order)."""
    result = _classify_document_type(
        "contract.pdf",
        "This Master Agreement is a Statement of Work"
    )
    assert result == "CONTRACT"


# ---------------------------------------------------------------------------
# _fetch_document_text
# ---------------------------------------------------------------------------

def test_fetch_text_file(tmp_path):
    f = tmp_path / "contract.txt"
    f.write_text("A" * 200, encoding="utf-8")
    text, truncated = _fetch_document_text(str(f))
    assert text is not None
    assert len(text) == 200
    assert truncated is False


def test_fetch_too_short_returns_none(tmp_path):
    f = tmp_path / "short.txt"
    f.write_text("short", encoding="utf-8")
    text, _ = _fetch_document_text(str(f))
    assert text is None


def test_fetch_truncates_long_file(tmp_path):
    f = tmp_path / "long.txt"
    f.write_text("X" * 60_000, encoding="utf-8")
    text, truncated = _fetch_document_text(str(f))
    assert len(text) == 50_000
    assert truncated is True


def test_fetch_unsupported_format_returns_none(tmp_path):
    f = tmp_path / "data.docx"
    f.write_bytes(b"PK some docx content here that is long enough to pass length check" * 10)
    text, _ = _fetch_document_text(str(f))
    assert text is None


# ---------------------------------------------------------------------------
# _score_confidence
# ---------------------------------------------------------------------------

def test_high_confidence_with_many_fields():
    terms = _make_terms()  # all fields populated
    score = _score_confidence(terms, was_truncated=False)
    assert score == "HIGH"


def test_low_confidence_when_truncated():
    terms = _make_terms()
    score = _score_confidence(terms, was_truncated=True)
    assert score == "LOW"


def test_medium_confidence_few_fields():
    # 2 non-null scalar fields → MEDIUM (not HIGH because < 5, not LOW because >= 2)
    terms = ContractTerms(
        document_id="d1", document_type="CONTRACT",
        effective_date="2024-01-01", expiry_date="2025-01-01",
        auto_renews=None, notice_period_days=None,
        total_value=None, currency=None,
        payment_terms_days=None, governing_law=None,
        termination_clauses=[], key_obligations=[],
        sla_summary=None, extraction_confidence="LOW",
    )
    score = _score_confidence(terms, was_truncated=False)
    assert score == "MEDIUM"


def test_low_confidence_minimal_fields():
    terms = ContractTerms(
        document_id="d1", document_type="CONTRACT",
        effective_date=None, expiry_date=None,
        auto_renews=None, notice_period_days=None,
        total_value=None, currency=None,
        payment_terms_days=None, governing_law=None,
        termination_clauses=[], key_obligations=[],
        sla_summary=None, extraction_confidence="LOW",
    )
    score = _score_confidence(terms, was_truncated=False)
    assert score == "LOW"


# ---------------------------------------------------------------------------
# _is_duplicate
# ---------------------------------------------------------------------------

def test_duplicate_detected_same_fingerprint():
    existing = [_make_terms(effective_date="2024-01-01", total_value=100_000.0, currency="USD")]
    new = _make_terms(document_id="doc2", effective_date="2024-01-01", total_value=100_000.0, currency="USD")
    assert _is_duplicate(new, existing) is True


def test_not_duplicate_different_value():
    existing = [_make_terms(total_value=100_000.0)]
    new = _make_terms(document_id="doc2", total_value=200_000.0)
    assert _is_duplicate(new, existing) is False


def test_not_duplicate_when_fingerprint_has_nulls():
    """If effective_date, total_value, or currency is None — not a duplicate."""
    existing = [_make_terms(total_value=100_000.0)]
    new = _make_terms(document_id="doc2", effective_date=None)
    assert _is_duplicate(new, existing) is False


# ---------------------------------------------------------------------------
# process_documents — integration with mocked LLM
# ---------------------------------------------------------------------------

def test_process_documents_single_txt(tmp_path):
    doc = tmp_path / "contract.txt"
    doc.write_text("X" * 300, encoding="utf-8")

    with patch("cobalt.tools.document_intelligence.llm_call", return_value=_LLM_RESPONSE):
        result = process_documents("V-001", "PROG-001", [str(doc)])

    assert result.documents_processed == 1
    assert result.documents_skipped == 0
    assert len(result.extracted_contracts) == 1
    ct = result.extracted_contracts[0]
    assert ct.effective_date == "2024-01-01"
    assert ct.total_value == 100_000.0
    assert ct.currency == "USD"


def test_process_documents_unsupported_format(tmp_path):
    doc = tmp_path / "file.docx"
    doc.write_bytes(b"binary content")

    result = process_documents("V-001", "PROG-001", [str(doc)])

    assert result.documents_skipped == 1
    assert any("UNSUPPORTED_FORMAT" in w for w in result.extraction_warnings)


def test_process_documents_unreadable_file(tmp_path):
    doc = tmp_path / "empty.txt"
    doc.write_text("short", encoding="utf-8")  # < 100 chars → unreadable

    result = process_documents("V-001", "PROG-001", [str(doc)])

    assert result.documents_skipped == 1
    assert any("DOCUMENT_UNREADABLE" in w for w in result.extraction_warnings)


def test_process_documents_llm_failure_no_raise(tmp_path):
    doc = tmp_path / "contract.txt"
    doc.write_text("X" * 300, encoding="utf-8")

    with patch("cobalt.tools.document_intelligence.llm_call", side_effect=Exception("API down")):
        result = process_documents("V-001", "PROG-001", [str(doc)])

    assert result is not None
    assert result.documents_processed == 1  # still counted as processed
    assert any("LLM_EXTRACTION_FAILED" in w for w in result.extraction_warnings)


def test_process_documents_duplicate_skipped(tmp_path):
    doc1 = tmp_path / "contract1.txt"
    doc2 = tmp_path / "contract2.txt"
    for d in (doc1, doc2):
        d.write_text("X" * 300, encoding="utf-8")

    with patch("cobalt.tools.document_intelligence.llm_call", return_value=_LLM_RESPONSE):
        result = process_documents("V-001", "PROG-001", [str(doc1), str(doc2)])

    # Both processed, but only one added (second is a duplicate fingerprint)
    assert result.documents_processed == 2
    assert len(result.extracted_contracts) == 1
    assert any("DUPLICATE_DOCUMENT" in w for w in result.extraction_warnings)


def test_process_documents_empty_list():
    result = process_documents("V-001", "PROG-001", [])
    assert result.documents_processed == 0
    assert result.documents_skipped == 0
    assert result.extracted_contracts == []


def test_process_documents_truncated_file_is_low_confidence(tmp_path):
    doc = tmp_path / "long_contract.txt"
    doc.write_text("X" * 60_000, encoding="utf-8")

    with patch("cobalt.tools.document_intelligence.llm_call", return_value=_LLM_RESPONSE):
        result = process_documents("V-001", "PROG-001", [str(doc)])

    assert any("DOCUMENT_TRUNCATED" in w for w in result.extraction_warnings)
    assert result.extracted_contracts[0].extraction_confidence == "LOW"


def test_process_documents_markdown_strip_code_fence(tmp_path):
    doc = tmp_path / "contract.md"
    doc.write_text("Y" * 300, encoding="utf-8")

    fenced = f"```json\n{_LLM_RESPONSE}\n```"
    with patch("cobalt.tools.document_intelligence.llm_call", return_value=fenced):
        result = process_documents("V-001", "PROG-001", [str(doc)])

    assert len(result.extracted_contracts) == 1
    assert result.extracted_contracts[0].total_value == 100_000.0
