"""
Call History v2 — Database Models for Call Recordings and Access Audits.
Additive only. No ALTER on existing tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CallRecording(Base):
    __tablename__ = "call_recordings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    segment_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="provider")  # provider | capture | local
    storage_provider: Mapped[str] = mapped_column(String(50), default="local")  # twilio | local | s3
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    format: Mapped[str] = mapped_column(String(20), default="wav")
    channels: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="ready")  # processing | ready | failed | not_recorded
    # Campaign and Tenant isolation
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), index=True, nullable=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String(36), index=True, nullable=True)
    call_direction: Mapped[str] = mapped_column(String(20), default="OUTBOUND")

    # 24-hour expiry lifecycle
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    recording_status: Mapped[str] = mapped_column(String(30), default="available", index=True)  # available | expiring_soon | expired | deleted

    # Google Drive export and backup status
    backup_status: Mapped[str] = mapped_column(String(30), default="NOT_BACKED_UP", index=True)  # NOT_BACKED_UP | QUEUED | UPLOADING | BACKED_UP | FAILED | EXPIRED
    google_drive_file_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    google_drive_folder_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    google_drive_file_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    backup_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    backup_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    cleanup_status: Mapped[str] = mapped_column(String(30), default="pending")  # pending | cleaned | retained_backup | failed

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    @property
    def original_storage_key(self) -> str:
        return self.storage_key

    @original_storage_key.setter
    def original_storage_key(self, val: str):
        self.storage_key = val

    @property
    def recording_format(self) -> str:
        return self.format

    @property
    def recording_size(self) -> Optional[int]:
        return self.size_bytes

    @property
    def recording_created_at(self) -> datetime:
        return self.created_at

    __table_args__ = (
        Index("ix_call_recordings_call_segment", "call_id", "segment_index"),
        Index("ix_call_recordings_campaign_created", "campaign_id", "created_at"),
        Index("ix_call_recordings_tenant_campaign", "tenant_id", "campaign_id"),
        Index("ix_call_recordings_expiry_status", "expires_at", "recording_status"),
    )


class RecordingAccessAudit(Base):
    __tablename__ = "recording_access_audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # stream | download
    ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
