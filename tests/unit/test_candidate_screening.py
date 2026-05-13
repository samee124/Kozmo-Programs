"""Tests for cobalt.tools.candidate_screening."""

import pytest
from cobalt.tools.candidate_screening import screen, screen_all, ScreenedCandidate
from cobalt.models.schemas.signal_profile_schema import EntityType


def test_screen_ibm_passes_with_key():
    result = screen("IBM")
    assert result.disposition == "PASS"
    assert result.comparison_key == "ibm"


def test_screen_john_smith_passes_as_company():
    # Without an explicit title (Mr/Mrs/Dr), two-word names like "John Smith"
    # are treated as COMPANY to avoid discarding real vendors like "Iron Mountain".
    result = screen("John Smith")
    assert result.disposition == "PASS"
    assert result.entity_type == EntityType.COMPANY


def test_screen_mr_john_smith_rejected_is_person():
    result = screen("Mr. John Smith")
    assert result.disposition == "REJECTED"
    assert result.rejection_reason == "IS_PERSON"
    assert result.entity_type == EntityType.PERSON


def test_screen_hr_department_rejected_is_internal():
    result = screen("HR Department")
    assert result.disposition == "REJECTED"
    assert result.rejection_reason == "IS_INTERNAL"
    assert result.entity_type == EntityType.INTERNAL


def test_screen_rao_consulting_passes_as_company():
    # "Consulting" is a corporate indicator — classified as COMPANY not AMBIGUOUS
    result = screen("Rao Consulting")
    assert result.disposition == "PASS"
    assert result.entity_type == EntityType.COMPANY


def test_screen_whitespace_cleaned():
    result = screen("  IBM  ")
    assert result.disposition == "PASS"
    assert result.cleaned == "IBM"


def test_screen_document_title_extracts_vendor():
    result = screen("Service Agreement – Dell")
    assert result.disposition == "PASS"
    assert result.comparison_key == "dell"


def test_screen_all_same_length_as_input():
    candidates = [
        {"raw": "IBM"},
        {"raw": "Aramark"},
        {"raw": "John Smith"},
    ]
    results = screen_all(candidates)
    assert len(results) == len(candidates)


def test_screen_all_failed_entry_returns_rejected_not_raises():
    # Inject a candidate that will cause an error inside screen().
    bad_candidates = [{"raw": None}]  # None will fail structural_clean
    results = screen_all(bad_candidates)
    assert len(results) == 1
    assert results[0].disposition == "REJECTED"
    assert results[0].rejection_reason == "SCREENING_ERROR"


def test_screen_all_mixed_batch():
    candidates = [
        {"raw": "Microsoft Corp"},
        {"raw": "Dr. Jane Doe"},   # explicit title → PERSON → REJECTED
        {"raw": "IT Department"},
    ]
    results = screen_all(candidates)
    assert results[0].disposition == "PASS"
    assert results[1].disposition == "REJECTED"
    assert results[2].disposition == "REJECTED"


def test_screened_candidate_has_source_refs():
    result = screen("IBM", source_refs=["sheet1:row2"])
    assert result.source_refs == ["sheet1:row2"]


def test_screened_candidate_has_spend_hint():
    from decimal import Decimal
    result = screen("IBM", spend_hint=Decimal("50000"))
    assert result.spend_hint == Decimal("50000")
