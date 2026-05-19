"""Tests for name_matching.py."""

from __future__ import annotations

import pytest

from cobalt.core.name_matching import best_match, fuzzy_match, normalise_for_match


# ---------------------------------------------------------------------------
# normalise_for_match
# ---------------------------------------------------------------------------

def test_normalise_strips_ltd_and_corp():
    assert normalise_for_match("Acme Corporation Ltd") == "acme"


def test_normalise_strips_ibm_global():
    assert normalise_for_match("  IBM Global Services  ") == "ibm"


def test_normalise_acme_ltd_vs_inc():
    assert normalise_for_match("Acme Ltd") == "acme"
    assert normalise_for_match("Acme Inc") == "acme"


def test_normalise_empty_string():
    assert normalise_for_match("") == ""


def test_normalise_removes_punctuation():
    result = normalise_for_match("Acme, Corp.")
    assert "," not in result
    assert "." not in result


# ---------------------------------------------------------------------------
# fuzzy_match
# ---------------------------------------------------------------------------

def test_fuzzy_match_identical():
    assert fuzzy_match("Microsoft", "Microsoft") == 1.0


def test_fuzzy_match_typo_tolerance():
    score = fuzzy_match("Microsoft", "Microsft")
    assert score > 0.90


def test_fuzzy_match_case_insensitive_after_normalise():
    # Both normalise to "acme"
    score = fuzzy_match("Acme Ltd", "Acme Inc")
    assert score == 1.0


def test_fuzzy_match_empty_both():
    assert fuzzy_match("", "") == 1.0


def test_fuzzy_match_one_empty():
    assert fuzzy_match("", "Microsoft") == 0.0


# ---------------------------------------------------------------------------
# best_match
# ---------------------------------------------------------------------------

def test_best_match_finds_google_llc():
    result = best_match("Google", ["Alphabet Inc", "Google LLC", "Microsoft Corp"], threshold=0.85)
    assert result is not None
    candidate, score = result
    assert candidate == "Google LLC"
    assert score == 1.0


def test_best_match_no_match_below_threshold():
    result = best_match("Xyz", ["Apple", "Amazon"], threshold=0.85)
    assert result is None


def test_best_match_empty_candidates():
    assert best_match("Google", []) is None


def test_best_match_empty_query_empty_candidates():
    assert best_match("", []) is None


def test_best_match_tie_returns_earliest():
    """When two candidates have identical score, earliest is returned."""
    result = best_match("Acme", ["Acme Ltd", "Acme Inc"], threshold=0.0)
    assert result is not None
    assert result[0] == "Acme Ltd"
