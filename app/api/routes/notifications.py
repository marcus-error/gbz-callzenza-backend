from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User
from app.dependencies.tenant_context import get_current_tenant_id
from app.models.notification import NotificationLog
from app.schemas.notification import NotificationCreate, NotificationOut
from app.services.email_service import send_email
from app.services.sms_service import send_sms
from app.services.whatsapp_service import send_whatsapp_message

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=List[NotificationOut])
async def list_notifications(
    channel: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> List[NotificationOut]:
    """List notification delivery history for the current tenant."""
    stmt = select(NotificationLog).where(NotificationLog.tenant_id == tenant_id)
    if channel:
        stmt = stmt.where(NotificationLog.channel == channel.upper())
    if status_filter:
        stmt = stmt.where(NotificationLog.status == status_filter.upper())
    stmt = stmt.order_by(NotificationLog.created_at.desc()).limit(100)
    res = await db.execute(stmt)
    return res.scalars().all()


@router.post("", response_model=NotificationOut, status_code=status.HTTP_201_CREATED)
async def send_notification(
    payload: NotificationCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> NotificationOut:
    """Send an Email, SMS, or WhatsApp notification and store unified delivery log."""
    ch = payload.channel.upper()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    delivery_status = "SENT"
    external_id = None
    error_msg = None

    try:
        if ch == "EMAIL":
            res = await send_email(
                to_email=payload.recipient,
                subject=payload.subject or "Notification from CallZenza",
                html_content=f"<p>{payload.content}</p>",
                business_id=tenant_id,
                db=db,
            )
            if not res.get("success", False):
                delivery_status = "FAILED"
                error_msg = res.get("error", "Email send failed")
            else:
                external_id = res.get("id")

        elif ch == "SMS":
            res = await send_sms(
                to=payload.recipient,
                body=payload.content,
            )
            external_id = res.get("sid")
            if res.get("status") == "failed":
                delivery_status = "FAILED"
                error_msg = res.get("error_message")

        elif ch == "WHATSAPP":
            res = await send_whatsapp_message(
                to=payload.recipient,
                message=payload.content,
            )
            external_id = res.get("message_id")
            if res.get("status") == "error":
                delivery_status = "FAILED"
                error_msg = res.get("message")

    except Exception as exc:
        logger.error(f"[NOTIFICATION ERROR] Error dispatching {ch}: {exc}")
        delivery_status = "FAILED"
        error_msg = str(exc)

    log_entry = NotificationLog(
        tenant_id=tenant_id,
        channel=ch,
        recipient=payload.recipient,
        subject=payload.subject,
        content=payload.content,
        status=delivery_status,
        external_id=external_id,
        error=error_msg,
        notification_metadata=payload.notification_metadata or {},
        sent_at=now if delivery_status == "SENT" else None,
        created_at=now,
    )
    db.add(log_entry)
    await db.commit()
    await db.refresh(log_entry)
    return log_entry
