"""Tests for cobalt.intake._normalizer."""

import pytest
from cobalt.intake._normalizer import normalize, detect_country_hint


# ---------------------------------------------------------------------------
# comparison_key parametrize table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cleaned,country,expected_key", [
    ("IBM",                   None, "ibm"),
    ("I.B.M.",                None, "ibm"),
    ("I B M",                 None, "ibm"),
    ("I.B.M",                 None, "ibm"),
    ("Infosys Ltd",           None, "infosys"),
    ("Infosys Limited",       None, "infosys"),
    ("ARAMARK CORP",          None, "aramark"),
    ("Aramark",               None, "aramark"),
    ("Aramark Services",      None, "aramark services"),
    ("Aramark Corp Services", None, "aramark"),
    ("Amazon Web Services",   None, "amazon web services"),
    ("AWS",                   None, "aws"),
    ("AT&T",                  None, "att"),
    ("Coca-Cola",             None, "coca-cola"),
    ("Siemens AG",            "DE", "siemens"),
    ("Infosys Pvt Ltd",       "IN", "infosys"),
])
def test_comparison_key(cleaned, country, expected_key):
    _, key = normalize(cleaned, country)
    assert key == expected_key, f"normalize({cleaned!r}, {country!r}) key={key!r}, expected {expected_key!r}"


# ---------------------------------------------------------------------------
# detect_country_hint
# ---------------------------------------------------------------------------

def test_detect_siemens_ag_is_de():
    assert detect_country_hint("Siemens AG") == "DE"

def test_detect_infosys_pvt_ltd_is_in():
    assert detect_country_hint("Infosys Pvt Ltd") == "IN"

def test_detect_atlassian_pty_ltd_is_au():
    assert detect_country_hint("Atlassian Pty Ltd") == "AU"

def test_detect_ibm_is_none():
    assert detect_country_hint("IBM") is None


# ---------------------------------------------------------------------------
# Two-tier suffix rule
# ---------------------------------------------------------------------------

def test_two_tier_services_alone_not_stripped():
    _, key = normalize("Aramark Services", None)
    assert key == "aramark services", f"Got: {key!r}"

def test_two_tier_corp_then_services_stripped():
    _, key = normalize("Aramark Corp Services", None)
    assert key == "aramark", f"Got: {key!r}"


# ---------------------------------------------------------------------------
# Longest-first stripping
# ---------------------------------------------------------------------------

def test_longest_first_pvt_ltd_stripped_as_unit():
    _, key = normalize("Infosys Pvt Ltd", "IN")
    assert key == "infosys", f"Got: {key!r}"


# ---------------------------------------------------------------------------
# Misc edge cases
# ---------------------------------------------------------------------------

def test_coca_cola_hyphen_preserved():
    _, key = normalize("Coca-Cola", None)
    assert key == "coca-cola"

def test_at_and_t_ampersand_removed():
    _, key = normalize("AT&T", None)
    assert key == "att"

def test_normalized_name_is_title_cased():
    name, _ = normalize("infosys ltd", None)
    assert name[0].isupper()
