"""DB query helpers used by the Orchestrator and VW Agent scheduler."""

import logging
import os
from datetime import datetime

from sqlalchemy import create_engine, insert, update, select
from sqlalchemy.orm import sessionmaker

from cobalt.db.models import VendorIntelligence

logger = logging.getLogger(__name__)

_SKIP_STATUSES = {
    "WAITING_HUMAN_GATE",
    "CHECKIN_SENT",
    "SURVEY_PENDING",
    "COMPLETE",
    "PAUSED",
}


def _get_session_factory():
    """Return a sessionmaker bound to the current DATABASE_URL, or None."""
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    engine = create_engine(url)
    return sessionmaker(bind=engine)


def get_due_vendors(programme_id: str, limit: int = 20) -> list[str]:
    """Return vendor_ids whose next_action_due has passed and are actionable.

    Returns:
        List of vendor_id strings, ordered by tier DESC then pcs_score ASC.
        Empty list if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("get_due_vendors: DATABASE_URL not set — returning []")
        return []

    try:
        with factory() as session:
            stmt = (
                select(VendorIntelligence.vendor_id)
                .where(VendorIntelligence.programme_id == programme_id)
                .where(VendorIntelligence.next_action_due <= datetime.utcnow())
                .where(VendorIntelligence.status.not_in(list(_SKIP_STATUSES)))
                .order_by(VendorIntelligence.tier.desc().nulls_last())
                .order_by(VendorIntelligence.pcs_score.asc())
                .limit(limit)
            )
            rows = session.execute(stmt).fetchall()
            return [row[0] for row in rows]
    except Exception as exc:
        logger.warning("get_due_vendors: DB error: %s", exc)
        return []


def insert_vendor(
    vendor_id: str,
    programme_id: str,
    vendor_name: str,
    input_name: str,
    data_class: str = "CLASS_D",
    identity_confidence: float = 0.0,
    erp_spend: float | None = None,
) -> None:
    """Insert a vendor row; silently skips on conflict (idempotent).

    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("insert_vendor: DATABASE_URL not set — skipping")
        return

    try:
        with factory() as session:
            stmt = (
                insert(VendorIntelligence)
                .values(
                    vendor_id=vendor_id,
                    programme_id=programme_id,
                    vendor_name=vendor_name,
                    input_name=input_name,
                    data_class=data_class,
                    identity_confidence=identity_confidence,
                    status="NEEDS_ACTION",
                    next_action_due=datetime.utcnow(),
                )
                .prefix_with("OR IGNORE")  # SQLite-compatible; replaced per dialect in prod
            )
            # Use ON CONFLICT DO NOTHING via a try/except for cross-DB compatibility
            try:
                session.execute(stmt)
                session.commit()
            except Exception:
                session.rollback()
    except Exception as exc:
        logger.warning("insert_vendor: DB error: %s", exc)


def update_vendor_status(
    vendor_id: str,
    status: str,
    next_action_due: datetime | None = None,
) -> None:
    """Update vendor status and optionally next_action_due.

    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("update_vendor_status: DATABASE_URL not set — skipping")
        return

    values = {
        "status": status,
        "updated_at": datetime.utcnow(),
    }
    if next_action_due is not None:
        values["next_action_due"] = next_action_due

    try:
        with factory() as session:
            session.execute(
                update(VendorIntelligence)
                .where(VendorIntelligence.vendor_id == vendor_id)
                .values(**values)
            )
            session.commit()
    except Exception as exc:
        logger.warning("update_vendor_status: DB error: %s", exc)
