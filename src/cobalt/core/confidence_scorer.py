"""Per-field confidence scoring for Process 3.

Based on source trust level, corroboration count, and data age. Used by
spend_aggregator and rs_profile_assembler.

Confidence values are plain string literals: HIGH / MEDIUM / LOW / MISSING
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Precedence order (worst → best) for aggregate_confidence
_PRECEDENCE: list[str] = ["MISSING", "LOW", "INFERRED", "MEDIUM", "HIGH"]


def score_field(
    value: Any,
    source_trust: str,
    corroborating_sources: int,
    age_days: int,
) -> str:
    """Return confidence level for a single field value.

    Rules (evaluated in order):
    - value is None or empty → MISSING
    - age > 365 days → cap at LOW
    - OFFICIAL trust + ≥ 2 corroborating sources + age < 90 days → HIGH
    - OFFICIAL trust + 1 source OR age 90–365 days → MEDIUM
    - SYSTEM_EXPORT trust + ≥ 1 source + age < 180 days → MEDIUM
    - USER_SUBMITTED trust → LOW (always)
    - Default → LOW
    """
    # Missing check
    if value is None:
        return "MISSING"
    if isinstance(value, str) and not value.strip():
        return "MISSING"

    # Age cap
    if age_days > 365:
        return "LOW"

    if source_trust == "OFFICIAL":
        if corroborating_sources >= 2 and age_days < 90:
            return "HIGH"
        # 1 source OR age 90-365
        return "MEDIUM"

    if source_trust == "SYSTEM_EXPORT":
        if corroborating_sources >= 1 and age_days < 180:
            return "MEDIUM"
        return "LOW"

    # USER_SUBMITTED always LOW
    return "LOW"


def aggregate_confidence(levels: list[str]) -> str:
    """Return the lowest-wins aggregation of a list of confidence levels.

    Precedence (worst to best): MISSING > LOW > INFERRED > MEDIUM > HIGH
    Empty list → MISSING.
    """
    if not levels:
        return "MISSING"

    worst_idx = len(_PRECEDENCE) - 1
    for level in levels:
        try:
            idx = _PRECEDENCE.index(level)
        except ValueError:
            idx = 0  # unknown level treated as worst
        if idx < worst_idx:
            worst_idx = idx

    result = _PRECEDENCE[worst_idx]
    logger.info("aggregate_confidence: %s -> %s", levels, result)
    return result
