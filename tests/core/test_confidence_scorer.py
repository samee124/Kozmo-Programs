"""Tests for confidence_scorer.py."""

from __future__ import annotations

import pytest

from cobalt.core.confidence_scorer import aggregate_confidence, score_field


# ---------------------------------------------------------------------------
# score_field
# ---------------------------------------------------------------------------

def test_score_field_none_returns_missing():
    assert score_field(None, "OFFICIAL", 3, 30) == "MISSING"


def test_score_field_empty_string_returns_missing():
    assert score_field("", "OFFICIAL", 3, 30) == "MISSING"


def test_score_field_official_high_corroboration_fresh():
    assert score_field("value", "OFFICIAL", 2, 30) == "HIGH"


def test_score_field_official_single_source():
    assert score_field("value", "OFFICIAL", 1, 30) == "MEDIUM"


def test_score_field_system_export_medium():
    assert score_field("value", "SYSTEM_EXPORT", 1, 60) == "MEDIUM"


def test_score_field_user_submitted_always_low():
    assert score_field("value", "USER_SUBMITTED", 5, 1) == "LOW"


def test_score_field_age_cap():
    assert score_field("value", "OFFICIAL", 3, 400) == "LOW"


# ---------------------------------------------------------------------------
# aggregate_confidence
# ---------------------------------------------------------------------------

def test_aggregate_confidence_lowest_wins():
    assert aggregate_confidence(["HIGH", "MEDIUM", "LOW"]) == "LOW"


def test_aggregate_confidence_all_high():
    assert aggregate_confidence(["HIGH", "HIGH"]) == "HIGH"


def test_aggregate_confidence_empty():
    assert aggregate_confidence([]) == "MISSING"


def test_aggregate_confidence_missing_wins():
    assert aggregate_confidence(["MISSING", "HIGH"]) == "MISSING"
