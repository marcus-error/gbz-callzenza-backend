"""Call Recording Management Service for CallZenza.

Handles:
1. Campaign-associated recording retrieval and filtering.
2. 24-hour automatic recording expiry lifecycle.
3. Safe physical storage deletion & idempotency.
4. Retention policy enforcement (Original expires, verified Drive backups remain).
5. Audit logging for access, deletion, and cleanup.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from loguru import logger
from sqlalchemy import select, func, and_, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.models import Call, Campaign, AuditLog, Lead
from app.db.call_history_models import CallRecording, RecordingAccessAudit

# Base storage directories
BASE_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
RECORDINGS_DIR = BASE_STATIC_DIR / "recordings"
AUDIO_DIR = BASE_STATIC_DIR / "audio"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

EXPIRY_HOURS = 24
EXPIRING_SOON_HOURS = 2


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_local_audio_path(storage_key: str) -> Optional[Path]:
    """Resolves local absolute Path for a storage key or URL, with traversal prevention and on-demand synthesis."""
    if not storage_key:
        return None

    safe_fname = ""
    # Handle /recordings/xyz.wav
    if "/recordings/" in storage_key:
        fname = storage_key.split("/recordings/")[-1].strip()
        safe_fname = Path(fname).name
    # Handle /static/audio/xyz.mp3 or .wav
    elif "/audio/" in storage_key or "/static/audio/" in storage_key:
        fname = storage_key.split("/")[-1].strip()
        safe_fname = Path(fname).name
    else:
        safe_fname = Path(storage_key).name

    if not safe_fname:
        return None

    target = RECORDINGS_DIR / safe_fname
    if target.exists() and target.stat().st_size > 0:
        return target

    # Check root static/recordings/
    alt_target = BASE_STATIC_DIR.parent / "static" / "recordings" / safe_fname
    if alt_target.exists() and alt_target.stat().st_size > 0:
        return alt_target

    # Check audio dir
    audio_target = AUDIO_DIR / safe_fname
    if audio_target.exists() and audio_target.stat().st_size > 0:
        return audio_target

    # On-demand synthesis/fallback via recording_service if missing
    try:
        clean_id = safe_fname.replace("_rec.wav", "").replace(".wav", "").replace(".mp3", "")
        from app.services.recording_service import ensure_call_recording
        ensure_call_recording(clean_id)
        if target.exists() and target.stat().st_size > 0:
            return target
        if alt_target.exists() and alt_target.stat().st_size > 0:
            return alt_target
    except Exception as exc:
        logger.debug(f"[RECORDINGS] On-demand ensure_call_recording error: {exc}")

    return target



async def ensure_recording_for_call(
    db: AsyncSession,
    call: Call,
    recording_url: Optional[str] = None
) -> CallRecording:
    """
    Ensures a CallRecording record exists for a call, computing 24h expiry
    from call creation / initiation.
    """
    stmt = select(CallRecording).where(
        CallRecording.call_id == call.id,
        CallRecording.segment_index == 0
    ).order_by(CallRecording.created_at.desc())
    existing = (await db.execute(stmt)).scalars().first()

    url = recording_url or call.recording_url or f"/recordings/{call.id}_rec.wav"
    initiated_time = call.initiated_at or call.created_at or _now()
    if initiated_time.tzinfo is not None:
        initiated_time = initiated_time.astimezone(timezone.utc).replace(tzinfo=None)

    expiry_time = initiated_time + timedelta(hours=EXPIRY_HOURS)

    # Check file size if local
    local_path = resolve_local_audio_path(url)
    size_bytes = local_path.stat().st_size if local_path and local_path.exists() else None
    fmt = "mp3" if str(url).endswith(".mp3") else "wav"

    now_utc = _now()
    call_failed = str(call.status or "").upper().strip() in (
        "FAILED", "BUSY", "NO_ANSWER", "NO-ANSWER", "MISSED", "CANCELED", "CANCELLED", "REJECTED"
    )
    if call_failed:
        initial_status = "unavailable"
    elif local_path and local_path.exists() and (size_bytes or 0) == 0:
        initial_status = "unavailable"
    elif now_utc >= expiry_time:
        initial_status = "expired"
    elif expiry_time - now_utc <= timedelta(hours=EXPIRING_SOON_HOURS):
        initial_status = "expiring_soon"
    else:
        initial_status = "available"

    if existing:
        # Update missing fields additively
        modified = False
        if not existing.tenant_id and call.business_id:
            existing.tenant_id = call.business_id
            modified = True
        if not existing.campaign_id and call.campaign_id:
            existing.campaign_id = call.campaign_id
            modified = True
        if not existing.call_direction:
            existing.call_direction = call.direction or "OUTBOUND"
            modified = True
        if not existing.expires_at:
            existing.expires_at = expiry_time
            modified = True
        if not existing.recording_status or existing.recording_status in ("ready", "available"):
            if initial_status == "unavailable" and existing.recording_status != "unavailable":
                existing.recording_status = "unavailable"
                modified = True
            elif initial_status != existing.recording_status:
                existing.recording_status = initial_status
                modified = True
        if size_bytes and not existing.size_bytes:
            existing.size_bytes = size_bytes
            modified = True
        if modified:
            existing.updated_at = now_utc
            await db.commit()
            await db.refresh(existing)
        return existing

    rec = CallRecording(
        call_id=call.id,
        segment_index=0,
        source="local" if not url.startswith("http") else "provider",
        storage_provider="local" if not url.startswith("http") else "twilio",
        storage_key=url,
        format=fmt,
        channels=1,
        duration_ms=(call.duration_seconds or 0) * 1000,
        size_bytes=size_bytes,
        status="ready" if initial_status == "available" else "failed",
        tenant_id=call.business_id,
        campaign_id=call.campaign_id,
        call_direction=call.direction or "OUTBOUND",
        expires_at=expiry_time,
        recording_status=initial_status,
        backup_status="NOT_BACKED_UP",
        cleanup_status="pending",
        created_at=call.created_at or initiated_time,
        updated_at=now_utc,
    )
    db.add(rec)
    await db.commit()
    await db.refresh(rec)
    return rec


async def sync_all_call_recordings(db: AsyncSession) -> int:
    """
    Backfills and synchronizes calls into call_recordings table with
    tenant, campaign, direction, and 24-hour expiry metadata.
    """
    logger.info("[RECORDINGS] Synchronizing call recordings table...")
    stmt = select(Call).where(
        or_(
            and_(Call.recording_url.isnot(None), Call.recording_url != ""),
            Call.campaign_id.isnot(None),
            Call.status == "COMPLETED"
        )
    )
    calls = (await db.execute(stmt)).scalars().all()
    count = 0
    for call in calls:
        try:
            await ensure_recording_for_call(db, call)
            count += 1
        except Exception as e:
            logger.warning(f"[RECORDINGS] Failed to sync recording for call {call.id}: {e}")
    logger.info(f"[RECORDINGS] Synchronized {count} call recordings.")
    return count


async def cleanup_expired_recordings(db: AsyncSession) -> Dict[str, Any]:
    """
    Scans for recordings whose 24-hour retention period has passed.
    Enforces the strict expiry policy:
    1. Removes original audio files from disk.
    2. Marks recording_status = 'expired'.
    3. Retains any verified Google Drive backups untouched!
    4. Handles stuck uploads safely before deletion.
    """
    now_utc = _now()
    # Select recordings that have expired and are still marked available or expiring_soon
    stmt = select(CallRecording).where(
        CallRecording.expires_at.isnot(None),
        CallRecording.expires_at <= now_utc,
        CallRecording.recording_status.in_(["available", "expiring_soon", "ready"])
    )
    expired_records = (await db.execute(stmt)).scalars().all()

    cleaned_count = 0
    retained_backup_count = 0
    failed_cleanups = 0

    for rec in expired_records:
        try:
            # Check if an upload is actively in progress
            if rec.backup_status == "UPLOADING":
                # Check if upload has been running for > 15 minutes without completing
                time_since_update = (now_utc - (rec.updated_at or rec.created_at)).total_seconds()
                if time_since_update < 900:
                    # Give upload a small window to finalize
                    logger.info(f"[CLEANUP] Recording {rec.id} backup is currently uploading, deferring expiry...")
                    continue
                else:
                    # Timed out upload
                    rec.backup_status = "FAILED"
                    rec.backup_error = "Upload timed out before 24-hour expiry"

            # Remove physical file from disk
            local_path = resolve_local_audio_path(rec.storage_key)
            if local_path and local_path.exists():
                try:
                    os.remove(local_path)
                    logger.info(f"[CLEANUP] Deleted expired audio file: {local_path} (Recording {rec.id})")
                except Exception as del_err:
                    logger.error(f"[CLEANUP] Failed to remove audio file {local_path}: {del_err}")
                    failed_cleanups += 1

            # Update DB metadata
            rec.recording_status = "expired"
            rec.updated_at = now_utc

            if rec.backup_status == "BACKED_UP" and rec.google_drive_file_id:
                # Backup remains intact in Google Drive!
                rec.cleanup_status = "retained_backup"
                retained_backup_count += 1
            else:
                rec.cleanup_status = "cleaned"
                cleaned_count += 1

            # Audit entry
            db.add(AuditLog(
                business_id=rec.tenant_id or "system",
                user_id="system_cron",
                action="RECORDING_EXPIRED_CLEANUP",
                entity="call_recordings",
                entity_id=rec.id,
                details={
                    "call_id": rec.call_id,
                    "campaign_id": rec.campaign_id,
                    "backup_status": rec.backup_status,
                    "drive_file_id": rec.google_drive_file_id,
                    "retained_in_drive": (rec.backup_status == "BACKED_UP")
                }
            ))

        except Exception as err:
            logger.error(f"[CLEANUP] Error processing expired recording {rec.id}: {err}")
            failed_cleanups += 1

    # Also update recordings entering 'expiring_soon' window
    expiring_soon_stmt = select(CallRecording).where(
        CallRecording.expires_at.isnot(None),
        CallRecording.expires_at > now_utc,
        CallRecording.expires_at <= now_utc + timedelta(hours=EXPIRING_SOON_HOURS),
        CallRecording.recording_status == "available"
    )
    soon_records = (await db.execute(expiring_soon_stmt)).scalars().all()
    for s_rec in soon_records:
        s_rec.recording_status = "expiring_soon"
        s_rec.updated_at = now_utc

    await db.commit()

    return {
        "expired_processed": len(expired_records),
        "cleaned_count": cleaned_count,
        "retained_backup_count": retained_backup_count,
        "failed_cleanups": failed_cleanups,
        "expiring_soon_marked": len(soon_records),
        "timestamp": now_utc.isoformat(),
    }


async def delete_recording(
    db: AsyncSession,
    recording_id: str,
    tenant_id: str,
    user_id: str = "system"
) -> Dict[str, Any]:
    """
    Deletes original call recording permanently from storage.
    Enforces tenant permission, idempotency, and preserves any Google Drive backup.
    """
    stmt = select(CallRecording).where(
        CallRecording.id == recording_id,
        CallRecording.tenant_id == tenant_id
    )
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if not rec:
        raise ValueError("Recording not found or access denied")

    # If already deleted or expired, return idempotently
    if rec.recording_status == "deleted":
        return {
            "success": True,
            "message": "Recording was already deleted",
            "recording_id": rec.id,
            "backup_preserved": (rec.backup_status == "BACKED_UP")
        }

    # If backup is currently uploading, mark cancelled
    if rec.backup_status == "UPLOADING":
        rec.backup_status = "FAILED"
        rec.backup_error = "Original recording deleted while upload was in progress"

    # Safely delete local file
    local_path = resolve_local_audio_path(rec.storage_key)
    if local_path and local_path.exists():
        try:
            os.remove(local_path)
            logger.info(f"[DELETE] Removed recording file: {local_path}")
        except Exception as e:
            logger.warning(f"[DELETE] Could not delete audio file {local_path}: {e}")

    rec.recording_status = "deleted"
    rec.cleanup_status = "cleaned" if rec.backup_status != "BACKED_UP" else "retained_backup"
    rec.updated_at = _now()

    # Log audit
    db.add(AuditLog(
        business_id=tenant_id,
        user_id=user_id,
        action="CALL_RECORDING_DELETED",
        entity="call_recordings",
        entity_id=rec.id,
        details={
            "call_id": rec.call_id,
            "campaign_id": rec.campaign_id,
            "had_backup": (rec.backup_status == "BACKED_UP"),
            "drive_file_id": rec.google_drive_file_id,
        }
    ))

    await db.commit()
    await db.refresh(rec)

    return {
        "success": True,
        "message": "Call recording removed successfully",
        "recording_id": rec.id,
        "recording_status": rec.recording_status,
        "backup_status": rec.backup_status,
        "backup_preserved": (rec.backup_status == "BACKED_UP"),
        "google_drive_file_url": rec.google_drive_file_url if rec.backup_status == "BACKED_UP" else None,
    }


# Background loop runner
_cleanup_task: Optional[asyncio.Task] = None


async def _cleanup_periodic_loop(interval_seconds: int = 600):
    """Periodic background loop that executes cleanup every N seconds."""
    logger.info(f"🚀 [RECORDINGS] Background 24h recording expiry monitor started (interval: {interval_seconds}s)")
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            async with AsyncSessionLocal() as session:
                result = await cleanup_expired_recordings(session)
                if result["expired_processed"] > 0 or result["expiring_soon_marked"] > 0:
                    logger.info(f"[RECORDINGS] Periodic expiry cycle complete: {result}")
        except asyncio.CancelledError:
            logger.info("[RECORDINGS] Expiry worker loop cancelled.")
            break
        except Exception as e:
            logger.error(f"[RECORDINGS] Unexpected error in expiry worker loop: {e}")
            await asyncio.sleep(30)


def start_expiry_cleanup_worker():
    """Starts the asyncio background task for automatic recording expiry."""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_periodic_loop(interval_seconds=600))


def stop_expiry_cleanup_worker():
    """Stops the asyncio background task."""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
