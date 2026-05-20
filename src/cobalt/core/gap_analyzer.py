"""Gap analysis for Process 3 profiles.

Analyses a profile dict for missing, low-confidence, and stale fields.
Produces a GapReport. Used by rs_profile_assembler.

GapReport is defined in rs_schema.py (not here) to avoid circular imports.
This module imports GapReport from rs_schema.
"""

from __future__ import annotations

import logging

from cobalt.core.staleness import days_since
from cobalt.models.schemas.rs_schema import GapReport

logger = logging.getLogger(__name__)


# Recommended actions keyed by field name or condition
_RECOMMENDED_ACTIONS: dict[str, str] = {
    "spend_total_ttm_usd":  "Upload AP extract or connect ERP system",
    "contract_count":       "Upload contract documents for extraction",
    "relationship_type":    "Provide spend data or contract documents to enable classification",
    "dependency_tier":      "Classification incomplete — add spend or contract data",
    "_stale":               "Re-run Process 3 to refresh spend and contract data",
}


def analyse_gaps(
    profile_dict: dict,
    required_fields: list[str],
    age_thresholds: dict,
) -> GapReport:
    """Inspect a profile dict and classify every missing/weak field.

    Checks:
    1. Missing: field not in dict OR value is None
    2. Low confidence: field present but confidence sub-key = LOW or MISSING
    3. Stale: field has a timestamp sub-key older than max_age_days in age_thresholds

    Returns a GapReport with gap_severity of MAJOR, MINOR, or NONE.
    CRITICAL is never produced here — it is assigned by rs_profile_assembler
    when it combines a MAJOR gap report with data_completeness = NONE.
    """
    missing_fields: list[str] = []
    low_confidence_fields: list[str] = []
    stale_fields: list[str] = []

    # 1. Missing field check
    for field_name in required_fields:
        value = profile_dict.get(field_name)
        if value is None:
            missing_fields.append(field_name)
        elif isinstance(value, str) and not value.strip():
            missing_fields.append(field_name)

    # 2. Low-confidence check — look for sub-key "confidence"
    for field_name, field_value in profile_dict.items():
        if isinstance(field_value, dict):
            conf = field_value.get("confidence")
            if conf in ("LOW", "MISSING"):
                low_confidence_fields.append(field_name)

    # 3. Staleness check
    stale_action_needed = False
    for field_name, max_age_days in age_thresholds.items():
        field_value = profile_dict.get(field_name)
        timestamp: str | None = None
        if isinstance(field_value, dict):
            timestamp = field_value.get("last_updated") or field_value.get("updated_at")
        elif isinstance(field_value, str):
            timestamp = field_value

        if timestamp is not None:
            age = days_since(timestamp)
            if age is not None and age > max_age_days:
                stale_fields.append(field_name)
                stale_action_needed = True

    # Determine severity
    if missing_fields:
        gap_severity = "MAJOR"
    elif low_confidence_fields or stale_fields:
        gap_severity = "MINOR"
    else:
        gap_severity = "NONE"

    # Build recommended actions
    actions: list[str] = []
    seen: set[str] = set()

    for field_name in missing_fields:
        action = _RECOMMENDED_ACTIONS.get(field_name)
        if action and action not in seen:
            actions.append(action)
            seen.add(action)

    if stale_action_needed:
        action = _RECOMMENDED_ACTIONS["_stale"]
        if action not in seen:
            actions.append(action)
            seen.add(action)

    logger.info(
        "analyse_gaps: severity=%s  missing=%s  low_conf=%s  stale=%s",
        gap_severity, missing_fields, low_confidence_fields, stale_fields,
    )
    return GapReport(
        missing_fields=missing_fields,
        low_confidence_fields=low_confidence_fields,
        stale_fields=stale_fields,
        gap_severity=gap_severity,
        recommended_actions=actions,
    )
