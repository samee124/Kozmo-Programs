"""DB query helpers used by the Orchestrator and VW Agent scheduler.

Targets SQL Server (mssql+pyodbc). All queries are MSSQL-compatible:
  - No NULLS LAST (uses CASE WHEN workaround)
  - No OR IGNORE (uses IntegrityError catch for idempotent insert)
  - No INTERVAL (uses DATEADD equivalent in Python: timedelta)

Connection string is read from DATABASE_URL in .env.
Engine is created once and cached at module level for performance.
"""

import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import case, create_engine, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cobalt.db.models import ProgrammeRun, TriageItem, UserAccount, VendorCheckin, VendorIntelligence

load_dotenv()

logger = logging.getLogger(__name__)

_SKIP_STATUSES = {
    "WAITING_HUMAN_GATE",
    "CHECKIN_SENT",
    "SURVEY_PENDING",
    "COMPLETE",
    "PAUSED",
}

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
        is_mssql = "mssql" in url or "pyodbc" in url
        if is_mssql:
            connect_args["fast_executemany"] = True
        engine_kwargs = {
            "connect_args": connect_args,
            "pool_pre_ping": True,       # detect stale Azure SQL connections
            "pool_recycle": 1800,        # recycle connections every 30 min
        }
        _engine = create_engine(url, **engine_kwargs)
        _SessionFactory = sessionmaker(bind=_engine)

    return _SessionFactory


def get_due_vendors(programme_id: str, limit: int = 20) -> list[str]:
    """Return vendor_ids whose NextActionDue has passed and are actionable.

    Ordering: TIER_1 first (nulls last via CASE WHEN), then lowest PcsScore
    (most incomplete vendors processed first).

    Returns:
        List of vendor_id strings. Empty if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("get_due_vendors: DATABASE_URL not set — returning []")
        return []

    try:
        with factory() as session:
            # MSSQL has no NULLS LAST — use CASE WHEN to push nulls to end
            tier_nulls_last = case(
                (VendorIntelligence.tier.is_(None), 1), else_=0
            )
            stmt = (
                select(VendorIntelligence.vendor_id)
                .where(VendorIntelligence.programme_id == programme_id)
                .where(VendorIntelligence.next_action_due <= datetime.utcnow())
                .where(VendorIntelligence.status.not_in(list(_SKIP_STATUSES)))
                .order_by(
                    tier_nulls_last,                       # rows with tier NULL sort last
                    VendorIntelligence.tier.desc(),        # TIER_1 > TIER_2 > TIER_3
                    VendorIntelligence.pcs_score.asc(),    # lowest completeness first
                )
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
    user_id: str | None = None,
    data_class: str = "CLASS_D",
    identity_confidence: float = 0.0,
) -> None:
    """Insert a new vendor row. Silently skips if the vendor already exists (idempotent).

    Uses IntegrityError catch instead of OR IGNORE (MSSQL-compatible).
    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("insert_vendor: DATABASE_URL not set — skipping")
        return

    try:
        with factory() as session:
            session.execute(
                insert(VendorIntelligence).values(
                    vendor_id=vendor_id,
                    programme_id=programme_id,
                    user_id=user_id,
                    vendor_name=vendor_name,
                    input_name=input_name,
                    data_class=data_class,
                    identity_confidence=identity_confidence,
                    status="NEEDS_ACTION",
                    next_action_due=datetime.utcnow(),
                )
            )
            session.commit()
    except IntegrityError:
        # Vendor already exists — idempotent, not an error
        logger.debug("insert_vendor: vendor %s already exists — skipping", vendor_id)
    except Exception as exc:
        logger.warning("insert_vendor: DB error: %s", exc)


def update_vendor_status(
    vendor_id: str,
    status: str,
    next_action_due: datetime | None = None,
) -> None:
    """Update vendor Status and optionally NextActionDue.

    Always sets UpdatedAt. Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("update_vendor_status: DATABASE_URL not set — skipping")
        return

    values: dict = {
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


def get_vendors_needing_enrichment(programme_id: str) -> list[str]:
    """Return vendor_ids whose P2 enrichment is absent or stale (> 90 days old).

    Returns:
        List of vendor_id strings. Empty if DATABASE_URL is not set.
    """
    from datetime import timedelta

    factory = _get_session_factory()
    if factory is None:
        logger.warning("get_vendors_needing_enrichment: DATABASE_URL not set — returning []")
        return []

    stale_threshold = datetime.utcnow() - timedelta(days=90)

    try:
        with factory() as session:
            stmt = select(VendorIntelligence.vendor_id).where(
                VendorIntelligence.programme_id == programme_id,
                (
                    VendorIntelligence.profile_status.is_(None)
                    | VendorIntelligence.last_enriched_at.is_(None)
                    | (VendorIntelligence.last_enriched_at < stale_threshold)
                ),
            )
            rows = session.execute(stmt).fetchall()
            return [row[0] for row in rows]
    except Exception as exc:
        logger.warning("get_vendors_needing_enrichment: DB error: %s", exc)
        return []


# ─── UserAccount ──────────────────────────────────────────────────────────────

def insert_user(
    user_id: str,
    user_name: str,
    email: str,
    subscription_tier: str = "STARTER",
) -> None:
    """Insert a new UserAccount row. Silently skips if user already exists (idempotent).

    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("insert_user: DATABASE_URL not set — skipping")
        return

    try:
        with factory() as session:
            session.execute(
                insert(UserAccount).values(
                    user_id=user_id,
                    user_name=user_name,
                    email=email,
                    subscription_tier=subscription_tier,
                    is_active=True,
                )
            )
            session.commit()
    except IntegrityError:
        logger.debug("insert_user: user %s already exists — skipping", user_id)
    except Exception as exc:
        logger.warning("insert_user: DB error: %s", exc)


# ─── ProgrammeRun ─────────────────────────────────────────────────────────────

def insert_programme(
    programme_id: str,
    user_id: str,
    programme_name: str | None = None,
    input_file: str | None = None,
) -> None:
    """Insert a new ProgrammeRun row. Silently skips if already exists (idempotent).

    Status is set to PENDING on creation. Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("insert_programme: DATABASE_URL not set — skipping")
        return

    try:
        with factory() as session:
            session.execute(
                insert(ProgrammeRun).values(
                    programme_id=programme_id,
                    user_id=user_id,
                    programme_name=programme_name,
                    status="PENDING",
                    input_file=input_file,
                )
            )
            session.commit()
    except IntegrityError:
        logger.debug("insert_programme: programme %s already exists — skipping", programme_id)
    except Exception as exc:
        logger.warning("insert_programme: DB error: %s", exc)


def update_programme_status(programme_id: str, status: str) -> None:
    """Update ProgrammeRun.Status. Returns silently if DATABASE_URL is not set."""
    factory = _get_session_factory()
    if factory is None:
        logger.warning("update_programme_status: DATABASE_URL not set — skipping")
        return

    try:
        with factory() as session:
            session.execute(
                update(ProgrammeRun)
                .where(ProgrammeRun.programme_id == programme_id)
                .values(status=status)
            )
            session.commit()
    except Exception as exc:
        logger.warning("update_programme_status: DB error: %s", exc)


def update_programme_counters(
    programme_id: str,
    total_vendors: int | None = None,
    confirmed: int | None = None,
    triage: int | None = None,
    discarded: int | None = None,
    blocked: int | None = None,
) -> None:
    """Update ProgrammeRun progress counters. Only provided (non-None) values are set.

    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("update_programme_counters: DATABASE_URL not set — skipping")
        return

    values: dict = {}
    if total_vendors is not None:
        values["total_vendors"] = total_vendors
    if confirmed is not None:
        values["confirmed"] = confirmed
    if triage is not None:
        values["triage"] = triage
    if discarded is not None:
        values["discarded"] = discarded
    if blocked is not None:
        values["blocked"] = blocked

    if not values:
        return

    try:
        with factory() as session:
            session.execute(
                update(ProgrammeRun)
                .where(ProgrammeRun.programme_id == programme_id)
                .values(**values)
            )
            session.commit()
    except Exception as exc:
        logger.warning("update_programme_counters: DB error: %s", exc)


# ─── VendorCheckin ────────────────────────────────────────────────────────────

def insert_checkin(
    checkin_id: str,
    vendor_id: str,
    programme_id: str,
    user_id: str,
    reply_deadline: datetime | None = None,
) -> None:
    """Insert a VendorCheckin row when the VW Agent dispatches a check-in.

    Idempotent — silently skips if checkin_id already exists.
    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("insert_checkin: DATABASE_URL not set — skipping")
        return

    try:
        with factory() as session:
            session.execute(
                insert(VendorCheckin).values(
                    checkin_id=checkin_id,
                    vendor_id=vendor_id,
                    programme_id=programme_id,
                    user_id=user_id,
                    sent_at=datetime.utcnow(),
                    reply_deadline=reply_deadline,
                    status="SENT",
                )
            )
            session.commit()
    except IntegrityError:
        logger.debug("insert_checkin: checkin %s already exists — skipping", checkin_id)
    except Exception as exc:
        logger.warning("insert_checkin: DB error: %s", exc)


def update_checkin_status(
    checkin_id: str,
    status: str,
    responded_at: datetime | None = None,
) -> None:
    """Update VendorCheckin.Status and optionally RespondedAt.

    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("update_checkin_status: DATABASE_URL not set — skipping")
        return

    values: dict = {"status": status}
    if responded_at is not None:
        values["responded_at"] = responded_at

    try:
        with factory() as session:
            session.execute(
                update(VendorCheckin)
                .where(VendorCheckin.checkin_id == checkin_id)
                .values(**values)
            )
            session.commit()
    except Exception as exc:
        logger.warning("update_checkin_status: DB error: %s", exc)


# ─── TriageItem ───────────────────────────────────────────────────────────────

def insert_triage_item(
    triage_id: str,
    vendor_id: str,
    programme_id: str,
    user_id: str,
    triage_type: str,
    question: str,
    raw_input: str | None = None,
    options: str | None = None,
    sla_deadline: datetime | None = None,
) -> None:
    """Insert a TriageItem row when automation cannot make a confident decision.

    Idempotent — silently skips if triage_id already exists.
    options must be a JSON string if provided (MSSQL has no native JSON column).
    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("insert_triage_item: DATABASE_URL not set — skipping")
        return

    try:
        with factory() as session:
            session.execute(
                insert(TriageItem).values(
                    triage_id=triage_id,
                    vendor_id=vendor_id,
                    programme_id=programme_id,
                    user_id=user_id,
                    raw_input=raw_input,
                    triage_type=triage_type,
                    question=question,
                    options=options,
                    status="PENDING",
                    sla_deadline=sla_deadline,
                )
            )
            session.commit()
    except IntegrityError:
        logger.debug("insert_triage_item: triage %s already exists — skipping", triage_id)
    except Exception as exc:
        logger.warning("insert_triage_item: DB error: %s", exc)


def resolve_triage_item(triage_id: str, resolution: str) -> None:
    """Mark a TriageItem as RESOLVED with the given resolution string.

    Returns silently if DATABASE_URL is not set.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("resolve_triage_item: DATABASE_URL not set — skipping")
        return

    try:
        with factory() as session:
            session.execute(
                update(TriageItem)
                .where(TriageItem.triage_id == triage_id)
                .values(
                    status="RESOLVED",
                    resolved_at=datetime.utcnow(),
                    resolution=resolution,
                )
            )
            session.commit()
    except Exception as exc:
        logger.warning("resolve_triage_item: DB error: %s", exc)


def get_pending_triage(user_id: str, programme_id: str | None = None) -> list[dict]:
    """Return all PENDING triage items for a user, ordered by SLA deadline ascending.

    Args:
        user_id:      Filter to this user's triage items.
        programme_id: Optional — narrow to a specific programme.

    Returns:
        List of dicts with keys: triage_id, vendor_id, triage_type, question,
        options, sla_deadline, created_at.
        Empty list if DATABASE_URL is not set or no items found.
    """
    factory = _get_session_factory()
    if factory is None:
        logger.warning("get_pending_triage: DATABASE_URL not set — returning []")
        return []

    # MSSQL: NULLS LAST workaround for sla_deadline ordering
    sla_nulls_last = case((TriageItem.sla_deadline.is_(None), 1), else_=0)

    try:
        with factory() as session:
            stmt = (
                select(
                    TriageItem.triage_id,
                    TriageItem.vendor_id,
                    TriageItem.triage_type,
                    TriageItem.question,
                    TriageItem.options,
                    TriageItem.sla_deadline,
                    TriageItem.created_at,
                )
                .where(TriageItem.user_id == user_id)
                .where(TriageItem.status == "PENDING")
            )
            if programme_id is not None:
                stmt = stmt.where(TriageItem.programme_id == programme_id)
            stmt = stmt.order_by(sla_nulls_last, TriageItem.sla_deadline.asc())

            rows = session.execute(stmt).fetchall()
            return [
                {
                    "triage_id": r.triage_id,
                    "vendor_id": r.vendor_id,
                    "triage_type": r.triage_type,
                    "question": r.question,
                    "options": r.options,
                    "sla_deadline": r.sla_deadline,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
    except Exception as exc:
        logger.warning("get_pending_triage: DB error: %s", exc)
        return []


def get_confirmed_vendors(programme_id: str) -> list[str]:
    """Return all vendor_ids for a programme (intake-confirmed = present in DB)."""
    factory = _get_session_factory()
    if factory is None:
        logger.warning("get_confirmed_vendors: DATABASE_URL not set — returning []")
        return []
    try:
        with factory() as session:
            rows = session.execute(
                select(VendorIntelligence.vendor_id).where(
                    VendorIntelligence.programme_id == programme_id,
                )
            ).fetchall()
            return [r.vendor_id for r in rows]
    except Exception as exc:
        logger.warning("get_confirmed_vendors: DB error: %s", exc)
        return []
