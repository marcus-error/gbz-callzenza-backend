import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import PRIMARY_BUSINESS_ID
from app.core.super_admin_bootstrap import require_super_admin
from app.db.database import get_db
from app.db.models import DemoRequest, Notification, User
from app.schemas.demo_request import (
    DemoRequestCreate,
    DemoRequestItem,
    DemoRequestResponse,
    DemoRequestStatusUpdate,
    SuperAdminNotificationItem,
)

try:
    from app.api.routes.websockets import global_ws_manager
except Exception:
    global_ws_manager = None

router = APIRouter(tags=["demo-requests"])


# ==============================================================================
# 1. PUBLIC ENDPOINT: Submit Demo Request
# ==============================================================================
@router.post(
    "/api/demo-requests",
    response_model=DemoRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a new demo request from the public landing page",
)
async def create_demo_request(
    payload: DemoRequestCreate,
    db: AsyncSession = Depends(get_db),
) -> DemoRequestResponse:
    """
    Publicly accessible endpoint allowing prospective clients to request a demo.
    Atomically saves the DemoRequest and creates in-app notifications for Super Admin(s).
    """
    org_name = payload.organization_name.strip()
    email_val = payload.email.strip().lower()

    if not org_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Organization Name cannot be empty.",
        )

    if not email_val or "@" not in email_val:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A valid Email Address is required.",
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    demo_id = str(uuid.uuid4())

    new_demo = DemoRequest(
        id=demo_id,
        organization_name=org_name,
        email=email_val,
        status="NEW",
        created_at=now,
        updated_at=now,
    )

    try:
        # Atomic transaction: create demo request + super admin notifications
        db.add(new_demo)
        await db.flush()

        # Find all Super Admins to notify
        admin_res = await db.scalars(
            select(User).where(
                or_(
                    User.role == "SUPER_ADMIN",
                    User.email == "admin@callzenza.com",
                    User.email == "superadmin@callzenza.com",
                )
            )
        )
        super_admins = admin_res.all()

        if super_admins:
            for admin in super_admins:
                notif = Notification(
                    id=str(uuid.uuid4()),
                    business_id=admin.business_id or PRIMARY_BUSINESS_ID,
                    user_id=admin.id,
                    title="New Demo Request",
                    message=f"New demo request received from {org_name} ({email_val}).",
                    type="DEMO_REQUEST",
                    reference_id=demo_id,
                    read=False,
                    created_at=now,
                )
                db.add(notif)
        else:
            # Fallback notification linked to primary business
            fallback_notif = Notification(
                id=str(uuid.uuid4()),
                business_id=PRIMARY_BUSINESS_ID,
                user_id="system",
                title="New Demo Request",
                message=f"New demo request received from {org_name} ({email_val}).",
                type="DEMO_REQUEST",
                reference_id=demo_id,
                read=False,
                created_at=now,
            )
            db.add(fallback_notif)

        await db.commit()
        logger.info(f"[DEMO REQUEST] Created demo request '{demo_id}' for {org_name} <{email_val}>")

    except Exception as exc:
        await db.rollback()
        logger.error(f"[DEMO REQUEST ERROR] Failed to record demo request: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to submit your demo request. Please try again later.",
        )

    # Broadcast real-time event if WebSocket manager is active
    if global_ws_manager:
        try:
            await global_ws_manager.broadcast({
                "event": "DEMO_REQUEST_RECEIVED",
                "demo_request": {
                    "id": demo_id,
                    "organization_name": org_name,
                    "email": email_val,
                    "status": "NEW",
                    "created_at": now.isoformat(),
                },
            })
        except Exception as ws_err:
            logger.debug(f"[WS BROADCAST NOTICE] Live websocket broadcast note: {ws_err}")

    return DemoRequestResponse(
        success=True,
        message="Demo request submitted successfully. Our team will contact you soon.",
        demo_request_id=demo_id,
    )


# ==============================================================================
# 2. SUPER ADMIN ENDPOINTS: Demo Requests Management
# ==============================================================================
@router.get(
    "/api/super-admin/demo-requests",
    response_model=List[DemoRequestItem],
    summary="List all demo requests (Super Admin)",
)
@router.get(
    "/api/admin/demo-requests",
    response_model=List[DemoRequestItem],
    summary="Alias: List all demo requests (Super Admin)",
)
async def list_demo_requests(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> List[DemoRequestItem]:
    """Retrieve demo requests filtered by status or all, sorted newest first."""
    stmt = select(DemoRequest).order_by(DemoRequest.created_at.desc())
    if status_filter and status_filter.strip().upper() != "ALL":
        stmt = stmt.where(DemoRequest.status == status_filter.strip().upper())

    res = await db.scalars(stmt)
    return res.all()


@router.get(
    "/api/super-admin/demo-requests/{demo_id}",
    response_model=DemoRequestItem,
    summary="Get single demo request details (Super Admin)",
)
@router.get(
    "/api/admin/demo-requests/{demo_id}",
    response_model=DemoRequestItem,
    summary="Alias: Get single demo request details (Super Admin)",
)
async def get_demo_request(
    demo_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> DemoRequestItem:
    """Retrieve a single demo request by ID."""
    demo = await db.get(DemoRequest, demo_id)
    if not demo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo request not found.",
        )
    return demo


@router.patch(
    "/api/super-admin/demo-requests/{demo_id}/status",
    response_model=DemoRequestItem,
    summary="Update demo request status (Super Admin)",
)
@router.patch(
    "/api/admin/demo-requests/{demo_id}/status",
    response_model=DemoRequestItem,
    summary="Alias: Update demo request status (Super Admin)",
)
async def update_demo_request_status(
    demo_id: str,
    payload: DemoRequestStatusUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> DemoRequestItem:
    """Update status of a demo request (e.g. NEW, CONTACTED, SCHEDULED, COMPLETED, ARCHIVED)."""
    demo = await db.get(DemoRequest, demo_id)
    if not demo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo request not found.",
        )

    demo.status = payload.status
    demo.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(demo)
    await db.commit()
    await db.refresh(demo)
    return demo


# ==============================================================================
# 3. SUPER ADMIN NOTIFICATIONS ENDPOINTS
# ==============================================================================
@router.get(
    "/api/super-admin/notifications",
    summary="List Super Admin notifications with unread count and demo details",
)
async def get_super_admin_notifications(
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """
    Fetch in-app notifications for the Super Admin.
    Automatically enriches DEMO_REQUEST notifications with linked DemoRequest data.
    """
    stmt = (
        select(Notification)
        .where(
            or_(
                Notification.user_id == admin.id,
                Notification.user_id == "system",
                Notification.type == "DEMO_REQUEST",
            )
        )
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    res = await db.scalars(stmt)
    raw_notifs = res.all()

    # Collect reference IDs for DEMO_REQUESTs
    demo_ids = [n.reference_id for n in raw_notifs if n.type == "DEMO_REQUEST" and n.reference_id]
    demo_map = {}
    if demo_ids:
        demos_res = await db.scalars(select(DemoRequest).where(DemoRequest.id.in_(demo_ids)))
        for d in demos_res.all():
            demo_map[d.id] = {
                "id": d.id,
                "organization_name": d.organization_name,
                "email": d.email,
                "status": d.status,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            }

    items = []
    unread_count = 0
    for n in raw_notifs:
        if not n.read:
            unread_count += 1
        items.append({
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "type": n.type,
            "reference_id": n.reference_id,
            "read": n.read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "demo_request": demo_map.get(n.reference_id) if n.reference_id else None,
        })

    return {
        "success": True,
        "unread_count": unread_count,
        "notifications": items,
    }


@router.patch(
    "/api/super-admin/notifications/{notification_id}/read",
    summary="Mark a notification as read (Super Admin)",
)
async def mark_notification_read(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """Mark a specific notification as read."""
    notif = await db.get(Notification, notification_id)
    if not notif:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found.",
        )

    notif.read = True
    db.add(notif)
    await db.commit()
    return {"success": True, "message": "Notification marked as read."}


@router.post(
    "/api/super-admin/notifications/mark-all-read",
    summary="Mark all notifications as read (Super Admin)",
)
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    """Mark all unread notifications as read."""
    stmt = (
        select(Notification)
        .where(
            or_(
                Notification.user_id == admin.id,
                Notification.user_id == "system",
                Notification.type == "DEMO_REQUEST",
            ),
            Notification.read == False,
        )
    )
    res = await db.scalars(stmt)
    for n in res.all():
        n.read = True
        db.add(n)
    await db.commit()
    return {"success": True, "message": "All notifications marked as read."}
