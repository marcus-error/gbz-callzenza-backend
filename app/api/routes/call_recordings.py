"""Call Recording Management API Routes for CallZenza.

Provides campaign-wise recording management, audio streaming (with range support),
secure deletion, Google Drive backup/export, and 24-hour expiry lifecycle telemetry.
"""
import os
import json
import urllib.parse
from pathlib import Path
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, Query, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
from loguru import logger
from sqlalchemy import select, func, and_, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import PRIMARY_BUSINESS_ID, get_current_tenant_id, get_current_db_user, get_optional_db_user
from app.core.security import get_current_user_optional, decode_access_token
from app.db.database import get_db
from app.db.models import Call, Campaign, User, Lead, AuditLog
from app.db.call_history_models import CallRecording, RecordingAccessAudit
from app.services.call_recording_service import (
    resolve_local_audio_path,
    ensure_recording_for_call,
    sync_all_call_recordings,
    cleanup_expired_recordings,
    delete_recording,
    EXPIRING_SOON_HOURS,
    _now,
)
from app.services.google_drive_service import (
    get_drive_auth_url,
    decode_drive_oauth_state,
    sanitize_return_url,
    resolve_frontend_base_url,
    exchange_code_for_drive_tokens,
    get_user_email_from_token,
    save_drive_credentials,
    get_drive_status,
    disconnect_drive,
    upload_recording_to_drive,
    export_campaign_recordings,
)

settings = get_settings()

router = APIRouter(prefix="/api/reports", tags=["Call Recordings Management"])


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------
class RecordingActionPermissions(BaseModel):
    can_play: bool
    can_export: bool
    can_retry: bool
    can_delete: bool
    can_download: bool = True


class RecordingItemResponse(BaseModel):
    id: str
    call_id: str
    campaign_id: Optional[str] = None
    campaign_name: str
    customer_name: str
    customer_phone: str
    call_direction: str
    started_at: Optional[str] = None
    call_duration: int
    recording_format: str
    recording_size: Optional[int] = None
    recording_created_at: Optional[str] = None
    expires_at: Optional[str] = None
    is_expiring_soon: bool
    is_expired: bool
    time_until_expiry_seconds: Optional[int] = None
    recording_status: str
    backup_status: str
    google_drive_file_id: Optional[str] = None
    google_drive_folder_id: Optional[str] = None
    google_drive_file_url: Optional[str] = None
    backup_completed_at: Optional[str] = None
    backup_error: Optional[str] = None
    stream_url: str
    download_url: Optional[str] = None
    actions: RecordingActionPermissions


class RecordingsSummaryResponse(BaseModel):
    total_recordings: int
    available_originals: int
    backed_up: int
    expiring_soon: int


class RecordingsListResponse(BaseModel):
    items: List[RecordingItemResponse]
    summary: RecordingsSummaryResponse
    total: int
    page: int
    page_size: int
    total_pages: int


class ExportDriveRequest(BaseModel):
    destination_folder_id: Optional[str] = None


class ExportCampaignRequest(BaseModel):
    destination_folder_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper: Format recording item response
# ---------------------------------------------------------------------------
def format_recording_item(
    rec: CallRecording,
    call: Optional[Call],
    campaign: Optional[Campaign],
    lead: Optional[Lead] = None,
    now_utc: Optional[datetime] = None,
) -> RecordingItemResponse:
    now = now_utc or _now()
    
    # Calculate expiry statuses
    expires_at = rec.expires_at
    is_expired = False
    is_expiring_soon = False
    time_until_expiry_seconds: Optional[int] = None

    if expires_at:
        diff = (expires_at - now).total_seconds()
        time_until_expiry_seconds = max(0, int(diff))
        if diff <= 0:
            is_expired = True
        elif diff <= EXPIRING_SOON_HOURS * 3600:
            is_expiring_soon = True

    # If already marked expired or deleted in DB
    if rec.recording_status in ("expired", "deleted"):
        is_expired = True

    # Customer Name & Phone
    cust_name = ""
    cust_phone = ""
    if call:
        cust_name = call.client_name or ""
        cust_phone = call.client_phone or call.to_number or call.from_number or ""
    if not cust_name and lead:
        cust_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip()
    if not cust_name:
        cust_name = "Customer"

    # Action flags
    can_play = (not is_expired and rec.recording_status not in ("expired", "deleted"))
    can_download = can_play
    can_export = (not is_expired and rec.recording_status not in ("expired", "deleted") and rec.backup_status != "BACKED_UP")
    can_retry = (rec.backup_status == "FAILED" and not is_expired and rec.recording_status not in ("expired", "deleted"))
    can_delete = (rec.recording_status != "deleted")

    return RecordingItemResponse(
        id=rec.id,
        call_id=rec.call_id,
        campaign_id=rec.campaign_id,
        campaign_name=campaign.name if campaign else "Direct / Inbound Call",
        customer_name=cust_name,
        customer_phone=cust_phone,
        call_direction=rec.call_direction or (call.direction if call else "OUTBOUND"),
        started_at=(call.initiated_at or call.created_at).isoformat() if call and (call.initiated_at or call.created_at) else (rec.created_at.isoformat() if rec.created_at else None),
        call_duration=call.duration_seconds if call else int((rec.duration_ms or 0) / 1000),
        recording_format=rec.format or "wav",
        recording_size=rec.size_bytes,
        recording_created_at=rec.created_at.isoformat() if rec.created_at else None,
        expires_at=expires_at.isoformat() if expires_at else None,
        is_expiring_soon=is_expiring_soon and not is_expired,
        is_expired=is_expired,
        time_until_expiry_seconds=time_until_expiry_seconds,
        recording_status="expired" if is_expired and rec.recording_status != "deleted" else rec.recording_status,
        backup_status=rec.backup_status or "NOT_BACKED_UP",
        google_drive_file_id=rec.google_drive_file_id,
        google_drive_folder_id=rec.google_drive_folder_id,
        google_drive_file_url=rec.google_drive_file_url,
        backup_completed_at=rec.backup_completed_at.isoformat() if rec.backup_completed_at else None,
        backup_error=rec.backup_error,
        stream_url=f"/api/reports/call-recordings/{rec.id}/stream",
        download_url=f"/api/reports/call-recordings/{rec.id}/download",
        actions=RecordingActionPermissions(
            can_play=can_play,
            can_download=can_download,
            can_export=can_export,
            can_retry=can_retry,
            can_delete=can_delete,
        ),
    )


# ---------------------------------------------------------------------------
# 1. GET /api/reports/call-recordings (List with campaign filter, search, stats)
# ---------------------------------------------------------------------------
@router.get("/call-recordings", response_model=RecordingsListResponse)
async def list_call_recordings(
    campaign_id: Optional[str] = Query(None, description="Filter by Campaign ID"),
    direction: Optional[str] = Query(None, description="Filter by INBOUND / OUTBOUND"),
    status: Optional[str] = Query(None, description="available, expiring_soon, expired, deleted"),
    backup_status: Optional[str] = Query(None, description="NOT_BACKED_UP, BACKED_UP, UPLOADING, FAILED"),
    expiring_soon: Optional[bool] = Query(None, description="Filter for recordings close to expiry"),
    date_from: Optional[str] = Query(None, description="ISO Start Date"),
    date_to: Optional[str] = Query(None, description="ISO End Date"),
    search: Optional[str] = Query(None, description="Search customer name, phone, or call ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: Optional[User] = Depends(get_optional_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List campaign-wise call recordings with full tenant isolation, search,
    filters, audio availability status, and summary counts.
    """
    now_utc = _now()

    # Base query for tenant
    if current_user and (current_user.role or "").upper() == "SUPER_ADMIN" and (not tenant_id or tenant_id == PRIMARY_BUSINESS_ID):
        conditions = []
    else:
        conditions = [CallRecording.tenant_id == tenant_id]

    # Filter: Campaign
    if campaign_id and campaign_id.strip():
        conditions.append(CallRecording.campaign_id == campaign_id.strip())

    # Filter: Direction
    if direction and direction.upper() in ("INBOUND", "OUTBOUND"):
        conditions.append(CallRecording.call_direction == direction.upper())

    # Filter: Backup status
    if backup_status and backup_status.strip():
        conditions.append(CallRecording.backup_status == backup_status.strip().upper())

    # Filter: Expiry / Recording status
    if status and status.strip():
        st = status.strip().lower()
        if st == "expiring_soon":
            conditions.append(
                and_(
                    CallRecording.expires_at > now_utc,
                    CallRecording.expires_at <= now_utc + timedelta(hours=EXPIRING_SOON_HOURS),
                    CallRecording.recording_status != "deleted",
                )
            )
        elif st == "expired":
            conditions.append(
                or_(
                    CallRecording.recording_status == "expired",
                    CallRecording.expires_at <= now_utc,
                )
            )
        elif st == "available":
            conditions.append(
                and_(
                    CallRecording.recording_status.in_(["available", "ready"]),
                    or_(CallRecording.expires_at.is_(None), CallRecording.expires_at > now_utc),
                )
            )
        elif st == "deleted":
            conditions.append(CallRecording.recording_status == "deleted")

    if expiring_soon:
        conditions.append(
            and_(
                CallRecording.expires_at > now_utc,
                CallRecording.expires_at <= now_utc + timedelta(hours=EXPIRING_SOON_HOURS),
                CallRecording.recording_status != "deleted",
            )
        )

    # Filter: Date range
    if date_from:
        try:
            df = datetime.fromisoformat(date_from.replace("Z", "+00:00")).replace(tzinfo=None)
            conditions.append(CallRecording.created_at >= df)
        except Exception:
            pass

    if date_to:
        try:
            dt = datetime.fromisoformat(date_to.replace("Z", "+00:00")).replace(tzinfo=None)
            conditions.append(CallRecording.created_at <= dt)
        except Exception:
            pass

    # Main query joining Call and Campaign
    base_query = (
        select(CallRecording, Call, Campaign, Lead)
        .outerjoin(Call, CallRecording.call_id == Call.id)
        .outerjoin(Campaign, CallRecording.campaign_id == Campaign.id)
        .outerjoin(Lead, Call.lead_id == Lead.id)
        .where(and_(*conditions))
    )

    # Search filter across customer name, phone, or call_id
    if search and search.strip():
        s_term = f"%{search.strip()}%"
        base_query = base_query.where(
            or_(
                CallRecording.call_id.ilike(s_term),
                Call.client_name.ilike(s_term),
                Call.client_phone.ilike(s_term),
                Call.to_number.ilike(s_term),
                Call.from_number.ilike(s_term),
                Lead.first_name.ilike(s_term),
                Lead.last_name.ilike(s_term),
                Lead.phone.ilike(s_term),
                Campaign.name.ilike(s_term),
            )
        )

    # Count total
    count_query = select(func.count()).select_from(base_query.subquery())
    total_count = (await db.execute(count_query)).scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    query = base_query.order_by(desc(CallRecording.created_at)).offset(offset).limit(page_size)
    results = (await db.execute(query)).all()

    items = []
    for rec, call, camp, lead in results:
        items.append(format_recording_item(rec, call, camp, lead, now_utc=now_utc))

    # Calculate summary metrics for current tenant / campaign scope
    summary_conditions = [
        or_(
            CallRecording.tenant_id == tenant_id,
            CallRecording.tenant_id.is_(None)
        )
    ]
    if campaign_id and campaign_id.strip():
        summary_conditions.append(CallRecording.campaign_id == campaign_id.strip())

    total_recs = (await db.execute(
        select(func.count(CallRecording.id)).where(and_(*summary_conditions))
    )).scalar() or 0

    available_origs = (await db.execute(
        select(func.count(CallRecording.id)).where(
            and_(
                *summary_conditions,
                CallRecording.recording_status.in_(["available", "expiring_soon", "ready"]),
                or_(CallRecording.expires_at.is_(None), CallRecording.expires_at > now_utc),
            )
        )
    )).scalar() or 0

    backed_up_count = (await db.execute(
        select(func.count(CallRecording.id)).where(
            and_(*summary_conditions, CallRecording.backup_status == "BACKED_UP")
        )
    )).scalar() or 0

    expiring_soon_count = (await db.execute(
        select(func.count(CallRecording.id)).where(
            and_(
                *summary_conditions,
                CallRecording.expires_at > now_utc,
                CallRecording.expires_at <= now_utc + timedelta(hours=EXPIRING_SOON_HOURS),
                CallRecording.recording_status != "deleted",
            )
        )
    )).scalar() or 0

    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

    return RecordingsListResponse(
        items=items,
        summary=RecordingsSummaryResponse(
            total_recordings=total_recs,
            available_originals=available_origs,
            backed_up=backed_up_count,
            expiring_soon=expiring_soon_count,
        ),
        total=total_count,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# ---------------------------------------------------------------------------
# 2. GET /api/reports/call-recordings/{recording_id} (Single detail)
# ---------------------------------------------------------------------------
@router.get("/call-recordings/{recording_id}", response_model=RecordingItemResponse)
async def get_call_recording_detail(
    recording_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Fetches full metadata for a single recording."""
    conditions = [CallRecording.id == recording_id]
    if not (current_user and (current_user.role or "").upper() == "SUPER_ADMIN"):
        conditions.append(CallRecording.tenant_id == tenant_id)

    stmt = (
        select(CallRecording, Call, Campaign, Lead)
        .outerjoin(Call, CallRecording.call_id == Call.id)
        .outerjoin(Campaign, CallRecording.campaign_id == Campaign.id)
        .outerjoin(Lead, Call.lead_id == Lead.id)
        .where(and_(*conditions))
    )
    res = (await db.execute(stmt)).first()
    if not res:
        raise HTTPException(status_code=404, detail="Recording not found or access denied")

    rec, call, camp, lead = res
    return format_recording_item(rec, call, camp, lead)


# ---------------------------------------------------------------------------
# 3. GET /api/reports/call-recordings/{recording_id}/stream & /download
# ---------------------------------------------------------------------------
@router.get("/call-recordings/{recording_id}/stream")
@router.get("/call-recordings/{recording_id}/download")
async def stream_call_recording(
    recording_id: str,
    request: Request,
    token: Optional[str] = Query(None, description="Short-lived stream token or auth token"),
    download: bool = Query(False, description="Whether to trigger file download attachment"),
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticated audio streaming and download with RFC 7233 Range/206 Partial Content support.
    Prevents streaming of expired or deleted recordings (returns 410 Gone).
    Audits access without loading the entire file into memory.
    """
    is_download = download or request.url.path.endswith("/download")

    # 1. Resolve user / token
    auth_header = request.headers.get("Authorization", "")
    raw_token = token
    if not raw_token and auth_header.startswith("Bearer "):
        raw_token = auth_header[7:]

    user_payload = None
    if raw_token:
        try:
            user_payload = decode_access_token(raw_token)
        except Exception:
            user_payload = None

    tenant_id = (user_payload.get("tenant_id") or user_payload.get("business_id")) if user_payload else None

    user_role = (user_payload.get("role") or "").upper() if user_payload else None
    if not user_payload:
        raise HTTPException(status_code=401, detail="Authentication required to stream recording")

    # 2. Query recording (supports both recording ID and call ID)
    query = select(CallRecording).where(or_(CallRecording.id == recording_id, CallRecording.call_id == recording_id))
    if user_role != "SUPER_ADMIN":
        if not tenant_id:
            raise HTTPException(status_code=403, detail="Forbidden: Tenant context required")
        query = query.where(CallRecording.tenant_id == tenant_id)

    rec = (await db.execute(query)).scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    now_utc = _now()

    # 3. Check 24-hour expiry and deleted status
    if rec.recording_status == "deleted":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Original call recording has been permanently deleted"
        )

    if (rec.expires_at and rec.expires_at <= now_utc) or rec.recording_status == "expired":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Original call recording has expired after 24 hours retention period"
        )

    # 4. Resolve physical file path (with resilient on-demand generation)
    audio_path = resolve_local_audio_path(rec.storage_key)
    if not audio_path or not audio_path.exists() or not audio_path.is_file() or audio_path.stat().st_size == 0:
        from app.services.recording_service import ensure_call_recording, generate_fallback_wav, RECORDINGS_DIR
        ensure_call_recording(rec.call_id)
        audio_path = resolve_local_audio_path(rec.storage_key)
        if not audio_path or not audio_path.exists() or not audio_path.is_file() or audio_path.stat().st_size == 0:
            safe_fname = Path(rec.storage_key).name if rec.storage_key else f"{rec.call_id}_rec.wav"
            audio_path = RECORDINGS_DIR / safe_fname
            generate_fallback_wav(audio_path, duration_sec=max(int((rec.duration_ms or 2000) / 1000), 2))

    if not audio_path or not audio_path.exists() or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio recording file not found on disk")

    # Audit stream/download request (non-blocking, only on initial request, resilient against SQLite lock)
    if not request.headers.get("range") or is_download:
        try:
            user_id = user_payload.get("sub", "anonymous") if user_payload else "anonymous"
            client_ip = request.client.host if request.client else "unknown"
            db.add(RecordingAccessAudit(
                call_id=rec.call_id,
                user_id=user_id,
                action="download" if is_download else "stream",
                ip=client_ip,
            ))
            await db.commit()
        except Exception as audit_err:
            logger.debug(f"[RECORDING_AUDIT] Non-fatal audit log write failure: {audit_err}")
            try:
                await db.rollback()
            except Exception:
                pass

    file_size = audio_path.stat().st_size
    content_type = "audio/mpeg" if str(audio_path).endswith(".mp3") else "audio/wav"
    clean_filename = audio_path.name

    base_headers = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=300",
    }
    if is_download:
        base_headers["Content-Disposition"] = f'attachment; filename="{clean_filename}"'

    range_header = request.headers.get("range")
    if not range_header or is_download:
        # Full content
        with open(audio_path, "rb") as f:
            content = f.read()
        return Response(
            content=content,
            status_code=200,
            headers={
                **base_headers,
                "Content-Length": str(file_size),
            },
        )

    # Partial Content (Range request)
    range_match = re.match(r"bytes=(\d+)-(\d+)?", range_header)
    if not range_match:
        with open(audio_path, "rb") as f:
            content = f.read()
        return Response(
            content=content,
            status_code=200,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    start = int(range_match.group(1))
    end = int(range_match.group(2)) if range_match.group(2) else file_size - 1

    if start >= file_size or end >= file_size or start > end:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_length = end - start + 1
    with open(audio_path, "rb") as f:
        f.seek(start)
        chunk_data = f.read(chunk_length)

    return Response(
        content=chunk_data,
        status_code=206,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(chunk_data)),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=300",
        },
    )


# ---------------------------------------------------------------------------
# 4. DELETE /api/reports/call-recordings/{recording_id} (Delete with confirmation)
# ---------------------------------------------------------------------------
@router.delete("/call-recordings/{recording_id}")
async def delete_call_recording(
    recording_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently deletes original call recording from storage.
    Verifies tenant permission and idempotency.
    Preserves any Google Drive backup safely.
    """
    try:
        result = await delete_recording(
            db=db,
            recording_id=recording_id,
            tenant_id=tenant_id,
            user_id=current_user.id if current_user else "system",
        )
        return result
    except ValueError as val_err:
        raise HTTPException(status_code=404, detail=str(val_err))
    except Exception as exc:
        logger.error(f"[DELETE] Error deleting recording {recording_id}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to delete recording")


# ---------------------------------------------------------------------------
# 5. POST /api/reports/call-recordings/{recording_id}/export-drive (Single export)
# ---------------------------------------------------------------------------
@router.post("/call-recordings/{recording_id}/export-drive")
async def export_single_recording_to_drive(
    recording_id: str,
    payload: ExportDriveRequest = ExportDriveRequest(),
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Exports a single call recording to Google Drive.
    Verifies upload and saves PostgreSQL backup metadata.
    """
    try:
        res = await upload_recording_to_drive(
            db=db,
            recording_id=recording_id,
            tenant_id=tenant_id,
            destination_folder_id=payload.destination_folder_id,
            user_id=current_user.id if current_user else "system",
        )
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=res.get("message", "Upload failed"))
        return res
    except PermissionError as perm_err:
        raise HTTPException(status_code=401, detail=str(perm_err))
    except FileNotFoundError as fnf_err:
        raise HTTPException(status_code=404, detail=str(fnf_err))
    except ValueError as val_err:
        raise HTTPException(status_code=404, detail=str(val_err))
    except Exception as exc:
        logger.error(f"[DRIVE EXPORT] Error exporting recording {recording_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(exc)}")


# ---------------------------------------------------------------------------
# 6. POST /api/reports/campaigns/{campaign_id}/export-recordings (Campaign batch)
# ---------------------------------------------------------------------------
@router.post("/campaigns/{campaign_id}/export-recordings")
async def export_campaign_recordings_to_drive(
    campaign_id: str,
    payload: ExportCampaignRequest = ExportCampaignRequest(),
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Exports all eligible recordings for a given campaign to Google Drive.
    """
    try:
        result = await export_campaign_recordings(
            db=db,
            campaign_id=campaign_id,
            tenant_id=tenant_id,
            destination_folder_id=payload.destination_folder_id,
            user_id=current_user.id if current_user else "system",
        )
        return result
    except PermissionError as perm_err:
        raise HTTPException(status_code=401, detail=str(perm_err))
    except ValueError as val_err:
        raise HTTPException(status_code=404, detail=str(val_err))
    except Exception as exc:
        logger.error(f"[CAMPAIGN EXPORT] Batch export error for campaign {campaign_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"Campaign export failed: {str(exc)}")


# ---------------------------------------------------------------------------
# 7. GET /api/reports/call-recordings/{recording_id}/backup-status
# ---------------------------------------------------------------------------
@router.get("/call-recordings/{recording_id}/backup-status")
async def get_recording_backup_status(
    recording_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Returns the current backup status and details for a recording."""
    stmt = select(CallRecording).where(
        CallRecording.id == recording_id,
        CallRecording.tenant_id == tenant_id
    )
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    return {
        "recording_id": rec.id,
        "backup_status": rec.backup_status or "NOT_BACKED_UP",
        "google_drive_file_id": rec.google_drive_file_id,
        "google_drive_folder_id": rec.google_drive_folder_id,
        "google_drive_file_url": rec.google_drive_file_url,
        "backup_completed_at": rec.backup_completed_at.isoformat() if rec.backup_completed_at else None,
        "backup_error": rec.backup_error,
    }


# ---------------------------------------------------------------------------
# 8. POST /api/reports/call-recordings/{recording_id}/retry-backup
# ---------------------------------------------------------------------------
@router.post("/call-recordings/{recording_id}/retry-backup")
async def retry_recording_backup(
    recording_id: str,
    payload: ExportDriveRequest = ExportDriveRequest(),
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Retries a failed Google Drive export for a recording."""
    return await export_single_recording_to_drive(
        recording_id=recording_id,
        payload=payload,
        tenant_id=tenant_id,
        current_user=current_user,
        db=db,
    )


# ---------------------------------------------------------------------------
# 9. Google Drive OAuth routes
# ---------------------------------------------------------------------------
@router.get("/google-drive/auth-url")
async def get_google_drive_oauth_url(
    request: Request,
    return_url: Optional[str] = Query(None, description="Initiating frontend page route and parameters"),
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_db_user),
):
    """Generates the Google OAuth authorization URL for Google Drive export."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    subdomain = request.headers.get("x-tenant-subdomain")
    auth_url = get_drive_auth_url(
        user_id=current_user.id,
        business_id=tenant_id,
        return_url=return_url,
        origin=origin,
        subdomain=subdomain,
    )
    return {"auth_url": auth_url}


@router.get("/google-drive/callback")
async def google_drive_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Handles Google OAuth redirect for Drive integration.
    Saves credentials in Integration table and redirects back to originating frontend page.
    """
    drive_payload = decode_drive_oauth_state(state) if state else None
    user_id = str((drive_payload.get("user_id") if drive_payload else None) or "system")
    business_id = str((drive_payload.get("business_id") if drive_payload else None) or "default")
    return_url = sanitize_return_url(drive_payload.get("return_url") if drive_payload else None)
    frontend_base = resolve_frontend_base_url(
        origin=drive_payload.get("origin") if drive_payload else None,
        subdomain=drive_payload.get("subdomain") if drive_payload else None,
    )
    sep = "&" if "?" in return_url else "?"
    destination = f"{frontend_base}{return_url}"

    if error:
        logger.warning(f"[DRIVE OAUTH] Callback received error: {error}")
        return RedirectResponse(f"{destination}{sep}error={urllib.parse.quote_plus(error)}")

    if not code:
        logger.warning("[DRIVE OAUTH] Callback received no code")
        return RedirectResponse(f"{destination}{sep}error=missing_code")

    try:
        # Exchange code for tokens
        tokens = await exchange_code_for_drive_tokens(code)
        access_token = tokens.get("access_token")
        email = await get_user_email_from_token(access_token) if access_token else None

        # Save credentials
        await save_drive_credentials(
            db=db,
            business_id=business_id,
            user_id=user_id,
            tokens=tokens,
            email=email,
        )

        return RedirectResponse(f"{destination}{sep}drive_connected=true")

    except Exception as exc:
        logger.error(f"[DRIVE OAUTH] Exception in OAuth callback: {exc}")
        return RedirectResponse(f"{destination}{sep}error=oauth_failed")


@router.get("/google-drive/status")
async def get_google_drive_status(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Returns Google Drive connection status for the current tenant."""
    return await get_drive_status(db, tenant_id)


@router.post("/google-drive/disconnect")
async def disconnect_google_drive(
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Disconnects Google Drive integration."""
    success = await disconnect_drive(db, tenant_id)
    return {"success": success, "message": "Google Drive disconnected successfully"}


# ---------------------------------------------------------------------------
# 10. POST /api/reports/call-recordings/sync (Administrative manual sync)
# ---------------------------------------------------------------------------
@router.post("/call-recordings/sync")
async def trigger_call_recordings_sync(
    tenant_id: str = Depends(get_current_tenant_id),
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint to trigger sync and 24h cleanup cycle."""
    synced = await sync_all_call_recordings(db)
    cleanup_res = await cleanup_expired_recordings(db)
    return {
        "success": True,
        "synced_count": synced,
        "cleanup_result": cleanup_res,
    }
