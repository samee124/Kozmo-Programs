"""Date-based freshness checks for Process 3.

Used by the orchestrator gate check and gap_analyzer. Pure functions, no I/O.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def _parse_date(iso_timestamp: str) -> date | None:
    """Parse ISO 8601 date or datetime string to a date. Returns None on failure."""
    try:
        if "T" in iso_timestamp:
            dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
            return dt.date()
        return date.fromisoformat(iso_timestamp)
    except (ValueError, AttributeError):
        return None


def is_stale(last_updated_iso: str | None, max_age_days: int) -> bool:
    """Return True if the timestamp is older than max_age_days from today.

    Also returns True if last_updated_iso is None (never updated = always stale).
    """
    if last_updated_iso is None:
        return True
    parsed = _parse_date(last_updated_iso)
    if parsed is None:
        return True
    today = date.today()
    delta = (today - parsed).days
    return delta >= max_age_days


def days_since(iso_timestamp: str | None) -> int | None:
    """Return number of full calendar days elapsed since iso_timestamp.

    Returns None if iso_timestamp is None or unparseable.
    """
    if iso_timestamp is None:
        return None
    parsed = _parse_date(iso_timestamp)
    if parsed is None:
        return None
    return (date.today() - parsed).days


def staleness_tier(days: int | None) -> str:
    """Categorise a days-since value into a freshness tier.

    None → UNKNOWN
    0–30 → FRESH
    31–90 → AGEING
    > 90 → STALE
    """
    if days is None:
        return "UNKNOWN"
    if days <= 30:
        return "FRESH"
    if days <= 90:
        return "AGEING"
    return "STALE"
