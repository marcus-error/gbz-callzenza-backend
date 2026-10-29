import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Tenant(Base):
    """Module 1 — Tenant model for multi-tenant SaaS architecture."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    subdomain: Mapped[str | None] = mapped_column(String(100), unique=True, index=True, nullable=True)
    custom_domain: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active", index=True)  # active, suspended, deleted
    
    # Server / Cluster Distribution
    cluster_id: Mapped[str] = mapped_column(String(50), default="default", server_default="default", index=True)  # e.g. default, us-east-1, eu-west-1
    server_url: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Ingress / node url for distributed clusters
    region: Mapped[str] = mapped_column(String(50), default="us-east", server_default="us-east")

    # Data Isolation Strategy (SHARED_DATABASE, SEPARATE_SCHEMA, SEPARATE_DATABASE)
    isolation_strategy: Mapped[str] = mapped_column(String(50), default="SHARED_DATABASE", server_default="SHARED_DATABASE")
    database_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Dedicated DB connection string if isolated
    schema_name: Mapped[str | None] = mapped_column(String(100), nullable=True)   # Dedicated PostgreSQL schema if schema-isolated

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
