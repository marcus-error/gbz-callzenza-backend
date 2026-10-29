import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TenantSettings(Base):
    """Module 12 — Per-tenant configuration and operational settings."""

    __tablename__ = "tenant_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), default="UTC")
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    date_format: Mapped[str] = mapped_column(String(20), default="YYYY-MM-DD")
    calling_hours_start: Mapped[str] = mapped_column(String(10), default="09:00")
    calling_hours_end: Mapped[str] = mapped_column(String(10), default="18:00")
    retry_limit: Mapped[int] = mapped_column(Integer, default=3)
    retry_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    voicemail_detection: Mapped[bool] = mapped_column(Boolean, default=True)
    custom_flags: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
