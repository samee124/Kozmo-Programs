"""Triage task builder for P4 BLOCKING gaps.

Generates structured triage task records from gap lists detected by the
finding_engine. Only BLOCKING gaps produce tasks — ENRICHMENT gaps are ignored.
No LLM. No file I/O. Pure functions.
"""

from __future__ import annotations

from datetime import date, timedelta


def _infer_owner(description: str, default_owner: str | None) -> str:
    desc_lower = description.lower()
    if "compliance" in desc_lower or "certificate" in desc_lower:
        return "compliance_owner"
    if "contract" in desc_lower or "renewal" in desc_lower:
        return "contract_owner"
    if "spend" in desc_lower or "invoice" in desc_lower:
        return "finance_owner"
    return default_owner or "vendor_owner"


def build_triage_task(
    triage_type: str,
    description: str,
    question: str,
    severity: str,
    vendor_id: str,
    programme_id: str,
    due_date: str,
    recommended_owner: str | None = None,
) -> dict:
    """Construct a single triage task dict."""
    return {
        "triage_type":        triage_type,
        "description":        description,
        "question":           question,
        "severity":           severity,
        "vendor_id":          vendor_id,
        "programme_id":       programme_id,
        "due_date":           due_date,
        "recommended_owner":  recommended_owner,
    }


def generate_triage_tasks(
    gaps: list[dict],
    flags: list[str],
    vendor_id: str,
    programme_id: str,
    default_owner: str | None = None,
    due_days_blocking: int = 7,
    due_days_enrichment: int = 30,
) -> list[dict]:
    """Return list of triage task dicts. One task per BLOCKING gap only.

    ENRICHMENT gaps are not included.

    Each task dict contains:
      triage_type, description, question, severity,
      vendor_id, programme_id, due_date (ISO string), recommended_owner
    """
    tasks: list[dict] = []
    due_date_blocking = (date.today() + timedelta(days=due_days_blocking)).isoformat()

    for gap in gaps:
        severity = gap.get("severity", "")
        if severity != "BLOCKING":
            continue

        description = gap.get("description", "")
        suggested_action = gap.get("suggested_action", "")
        owner = _infer_owner(description, default_owner)

        task = build_triage_task(
            triage_type="GAP_RESOLUTION",
            description=description,
            question=suggested_action,
            severity=severity,
            vendor_id=vendor_id,
            programme_id=programme_id,
            due_date=due_date_blocking,
            recommended_owner=owner,
        )
        tasks.append(task)

    return tasks
