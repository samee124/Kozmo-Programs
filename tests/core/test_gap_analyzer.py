"""Tests for gap_analyzer.py."""

from __future__ import annotations

import pytest

from cobalt.core.gap_analyzer import analyse_gaps
from cobalt.models.schemas.rs_schema import GapReport


def test_missing_required_field_is_major():
    profile = {"spend_total_ttm_usd": None}
    report = analyse_gaps(profile, required_fields=["spend_total_ttm_usd"], age_thresholds={})
    assert report.gap_severity == "MAJOR"
    assert "spend_total_ttm_usd" in report.missing_fields


def test_all_required_fields_present_is_none():
    profile = {"spend_total_ttm_usd": 50000.0}
    report = analyse_gaps(profile, required_fields=["spend_total_ttm_usd"], age_thresholds={})
    assert report.gap_severity == "NONE"
    assert report.missing_fields == []


def test_low_confidence_field_is_minor():
    profile = {"my_field": {"value": "something", "confidence": "LOW"}}
    report = analyse_gaps(profile, required_fields=[], age_thresholds={})
    assert report.gap_severity == "MINOR"
    assert "my_field" in report.low_confidence_fields


def test_recommended_actions_not_empty_when_missing():
    profile = {"spend_total_ttm_usd": None}
    report = analyse_gaps(profile, required_fields=["spend_total_ttm_usd"], age_thresholds={})
    assert len(report.recommended_actions) > 0


def test_returns_gap_report_instance():
    profile = {}
    report = analyse_gaps(profile, required_fields=["spend_total_ttm_usd"], age_thresholds={})
    assert isinstance(report, GapReport)


def test_gap_severity_never_critical():
    """gap_analyzer must never return CRITICAL — that is assembler's job."""
    profile = {"spend_total_ttm_usd": None, "relationship_type": None, "dependency_tier": None}
    report = analyse_gaps(
        profile,
        required_fields=["spend_total_ttm_usd", "relationship_type", "dependency_tier"],
        age_thresholds={},
    )
    assert report.gap_severity != "CRITICAL"
    assert report.gap_severity == "MAJOR"


def test_two_missing_fields_still_major():
    profile = {"spend_total_ttm_usd": None, "dependency_tier": None}
    report = analyse_gaps(
        profile,
        required_fields=["spend_total_ttm_usd", "dependency_tier"],
        age_thresholds={},
    )
    assert report.gap_severity == "MAJOR"
    assert len(report.missing_fields) == 2
