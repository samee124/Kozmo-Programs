"""Sync workspace file content to the DB projection layer.

Called automatically by atomic_write() after every successful commit.
Never raises — DB is a projection; failures are warnings, never blockers.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker, Session

from cobalt.core.file_system import read_md
from cobalt.db.models import VendorIntelligence

logger = logging.getLogger(__name__)


def _get_session_factory():
    """Return a sessionmaker bound to the current DATABASE_URL, or None.

    Checked on every call so monkeypatch isolation works in tests.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    engine = create_engine(url)
    return sessionmaker(bind=engine)


def _parse_datetime(value) -> datetime | None:
    """Coerce a string or datetime value to datetime, or return None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _sync_entity(session: Session, data: dict, vendor_id: str) -> None:
    session.execute(
        update(VendorIntelligence)
        .where(VendorIntelligence.vendor_id == vendor_id)
        .values(
            vendor_name=data.get("vendor_name"),
            data_class=data.get("data_class", "CLASS_D"),
            identity_confidence=data.get("identity_confidence", 0.0),
            category=data.get("category"),
            updated_at=datetime.utcnow(),
        )
    )


def _sync_coverage(session: Session, data: dict, vendor_id: str) -> None:
    session.execute(
        update(VendorIntelligence)
        .where(VendorIntelligence.vendor_id == vendor_id)
        .values(
            pcs_score=data.get("overall_pcs", 0),
            updated_at=datetime.utcnow(),
        )
    )


def _sync_action_queue(session: Session, data: dict, vendor_id: str) -> None:
    session.execute(
        update(VendorIntelligence)
        .where(VendorIntelligence.vendor_id == vendor_id)
        .values(
            status=data.get("status"),
            next_action_due=_parse_datetime(data.get("next_action_due")),
            last_run_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )


def _sync_vendor_profile(session: Session, data: dict, vendor_id: str) -> None:
    """UPDATE vendor_intelligence with V2 enrichment columns from vendor_profile.md."""
    enriched_at_raw = data.get("enriched_at")
    session.execute(
        update(VendorIntelligence)
        .where(VendorIntelligence.vendor_id == vendor_id)
        .values(
            category=data.get("category"),
            subcategory=data.get("subcategory"),
            vendor_type=data.get("vendor_type"),
            hq_country=data.get("hq_country"),
            company_size_band=data.get("company_size_band"),
            profile_status=data.get("profile_status"),
            last_enriched_at=_parse_datetime(enriched_at_raw),
            updated_at=datetime.utcnow(),
        )
    )


_HANDLERS = {
    "entity.md": _sync_entity,
    "coverage.md": _sync_coverage,
    "action_queue.md": _sync_action_queue,
    "vendor_profile.md": _sync_vendor_profile,
}


def sync_to_db(
    path: Path,
    vendor_id: str,
    programme_id: str,
) -> None:
    """Route a workspace file write to the appropriate DB update.

    Args:
        path: Path of the file that was just written.
        vendor_id: Vendor identifier.
        programme_id: Programme identifier.
    """
    handler = _HANDLERS.get(path.name)
    if handler is None:
        logger.debug("sync_to_db: no handler for %s — skipping", path.name)
        return

    factory = _get_session_factory()
    if factory is None:
        logger.warning("sync_to_db: DATABASE_URL not set — skipping DB sync for %s", path)
        return

    data = read_md(path)
    if not data:
        logger.warning("sync_to_db: empty or unreadable file %s — skipping", path)
        return

    try:
        with factory() as session:
            handler(session, data, vendor_id)
            session.commit()
    except Exception as exc:
        logger.warning("sync_to_db: DB error for %s: %s", path, exc)
