"""Tests for cobalt.core.state_classifier — classify_vendor_state()."""

import pytest

from cobalt.core.state_classifier import classify_vendor_state


def _classify(
    cri_score,
    open_findings=0,
    trend_direction=None,
    renewal_days=None,
    flags=None,
):
    return classify_vendor_state(
        cri_score=cri_score,
        open_findings=open_findings,
        trend_direction=trend_direction,
        renewal_days=renewal_days,
        flags=flags or [],
    )


# ---------------------------------------------------------------------------
# Base rules
# ---------------------------------------------------------------------------

def test_cri_none_returns_unknown():
    assert _classify(None) == "UNKNOWN"


def test_archived_flag_overrides_everything():
    assert _classify(85, flags=["ARCHIVED"]) == "ARCHIVED"


def test_archived_flag_overrides_even_high_cri():
    assert _classify(100, trend_direction="IMPROVING", flags=["ARCHIVED"]) == "ARCHIVED"


def test_cri_85_improving_is_healthy():
    assert _classify(85, trend_direction="IMPROVING") == "HEALTHY"


def test_cri_85_stable_is_healthy():
    assert _classify(85, trend_direction="STABLE") == "HEALTHY"


def test_cri_85_declining_is_watch():
    # DECLINING overrides the >= 80 → HEALTHY rule
    assert _classify(85, trend_direction="DECLINING") == "WATCH"


def test_cri_80_no_trend_is_healthy():
    # trend_direction=None means not DECLINING → HEALTHY
    assert _classify(80) == "HEALTHY"


def test_cri_70_is_watch():
    assert _classify(70) == "WATCH"


def test_cri_65_is_watch():
    assert _classify(65) == "WATCH"


def test_cri_55_is_at_risk():
    assert _classify(55) == "AT_RISK"


def test_cri_50_is_at_risk():
    assert _classify(50) == "AT_RISK"


def test_cri_45_is_critical():
    assert _classify(45) == "CRITICAL"


def test_cri_0_is_critical():
    assert _classify(0) == "CRITICAL"


# ---------------------------------------------------------------------------
# Renewal elevation
# ---------------------------------------------------------------------------

def test_cri_68_renewal_25_elevated_to_at_risk():
    # Base = WATCH (cri=68), cri < 70 and renewal < 30 → elevate to AT_RISK
    assert _classify(68, renewal_days=25) == "AT_RISK"


def test_cri_55_renewal_25_elevated_to_critical():
    # Base = AT_RISK (cri=55), cri < 70 and renewal < 30 → elevate to CRITICAL
    assert _classify(55, renewal_days=25) == "CRITICAL"


def test_cri_68_renewal_90_no_elevation():
    # renewal_days=90 >= 30, so rule 3 does not apply → stays WATCH
    assert _classify(68, renewal_days=90) == "WATCH"


def test_cri_68_renewal_30_no_elevation():
    # renewal_days=30 is NOT < 30, so rule does not apply → stays WATCH
    assert _classify(68, renewal_days=30) == "WATCH"


def test_critical_stays_critical_with_renewal():
    # CRITICAL stays CRITICAL — _ELEVATION["CRITICAL"] = "CRITICAL"
    assert _classify(40, renewal_days=10) == "CRITICAL"


def test_healthy_stays_healthy_with_renewal():
    # cri=80 is not < 70, so renewal elevation condition is False → HEALTHY
    assert _classify(80, renewal_days=10) == "HEALTHY"


def test_renewal_none_no_elevation():
    assert _classify(68, renewal_days=None) == "WATCH"


# ---------------------------------------------------------------------------
# open_findings and trend_direction do not affect base band (V1)
# ---------------------------------------------------------------------------

def test_open_findings_does_not_change_band():
    # open_findings is accepted but unused in V1
    assert _classify(70, open_findings=10) == "WATCH"
    assert _classify(70, open_findings=0) == "WATCH"


def test_trend_direction_only_affects_healthy_threshold():
    # For cri=70 (WATCH), trend_direction irrelevant
    assert _classify(70, trend_direction="DECLINING") == "WATCH"
    assert _classify(70, trend_direction="IMPROVING") == "WATCH"
