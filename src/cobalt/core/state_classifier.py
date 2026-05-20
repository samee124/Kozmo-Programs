"""Vendor state classifier for P4.

Maps CRI score and signals to a health label. Used by the analysis orchestrator
for DB sync after each run. Pure function — no LLM, no file I/O.
"""

from __future__ import annotations


_ELEVATION: dict[str, str] = {
    "WATCH":    "AT_RISK",
    "AT_RISK":  "CRITICAL",
    "CRITICAL": "CRITICAL",
    "HEALTHY":  "HEALTHY",
}


def classify_vendor_state(
    cri_score: float | None,
    open_findings: int,
    trend_direction: str | None,
    renewal_days: int | None,
    flags: list[str],
) -> str:
    """Return one of: HEALTHY / WATCH / AT_RISK / CRITICAL / UNKNOWN / ARCHIVED.

    Rules evaluated in order — first match wins:
    1. "ARCHIVED" in flags                                  → ARCHIVED
    2. cri_score is None                                    → UNKNOWN
    3. Compute base band from cri_score + trend_direction
    4. Apply renewal elevation if renewal_days < 30 and cri_score < 70
    """
    if "ARCHIVED" in flags:
        return "ARCHIVED"

    if cri_score is None:
        return "UNKNOWN"

    # Base band from score and trend
    if cri_score >= 80 and trend_direction != "DECLINING":
        base = "HEALTHY"
    elif cri_score >= 65:
        base = "WATCH"
    elif cri_score >= 50:
        base = "AT_RISK"
    else:
        base = "CRITICAL"

    # Renewal elevation: applies only when cri_score < 70 and renewal_days < 30
    if renewal_days is not None and renewal_days < 30 and cri_score < 70:
        base = _ELEVATION.get(base, base)

    return base
