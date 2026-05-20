"""SQLAlchemy 2.0 ORM models for Cobalt.

DB is scheduler and projection only — workspace files are the source of truth.
All values are written via sync_to_db() handlers; never written directly.

Table naming: PascalCase (matches SQL Server / SSMS convention).
Python attributes: snake_case; mapped to PascalCase DB column names via the
                   'name' parameter on mapped_column.

Connection: SQL Server via mssql+pyodbc (DATABASE_URL in .env).
"""

from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()


class Base(DeclarativeBase):
    pass


# ─── UserAccount ─────────────────────────────────────────────────────────────

class UserAccount(Base):
    """One row per user / organisation account.

    UserId (e.g. 'User001') is the top-level prefix in Azure Blob Storage,
    scoping every workspace file to its owner and enabling multi-tenancy.
    """

    __tablename__ = "UserAccount"

    user_id: Mapped[str] = mapped_column("UserId", String(50), primary_key=True)
    user_name: Mapped[str] = mapped_column("UserName", String(200), nullable=False)
    email: Mapped[str] = mapped_column("Email", String(255), nullable=False, unique=True)
    subscription_tier: Mapped[str] = mapped_column(
        "SubscriptionTier", String(50), default="STARTER", server_default="STARTER"
    )
    is_active: Mapped[bool] = mapped_column(
        "IsActive", Boolean, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        "CreatedAt", DateTime, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<UserAccount user_id={self.user_id!r}>"


# ─── ProgrammeRun ─────────────────────────────────────────────────────────────

class ProgrammeRun(Base):
    """One row per vendor intake run (programme).

    Tracks aggregate progress counters and run lifecycle status.
    Each programme belongs to one UserAccount.
    """

    __tablename__ = "ProgrammeRun"

    programme_id: Mapped[str] = mapped_column("ProgrammeId", String(50), primary_key=True)
    user_id: Mapped[str] = mapped_column("UserId", String(50), nullable=False)
    programme_name: Mapped[str | None] = mapped_column("ProgrammeName", String(200), nullable=True)
    status: Mapped[str | None] = mapped_column("Status", String(50), nullable=True)
    input_file: Mapped[str | None] = mapped_column("InputFile", String(500), nullable=True)

    total_vendors: Mapped[int] = mapped_column(
        "TotalVendors", Integer, default=0, server_default="0"
    )
    confirmed: Mapped[int] = mapped_column("Confirmed", Integer, default=0, server_default="0")
    triage: Mapped[int] = mapped_column("Triage", Integer, default=0, server_default="0")
    discarded: Mapped[int] = mapped_column("Discarded", Integer, default=0, server_default="0")
    blocked: Mapped[int] = mapped_column("Blocked", Integer, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        "CreatedAt", DateTime, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<ProgrammeRun programme_id={self.programme_id!r}>"


# ─── VendorIntelligence ───────────────────────────────────────────────────────

class VendorIntelligence(Base):
    """Central scheduling and projection table. One row per vendor per programme.

    The scheduler queries this table to answer "which vendors need work right now?"
    All columns are written by sync_to_db() handlers after workspace file writes —
    never by direct application code.

    Column groups:
      Identity   — set at P1 creation; InputName is immutable after creation
      Scheduling — updated by VW Agent via action_queue.md sync
      P1 class.  — set when entity.md is first written
      P2 enrich. — set when vendor_profile.md is written (null until P2 runs)
      P3 RS      — set when relationship_spend_profile.md is written (null until P3 runs)
      Audit      — CreatedAt is immutable; UpdatedAt is set on every sync
    """

    __tablename__ = "VendorIntelligence"

    # Identity
    vendor_id: Mapped[str] = mapped_column("VendorId", String(50), primary_key=True)
    programme_id: Mapped[str] = mapped_column("ProgrammeId", String(50), nullable=False)
    user_id: Mapped[str] = mapped_column("UserId", String(50), nullable=False)
    vendor_name: Mapped[str] = mapped_column("VendorName", String(500), nullable=False)
    # input_name is IMMUTABLE after creation — never overwrite
    input_name: Mapped[str] = mapped_column("InputName", String(500), nullable=False)

    # Scheduling
    status: Mapped[str] = mapped_column("Status", String(50), nullable=False)
    next_action_due: Mapped[datetime | None] = mapped_column(
        "NextActionDue", DateTime, nullable=True
    )
    reply_deadline: Mapped[datetime | None] = mapped_column(
        "ReplyDeadline", DateTime, nullable=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column("LastRunAt", DateTime, nullable=True)

    # Process 1 — Classification
    data_class: Mapped[str] = mapped_column(
        "DataClass", String(10), default="CLASS_D", server_default="CLASS_D"
    )
    identity_confidence: Mapped[float] = mapped_column(
        "IdentityConfidence", Numeric(4, 3), default=0.0, server_default="0.0"
    )
    category: Mapped[str | None] = mapped_column("Category", String(100), nullable=True)
    tier: Mapped[str | None] = mapped_column("Tier", String(10), nullable=True)
    pcs_score: Mapped[int] = mapped_column(
        "PcsScore", Integer, default=0, server_default="0"
    )
    vif_generated: Mapped[bool] = mapped_column(
        "VifGenerated", Boolean, default=False, server_default="0"
    )

    # Process 2 — Enrichment (null until P2 runs)
    profile_status: Mapped[str | None] = mapped_column(
        "ProfileStatus", String(50), nullable=True
    )
    last_enriched_at: Mapped[datetime | None] = mapped_column(
        "LastEnrichedAt", DateTime, nullable=True
    )
    subcategory: Mapped[str | None] = mapped_column("Subcategory", String(100), nullable=True)
    vendor_type: Mapped[str | None] = mapped_column("VendorType", String(50), nullable=True)
    hq_country: Mapped[str | None] = mapped_column("HqCountry", String(10), nullable=True)
    company_size_band: Mapped[str | None] = mapped_column(
        "CompanySizeBand", String(20), nullable=True
    )

    # Process 3 — Relationship & Spend (null until P3 runs)
    rs_last_updated: Mapped[datetime | None] = mapped_column(
        "RsLastUpdated", DateTime, nullable=True
    )
    spend_total_usd: Mapped[float | None] = mapped_column(
        "SpendTotalUsd", Numeric(18, 2), nullable=True
    )
    dependency_tier: Mapped[str | None] = mapped_column(
        "DependencyTier", String(20), nullable=True
    )
    relationship_type: Mapped[str | None] = mapped_column(
        "RelationshipType", String(50), nullable=True
    )

    # Process 4 — Analysis & Intelligence (null until P4 runs)
    cri_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    health_band: Mapped[str | None] = mapped_column(String(20), nullable=True)
    vendor_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_analysed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        "CreatedAt", DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        "UpdatedAt", DateTime, server_default=func.now()
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("pcs_score", 0)
        kwargs.setdefault("data_class", "CLASS_D")
        kwargs.setdefault("identity_confidence", 0.0)
        kwargs.setdefault("vif_generated", False)
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<VendorIntelligence vendor_id={self.vendor_id!r}>"


# ─── VendorCheckin ────────────────────────────────────────────────────────────

class VendorCheckin(Base):
    """Tracks each check-in campaign sent to a vendor.

    A row is inserted each time the VW Agent dispatches a check-in request
    asking the vendor to self-report spend and contract data.
    """

    __tablename__ = "VendorCheckin"

    checkin_id: Mapped[str] = mapped_column("CheckinId", String(50), primary_key=True)
    vendor_id: Mapped[str | None] = mapped_column("VendorId", String(50), nullable=True)
    programme_id: Mapped[str | None] = mapped_column("ProgrammeId", String(50), nullable=True)
    user_id: Mapped[str | None] = mapped_column("UserId", String(50), nullable=True)

    sent_at: Mapped[datetime | None] = mapped_column("SentAt", DateTime, nullable=True)
    reply_deadline: Mapped[datetime | None] = mapped_column(
        "ReplyDeadline", DateTime, nullable=True
    )
    responded_at: Mapped[datetime | None] = mapped_column("RespondedAt", DateTime, nullable=True)

    status: Mapped[str | None] = mapped_column("Status", String(50), nullable=True)

    def __repr__(self) -> str:
        return f"<VendorCheckin checkin_id={self.checkin_id!r}>"


# ─── TriageItem ───────────────────────────────────────────────────────────────

class TriageItem(Base):
    """Captures every situation where automation cannot decide and a human must intervene.

    Examples: identity conflict, document unreadable, profile assembly failed.
    Each item has an SLA deadline to prevent backlog accumulation.
    """

    __tablename__ = "TriageItem"

    triage_id: Mapped[str] = mapped_column("TriageId", String(50), primary_key=True)
    vendor_id: Mapped[str | None] = mapped_column("VendorId", String(50), nullable=True)
    programme_id: Mapped[str | None] = mapped_column("ProgrammeId", String(50), nullable=True)
    user_id: Mapped[str | None] = mapped_column("UserId", String(50), nullable=True)

    raw_input: Mapped[str | None] = mapped_column("RawInput", String(500), nullable=True)
    triage_type: Mapped[str | None] = mapped_column("TriageType", String(50), nullable=True)
    question: Mapped[str | None] = mapped_column("Question", Text, nullable=True)
    # options stored as JSON string — MSSQL has no native JSON column type
    options: Mapped[str | None] = mapped_column("Options", Text, nullable=True)

    status: Mapped[str] = mapped_column(
        "Status", String(50), default="PENDING", server_default="PENDING"
    )
    sla_deadline: Mapped[datetime | None] = mapped_column("SlaDeadline", DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column("ResolvedAt", DateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column("Resolution", Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        "CreatedAt", DateTime, server_default=func.now()
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("status", "PENDING")
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<TriageItem triage_id={self.triage_id!r}>"
