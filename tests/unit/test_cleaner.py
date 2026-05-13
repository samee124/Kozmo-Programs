"""Tests for cobalt.intake._cleaner.structural_clean."""

import pytest
from cobalt.intake._cleaner import structural_clean


# ---------------------------------------------------------------------------
# Core named examples from the spec
# ---------------------------------------------------------------------------

def test_service_agreement_em_dash():
    assert structural_clean("Service Agreement – Dell") == "Dell"

def test_amendment_em_dash():
    assert structural_clean("Amendment 3 – Aramark") == "Aramark"

def test_amendment_multiple_dashes_rfind():
    assert structural_clean("Amendment 1 – Schedule A – Dell") == "Dell"

def test_double_quoted_name():
    assert structural_clean('"Phoenix Consulting"') == "Phoenix Consulting"

def test_surrounding_whitespace():
    assert structural_clean("  IBM  ") == "IBM"

def test_leading_ref_number():
    assert structural_clean("PO-99812 Microsoft") == "Microsoft"

def test_sow_hyphen_dash_trailing_comma():
    assert structural_clean("SOW - Accenture Tech,") == "Accenture Tech"

def test_plain_vendor_unchanged():
    assert structural_clean("IBM") == "IBM"

def test_vendor_with_suffix_unchanged():
    assert structural_clean("Aramark Services") == "Aramark Services"

def test_inv_ref_number():
    assert structural_clean("INV-001 Aramark") == "Aramark"


# ---------------------------------------------------------------------------
# Each trigger keyword tested at least once
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("trigger", [
    "Addendum 1 – Vendor Corp",
    "Schedule B – Vendor Corp",
    "Exhibit C – Vendor Corp",
    "Annex 2 – Vendor Corp",
    "Attachment D – Vendor Corp",
    "Statement of Work – Vendor Corp",
    "Master Agreement – Vendor Corp",
    "Order Form – Vendor Corp",
    "NDA – Vendor Corp",
    "BAA – Vendor Corp",
])
def test_trigger_keywords_extract_after_dash(trigger):
    result = structural_clean(trigger)
    assert result == "Vendor Corp", f"Failed for: {trigger!r} → {result!r}"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_single_quoted_name():
    assert structural_clean("'Acme Ltd'") == "Acme Ltd"

def test_trailing_semicolon_stripped():
    assert structural_clean("IBM;") == "IBM"

def test_trailing_colon_stripped():
    assert structural_clean("IBM:") == "IBM"

def test_trailing_period_stripped():
    assert structural_clean("IBM.") == "IBM"

def test_empty_after_clean_returns_original():
    # A string that would reduce to empty after all steps → return original.
    # Use a string of only stripped punctuation preceded by nothing else.
    raw = "   ,  "
    result = structural_clean(raw)
    # After strip → ",", after rstrip(",") → "", so returns original raw.
    assert result == raw

def test_hyphen_dash_variant():
    assert structural_clean("NDA - Vendor Corp") == "Vendor Corp"
