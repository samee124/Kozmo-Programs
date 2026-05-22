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
from cobalt.db.models import ActionPlan, Communication, PlanTask, ProgrammeRun, VendorIntelligence
from cobalt.db.queries import insert_vendor

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
    """vendor_register.md written → update ProgrammeRun confirmed count.

    vendor_register.md only contains confirmed vendors (the vendors list).
    Derive confirmed count from len(vendors).
    """
    programme_id = data.get("programme_id")
    if not programme_id:
        return
    vendors = data.get("vendors") or []
    session.execute(
        update(ProgrammeRun)
        .where(ProgrammeRun.programme_id == programme_id)
        .values(confirmed=len(vendors))
    )


def _sync_deduplication_report(session: Session, data: dict, vendor_id: str) -> None:
    """deduplication_report.md written → update all ProgrammeRun counters.

    This file has total_input, confirmed, triage, discarded, blocked.
    """
    programme_id = data.get("programme_id")
    if not programme_id:
        return
    values: dict = {}
    if data.get("total_input") is not None:
        values["total_vendors"] = int(data["total_input"])
    if data.get("confirmed") is not None:
        values["confirmed"] = int(data["confirmed"])
    if data.get("triage") is not None:
        values["triage"] = int(data["triage"])
    if data.get("discarded") is not None:
        values["discarded"] = int(data["discarded"])
    if data.get("blocked") is not None:
        values["blocked"] = int(data["blocked"])
    if values:
        session.execute(
            update(ProgrammeRun)
            .where(ProgrammeRun.programme_id == programme_id)
            .values(**values)
        )


def _sync_run_log(session: Session, data: dict, vendor_id: str) -> None:
    """run_log.md written → update ProgrammeRun.Status and LastRunAt."""
    programme_id = data.get("programme_id")
    status = data.get("status")
    if not programme_id or not status:
        return
    session.execute(
        update(ProgrammeRun)
        .where(ProgrammeRun.programme_id == programme_id)
        .values(status=status, last_run_at=datetime.utcnow())
    )


def _sync_triage_queue(session: Session, data: dict, vendor_id: str) -> None:
    """triage_queue.md written → update ProgrammeRun.Triage counter.

    Supports both triage_count (integer) and triage_items (list) keys.
    The orchestrator writes triage_items; triage_count is kept for compatibility.
    """
    programme_id = data.get("programme_id")
    triage_count = data.get("triage_count")
    if triage_count is None:
        items = data.get("triage_items")
        if isinstance(items, list):
            triage_count = len(items)
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


def _sync_single_vendor_file(session: Session, data: dict, vendor_id: str, programme_id: str) -> None:
    """Single-file vendor .md written → ensure row exists, then update all known columns.

    Single-file arch writes {slug}.md at the vendor root. This handler fires for
    any .md file whose name is not in the named-file registry, ensuring the
    VendorIntelligence row is created (idempotent) before the UPDATE runs.

    Nested-field paths in the single-file MD schema:
      canonical_name              → vendor_name
      intake.input_name           → input_name
      intake.confidence           → identity_confidence
      intake.data_class           → data_class
      classification.category.value     → category
      classification.subcategory.value  → subcategory
      classification.vendor_type.value  → vendor_type
      identity.hq_country.value         → hq_country
      size.company_size_band.value      → company_size_band
    """
    intake = data.get("intake") or {}
    clf    = data.get("classification") or {}
    iden   = data.get("identity") or {}
    size   = data.get("size") or {}
    # Nested P2 / P3 sections in the consolidated profile
    enr    = data.get("enrichment") or {}
    rel    = data.get("relationship") or {}
    enr_iden = enr.get("identity") or {}
    enr_size = enr.get("size") or {}
    enr_clf  = enr.get("classification") or {}

    vendor_name = (
        data.get("canonical_name")
        or data.get("vendor_name")
        or intake.get("input_name")
        or vendor_id
    )
    input_name = intake.get("input_name") or data.get("input_name") or vendor_id
    data_class  = intake.get("data_class") or data.get("data_class") or "CLASS_D"
    conf_raw    = intake.get("confidence") if intake.get("confidence") is not None else data.get("identity_confidence")
    id_conf     = float(conf_raw) if conf_raw is not None else 0.0

    insert_vendor(
        vendor_id=vendor_id,
        programme_id=programme_id,
        vendor_name=vendor_name,
        input_name=input_name,
        user_id=data.get("user_id") or os.getenv("COBALT_USER_ID"),
        data_class=data_class,
        identity_confidence=id_conf,
    )

    values: dict = {"updated_at": datetime.utcnow()}
    _maybe = lambda k, v: values.update({k: v}) if v is not None else None

    _maybe("vendor_name",         vendor_name)
    _maybe("data_class",          data_class)
    _maybe("identity_confidence", id_conf)
    _maybe("category",           (clf.get("category") or {}).get("value")
                                  or (enr_clf.get("category") or {}).get("value")
                                  or data.get("category"))
    _maybe("subcategory",        (clf.get("subcategory") or {}).get("value")
                                  or (enr_clf.get("subcategory") or {}).get("value")
                                  or data.get("subcategory"))
    _maybe("vendor_type",        (clf.get("vendor_type") or {}).get("value")
                                  or (enr_clf.get("vendor_type") or {}).get("value")
                                  or data.get("vendor_type"))
    _maybe("hq_country",         (iden.get("hq_country") or {}).get("value")
                                  or (enr_iden.get("hq_country") or {}).get("value")
                                  or data.get("hq_country"))
    _maybe("company_size_band",  (size.get("company_size_band") or {}).get("value")
                                  or (enr_size.get("company_size_band") or {}).get("value")
                                  or data.get("company_size_band"))
    _maybe("profile_status",     enr.get("profile_status") or data.get("profile_status"))
    _maybe("last_enriched_at",   _parse_datetime(enr.get("enriched_at")
                                  or data.get("enriched_at") or data.get("last_enriched_at")))
    _maybe("status",             data.get("status") or data.get("intake_status"))
    _maybe("spend_total_usd",    rel.get("spend_total_ttm_usd") or data.get("spend_total_ttm_usd"))
    _maybe("dependency_tier",    rel.get("dependency_tier") or data.get("dependency_tier"))
    _maybe("relationship_type",  rel.get("relationship_type") or data.get("relationship_type"))
    _maybe("rs_last_updated",    _parse_datetime(rel.get("last_updated") or data.get("rs_last_updated")))

    session.execute(
        update(VendorIntelligence)
        .where(VendorIntelligence.vendor_id == vendor_id)
        .where(VendorIntelligence.programme_id == programme_id)
        .values(**values)
    )


def _sync_action_plan(session: Session, data: dict, vendor_id: str) -> None:
    """action_plan.md written → upsert ActionPlan row."""
    plan_id = data.get("plan_id")
    if not plan_id:
        return

    from cobalt.db.queries import upsert_action_plan
    upsert_action_plan(
        session=session,
        plan_id=plan_id,
        vendor_id=vendor_id or "",
        programme_id=data.get("programme_id"),
        plan_title=data.get("plan_title"),
        plan_objective=data.get("plan_objective"),
        selected_playbook=data.get("selected_playbook"),
        playbook_rationale=data.get("playbook_rationale"),
        confidence=data.get("confidence"),
        step_count=len(data.get("steps") or []),
        status="ACTIVE",
    )


def _sync_task_list(session: Session, data: dict, vendor_id: str) -> None:
    """task_list.md written → upsert one PlanTask row per task in the list."""
    tasks = data.get("tasks") or []
    if not tasks:
        return

    from cobalt.db.queries import upsert_plan_task
    for task_data in tasks:
        task_id = task_data.get("task_id")
        if not task_id:
            continue
        upsert_plan_task(
            session=session,
            task_id=task_id,
            plan_id=task_data.get("plan_id"),
            vendor_id=vendor_id,
            programme_id=task_data.get("programme_id"),
            title=task_data.get("title"),
            owner=task_data.get("owner"),
            task_type=task_data.get("task_type"),
            priority=task_data.get("priority"),
            status=task_data.get("status", "PENDING"),
            due_date=_parse_datetime(task_data.get("due_date")),
            approval_required=bool(task_data.get("approval_required", False)),
            blocked_flag=bool(task_data.get("blocked_flag", False)),
        )


def _sync_execution_state(session: Session, data: dict, vendor_id: str) -> None:
    """execution_state.md written → update PlanTask statuses for overdue/blocked items."""
    tasks = data.get("task_statuses") or []
    if not tasks:
        return

    for task_data in tasks:
        task_id = task_data.get("task_id")
        if not task_id:
            continue
        session.execute(
            update(PlanTask)
            .where(PlanTask.task_id == task_id)
            .values(
                status=task_data.get("status"),
                blocked_flag=bool(task_data.get("blocked_flag", False)),
                updated_at=datetime.utcnow(),
            )
        )


# ─── Handler registry ─────────────────────────────────────────────────────────

_HANDLERS = {
    "entity.md":                        _sync_entity,
    "coverage.md":                      _sync_coverage,
    "action_queue.md":                  _sync_action_queue,
    "programme_plan.md":                _sync_programme_plan,
    "vendor_register.md":               _sync_vendor_register,
    "deduplication_report.md":          _sync_deduplication_report,
    "run_log.md":                       _sync_run_log,
    "triage_queue.md":                  _sync_triage_queue,
    "analysis_result.md":               _sync_analysis_result,
    "action_plan.md":                   _sync_action_plan,
    "task_list.md":                     _sync_task_list,
    "execution_state.md":               _sync_execution_state,
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
    is_single_vendor_file = (
        handler is None
        and vendor_id is not None
        and programme_id is not None
        and path.suffix == ".md"
    )
    if handler is None and not is_single_vendor_file:
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
            if is_single_vendor_file:
                if not (data.get("canonical_name") or data.get("intake") or data.get("vendor_name")):
                    logger.debug("sync_to_db: no vendor fields in %s — skipping", path.name)
                    return
                _sync_single_vendor_file(session, data, vendor_id, programme_id)
            else:
                handler(session, data, vendor_id)
            session.commit()
    except Exception as exc:
        logger.warning("sync_to_db: DB error for %s: %s", path, exc)
