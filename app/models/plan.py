import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Plan(Base):
    """Module 8 — Subscription Plan definition."""

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    annual_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    billing_interval: Mapped[str] = mapped_column(String(20), default="monthly")  # monthly, yearly
    max_agents: Mapped[int] = mapped_column(Integer, default=5)
    max_concurrent_calls: Mapped[int] = mapped_column(Integer, default=2)
    max_leads: Mapped[int] = mapped_column(Integer, default=1000)
    max_minutes_monthly: Mapped[int] = mapped_column(Integer, default=300)
    max_storage_mb: Mapped[int] = mapped_column(Integer, default=5000)
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    subscriptions: Mapped[list["Subscription"]] = relationship(
        "Subscription", back_populates="plan", lazy="selectin"
    )


class Subscription(Base):
    """Module 8 — Tenant Active Subscription."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE", index=True)  # ACTIVE, CANCELLED, PAST_DUE
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    start_date: Mapped[datetime] = mapped_column(DateTime, default=_now)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    payment_method_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    plan: Mapped[Plan] = relationship("Plan", back_populates="subscriptions", lazy="joined")
