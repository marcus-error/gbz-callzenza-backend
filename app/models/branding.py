import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TenantBranding(Base):
    """Module 7 — Tenant Branding model."""

    __tablename__ = "tenant_brandings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), default="")
    logo_url: Mapped[str] = mapped_column(String(500), default="")
    dark_logo_url: Mapped[str] = mapped_column(String(500), default="")
    favicon_url: Mapped[str] = mapped_column(String(500), default="")
    primary_color: Mapped[str] = mapped_column(String(30), default="#4F46E5")
    secondary_color: Mapped[str] = mapped_column(String(30), default="#06B6D4")
    accent_color: Mapped[str] = mapped_column(String(30), default="#10B981")
    custom_css: Mapped[Optional[str]] = mapped_column(Text, default="")
    support_email: Mapped[str] = mapped_column(String(255), default="")
    support_phone: Mapped[str] = mapped_column(String(50), default="")
    website: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
