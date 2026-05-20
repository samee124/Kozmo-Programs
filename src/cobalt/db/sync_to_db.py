"""Sync workspace file content to the DB projection layer.

Called explicitly by tool code after every atomic_write() call.
Never raises — DB is a projection; failures are warnings, never blockers.

Connection string is read from DATABASE_URL in .env.
Engine is created once and cached at module level for performance.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker, Session

from cobalt.core.file_system import read_md
from cobalt.db.models import ProgrammeRun, VendorIntelligence

load_dotenv()

logger = logging.getLogger(__name__)

# Module-level engine cache — created once on first use.
_engine = None
_SessionFactory = None


def _get_session_factory():
    """Return a cached sessionmaker bound to DATABASE_URL, or None if not set."""
    global _engine, _SessionFactory

    url = os.getenv("DATABASE_URL")
    if not url:
        return None

    if _SessionFactory is None:
        connect_args = {}
        if "mssql" in url or "pyodbc" in url:
            connect_args["fast_executemany"] = True
        _engine = create_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,   # detect stale Azure SQL connections
            pool_recycle=1800,    # recycle connections every 30 min
        )
        _SessionFactory = sessionmaker(bind=_engine)

    return _SessionFactory


def _parse_datetime(value) -> datetime | None:
    """Coerce a string or datetime to datetime, or return None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


# ─── Individual sync handlers ─────────────────────────────────────────────────

def _sync_entity(session: Session, data: dict, vendor_id: str) -> None:
    """entity.md written → update identity + classification columns."""
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
    """coverage.md written → update PcsScore."""
    session.execute(
        update(VendorIntelligence)
        .where(VendorIntelligence.vendor_id == vendor_id)
        .values(
            pcs_score=data.get("overall_pcs", 0),
            updated_at=datetime.utcnow(),
        )
    )


def _sync_action_queue(session: Session, data: dict, vendor_id: str) -> None:
    """action_queue.md written → update scheduling columns."""
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
    """vendor_profile.md written → update P2 enrichment columns."""
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
            last_enriched_at=_parse_datetime(data.get("enriched_at")),
            updated_at=datetime.utcnow(),
        )
    )


def _sync_rs_profile(session: Session, data: dict, vendor_id: str) -> None:
    """relationship_spend_profile.md written → update P3 relationship & spend columns."""
    session.execute(
        update(VendorIntelligence)
        .where(VendorIntelligence.vendor_id == vendor_id)
        .values(
            rs_last_updated=_parse_datetime(data.get("last_updated")),
            spend_total_usd=data.get("spend_total_ttm_usd"),
            dependency_tier=data.get("dependency_tier"),
            relationship_type=data.get("relationship_type"),
            updated_at=datetime.utcnow(),
        )
    )


def _sync_programme_plan(session: Session, data: dict, vendor_id: str) -> None:
    """programme_plan.md written → set ProgrammeRun.Status to IN_PROGRESS."""
    programme_id = data.get("programme_id")
    if not programme_id:
        return
    session.execute(
        update(ProgrammeRun)
        .where(ProgrammeRun.programme_id == programme_id)
        .values(status="IN_PROGRESS")
    )


def _sync_vendor_register(session: Session, data: dict, vendor_id: str) -> None:
    """vendor_register.md written → update ProgrammeRun.TotalVendors count."""
    programme_id = data.get("programme_id")
    total = data.get("total_vendors")
    if not programme_id or total is None:
        return
    session.execute(
        update(ProgrammeRun)
        .where(ProgrammeRun.programme_id == programme_id)
        .values(total_vendors=int(total))
    )


def _sync_triage_queue(session: Session, data: dict, vendor_id: str) -> None:
    """triage_queue.md written → update ProgrammeRun.Triage counter."""
    programme_id = data.get("programme_id")
    triage_count = data.get("triage_count")
    if not programme_id or triage_count is None:
        return
    session.execute(
        update(ProgrammeRun)
        .where(ProgrammeRun.programme_id == programme_id)
        .values(triage=int(triage_count))
    )


def _sync_analysis_result(session: Session, data: dict, vendor_id: str) -> None:
    """analysis_result.md written → update P4 columns on VendorIntelligence."""
    session.execute(
        update(VendorIntelligence)
        .where(VendorIntelligence.vendor_id == vendor_id)
        .values(
            cri_score=data.get("cri_score"),
            health_band=data.get("health_band"),
            vendor_state=data.get("vendor_state"),
            last_analysed_at=_parse_datetime(data.get("last_analysed_at")),
            updated_at=datetime.utcnow(),
        )
    )


# ─── Handler registry ─────────────────────────────────────────────────────────

_HANDLERS = {
    "entity.md":                        _sync_entity,
    "coverage.md":                      _sync_coverage,
    "action_queue.md":                  _sync_action_queue,
    "vendor_profile.md":                _sync_vendor_profile,
    "relationship_spend_profile.md":    _sync_rs_profile,
    "programme_plan.md":                _sync_programme_plan,
    "vendor_register.md":               _sync_vendor_register,
    "triage_queue.md":                  _sync_triage_queue,
    "analysis_result.md":               _sync_analysis_result,
}


# ─── Public entry point ───────────────────────────────────────────────────────

def sync_to_db(
    path: Path,
    vendor_id: str | None,
    programme_id: str | None,
) -> None:
    """Route a workspace file write to the appropriate DB update.

    Args:
        path:         Path of the file that was just written.
        vendor_id:    Vendor identifier. None for programme-level files.
        programme_id: Programme identifier.
    """
    handler = _HANDLERS.get(path.name)
    if handler is None:
        logger.debug("sync_to_db: no handler for %s — skipping", path.name)
        return

    factory = _get_session_factory()
    if factory is None:
        logger.warning(
            "sync_to_db: DATABASE_URL not set — skipping DB sync for %s", path
        )
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
