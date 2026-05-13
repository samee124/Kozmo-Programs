"""SQLAlchemy 2.0 ORM models for Cobalt.

DB is scheduler and projection only — workspace files are the source of truth.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class VendorIntelligence(Base):
    __tablename__ = "vendor_intelligence"

    vendor_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    programme_id: Mapped[str] = mapped_column(String(50), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(500), nullable=False)
    input_name: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    pcs_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tier: Mapped[str | None] = mapped_column(String(10), nullable=True)
    data_class: Mapped[str] = mapped_column(String(10), default="CLASS_D", server_default="CLASS_D")
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # V2 enrichment columns
    profile_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subcategory: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vendor_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    hq_country: Mapped[str | None] = mapped_column(String(10), nullable=True)
    company_size_band: Mapped[str | None] = mapped_column(String(20), nullable=True)
    identity_confidence: Mapped[float] = mapped_column(
        Numeric(4, 3), default=0.0, server_default="0.0"
    )
    next_action_due: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reply_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    vif_generated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __init__(self, **kwargs):
        kwargs.setdefault("pcs_score", 0)
        kwargs.setdefault("data_class", "CLASS_D")
        kwargs.setdefault("identity_confidence", 0.0)
        kwargs.setdefault("vif_generated", False)
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<VendorIntelligence vendor_id={self.vendor_id!r}>"


class ProgrammeRun(Base):
    __tablename__ = "programme_runs"

    programme_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    programme_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    input_file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    total_vendors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    triage: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    discarded: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    blocked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ProgrammeRun programme_id={self.programme_id!r}>"


class VendorCheckin(Base):
    __tablename__ = "vendor_checkins"

    checkin_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    vendor_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    programme_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reply_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)

    def __repr__(self) -> str:
        return f"<VendorCheckin checkin_id={self.checkin_id!r}>"


class TriageItem(Base):
    __tablename__ = "triage_items"

    def __init__(self, **kwargs):
        kwargs.setdefault("status", "PENDING")
        super().__init__(**kwargs)

    triage_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    vendor_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    programme_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    raw_input: Mapped[str | None] = mapped_column(String(500), nullable=True)
    triage_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="PENDING", server_default="PENDING")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    sla_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def __repr__(self) -> str:
        return f"<TriageItem triage_id={self.triage_id!r}>"
