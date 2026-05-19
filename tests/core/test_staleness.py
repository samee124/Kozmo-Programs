"""Tests for staleness.py."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cobalt.core.staleness import days_since, is_stale, staleness_tier


def _iso(d: date) -> str:
    return d.isoformat()


def test_is_stale_none():
    assert is_stale(None, 30) is True


def test_is_stale_recent_not_stale():
    recent = date.today() - timedelta(days=29)
    assert is_stale(_iso(recent), 30) is False


def test_is_stale_old_is_stale():
    old = date.today() - timedelta(days=31)
    assert is_stale(_iso(old), 30) is True


def test_days_since_none():
    assert days_since(None) is None


def test_days_since_14_days():
    past = date.today() - timedelta(days=14)
    assert days_since(_iso(past)) == 14


def test_days_since_datetime_string():
    past = date.today() - timedelta(days=5)
    iso_dt = f"{past.isoformat()}T12:00:00Z"
    assert days_since(iso_dt) == 5


def test_staleness_tier_none():
    assert staleness_tier(None) == "UNKNOWN"


def test_staleness_tier_fresh():
    assert staleness_tier(15) == "FRESH"


def test_staleness_tier_ageing():
    assert staleness_tier(60) == "AGEING"


def test_staleness_tier_stale():
    assert staleness_tier(100) == "STALE"
