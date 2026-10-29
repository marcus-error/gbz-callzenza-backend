"""Email API routes for CallZenza.
Provides endpoints for sending emails, checking Resend connectivity,
and viewing email audit logs.
"""
from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user, require_admin, require_supervisor_or_admin
from app.db.database import get_db
from app.db.models import EmailLog, User
from app.services.email_service import check_resend_connectivity, send_email, send_bulk_email

router = APIRouter(prefix="/api/email", tags=["Email"])


class EmailSendRequest(BaseModel):
    to_email: EmailStr
    subject: str
    html_content: str
    cc: Optional[List[EmailStr]] = None


class BulkEmailSendRequest(BaseModel):
    recipients: List[EmailStr]
    subject: str
    html_content: str


class EmailLogOut(BaseModel):
    id: str
    to_email: str
    subject: str
    status: str
    resend_id: Optional[str] = None
    error: Optional[str] = None
    sent_at: Optional[str] = None

    class Config:
        from_attributes = True


@router.post("/send")
async def send_single_email(
    payload: EmailSendRequest,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Authenticated endpoint to send an email via Resend and record in EmailLog."""
    result = await send_email(
        to_email=str(payload.to_email),
        subject=payload.subject,
        html_content=payload.html_content,
        cc=[str(c) for c in payload.cc] if payload.cc else None,
        business_id=current_user.business_id,
        db=db,
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.get("error") or "Failed to send email via Resend"
        )
    return {
        "success": True,
        "message": "Email sent successfully",
        "resend_id": result.get("id")
    }


@router.post("/send-bulk")
async def send_bulk_emails(
    payload: BulkEmailSendRequest,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Authenticated endpoint to send bulk emails."""
    results = await send_bulk_email(
        recipients=[str(r) for r in payload.recipients],
        subject=payload.subject,
        html_content=payload.html_content,
        business_id=current_user.business_id,
        db=db,
    )
    sent_count = sum(1 for r in results if r.get("success"))
    return {
        "success": True,
        "total": len(payload.recipients),
        "sent": sent_count,
        "results": results
    }


@router.get("/connectivity-check")
async def diagnostic_connectivity_check(
    api_key: Optional[str] = Query(None, description="Optional API key override for testing"),
    current_user: User = Depends(get_current_db_user),
):
    """Diagnostic endpoint (Admin / Supervisor only) verifying live connectivity
    against Resend's real API.
    """
    check_result = check_resend_connectivity(api_key=api_key)
    return check_result


@router.get("/logs", response_model=List[EmailLogOut])
async def get_email_logs(
    limit: int = Query(50, ge=1, le=200),
    status_filter: Optional[str] = Query(None),
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve recent email logs for the current business."""
    stmt = select(EmailLog)
    if current_user.business_id:
        stmt = stmt.where(EmailLog.business_id == current_user.business_id)
    if status_filter:
        stmt = stmt.where(EmailLog.status == status_filter)
    stmt = stmt.order_by(desc(EmailLog.sent_at)).limit(limit)

    results = (await db.execute(stmt)).scalars().all()
    return [
        EmailLogOut(
            id=log.id,
            to_email=log.to_email,
            subject=log.subject,
            status=log.status,
            resend_id=log.resend_id,
            error=log.error,
            sent_at=log.sent_at.isoformat() if log.sent_at else None,
        )
        for log in results
    ]
