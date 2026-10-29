import secrets
import string
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.database import get_db
from app.db.models import User
from app.dependencies.tenant_context import get_current_tenant_id
from app.schemas.tenant_user import (
    TenantUserCreate,
    TenantUserInvite,
    TenantUserOut,
    TenantUserUpdate,
)
from app.services.email_service import send_email

settings = get_settings()
router = APIRouter(prefix="/api/tenant/users", tags=["tenant-users"])


def generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.get("", response_model=List[TenantUserOut])
async def list_tenant_users(
    role: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> List[TenantUserOut]:
    """List all users belonging strictly to the current tenant."""
    stmt = select(User).where(User.business_id == tenant_id)
    if role:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    stmt = stmt.order_by(User.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=TenantUserOut, status_code=status.HTTP_201_CREATED)
async def create_tenant_user(
    payload: TenantUserCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> TenantUserOut:
    """Create a user bound directly to the authenticated tenant."""
    email_clean = payload.email.strip().lower()
    existing = await db.execute(select(User).where(User.email == email_clean))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists",
        )

    user = User(
        email=email_clean,
        hashed_password=hash_password(payload.password),
        plain_password=payload.password,
        full_name=payload.full_name.strip(),
        role=payload.role.strip().upper(),
        business_id=tenant_id,
        permissions=payload.permissions or {},
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/{user_id}", response_model=TenantUserOut)
async def get_tenant_user(
    user_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> TenantUserOut:
    stmt = select(User).where(User.id == user_id, User.business_id == tenant_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant user not found")
    return user


@router.put("/{user_id}", response_model=TenantUserOut)
async def update_tenant_user(
    user_id: str,
    payload: TenantUserUpdate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> TenantUserOut:
    stmt = select(User).where(User.id == user_id, User.business_id == tenant_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant user not found")

    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if payload.role is not None:
        user.role = payload.role.strip().upper()
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.permissions is not None:
        user.permissions = payload.permissions

    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_200_OK)
async def delete_tenant_user(
    user_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Soft deletes/deactivates a tenant user to maintain referential integrity."""
    stmt = select(User).where(User.id == user_id, User.business_id == tenant_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant user not found")

    user.is_active = False
    db.add(user)
    await db.commit()
    return {"message": "User deactivated successfully", "id": user_id, "is_active": False}


@router.post("/invite", status_code=status.HTTP_201_CREATED)
async def invite_tenant_user(
    payload: TenantUserInvite,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Invite a new team member to this tenant via secure invitation token (expires in 7 days)."""
    import hashlib
    from datetime import datetime, timedelta, timezone
    from app.db.models import Business, UserInvitation

    email_clean = payload.email.strip().lower()
    existing = await db.scalar(select(User).where(User.email == email_clean))
    if existing and existing.status != "INVITED" and existing.email_verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A registered user with this email already exists",
        )

    # Invalidate previous pending invitations for this email
    old_invites = await db.scalars(
        select(UserInvitation).where(
            UserInvitation.email == email_clean,
            UserInvitation.status == "PENDING"
        )
    )
    for old_inv in old_invites:
        old_inv.status = "REVOKED"
        db.add(old_inv)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).replace(tzinfo=None)

    role_clean = payload.role.strip().upper()
    if role_clean == "AGENT":
        role_clean = "VOICE_AGENT"

    # Pre-create or update invited user record
    if not existing:
        temp_secret = secrets.token_hex(16)
        user = User(
            email=email_clean,
            hashed_password=hash_password(temp_secret),
            plain_password="",
            full_name=payload.full_name.strip() if payload.full_name else email_clean.split("@")[0].capitalize(),
            role=role_clean,
            business_id=tenant_id,
            is_active=True,
            status="INVITED",
            email_verified=False,
        )
        db.add(user)
        await db.flush()
    else:
        user = existing
        user.role = role_clean
        user.business_id = tenant_id
        user.status = "INVITED"
        db.add(user)
        await db.flush()

    invitation = UserInvitation(
        tenant_id=tenant_id,
        business_id=tenant_id,
        user_id=user.id,
        email=email_clean,
        role=role_clean,
        token_hash=token_hash,
        expires_at=expires_at,
        status="PENDING",
    )
    db.add(invitation)

    await db.commit()
    await db.refresh(user)

    # Resolve Business Name
    biz = await db.get(Business, tenant_id)
    company_name = biz.name if biz else "CallZenza Workspace"

    invite_url = f"{settings.frontend_url}/accept-invitation?token={raw_token}"
    logger.info(f"[SAAS INVITATION LINK] Invitation URL for {email_clean}: {invite_url}")

    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: auto; padding: 32px 24px; background: #0b0f19; color: #f8fafc; border-radius: 12px; border: 1px solid #1e293b;">
        <div style="margin-bottom: 24px; text-align: center;">
            <h1 style="font-size: 26px; font-weight: 700; color: #ffffff; margin: 0; letter-spacing: -0.5px;">CallZenza</h1>
            <p style="color: #94a3b8; font-size: 14px; margin-top: 4px;">Enterprise AI Voice & Communication Platform</p>
        </div>
        <div style="background: #131b2e; padding: 28px; border-radius: 10px; border: 1px solid #1e293b;">
            <h2 style="color: #38bdf8; font-size: 20px; margin-top: 0; font-weight: 600;">You've Been Invited to Join {company_name}</h2>
            <p style="color: #cbd5e1; font-size: 14px; line-height: 1.6;">Hello {user.full_name or 'there'},</p>
            <p style="color: #cbd5e1; font-size: 14px; line-height: 1.6;">You have been invited to collaborate on CallZenza as a <strong>{user.role}</strong>.</p>
            <p style="color: #cbd5e1; font-size: 14px; line-height: 1.6;">Click the button below to accept the invitation and choose your password. This invitation is valid for 7 days.</p>
            <div style="text-align: center; margin: 32px 0;">
                <a href="{invite_url}" style="background: linear-gradient(135deg, #2563eb, #3b82f6); color: #ffffff; padding: 14px 32px; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 15px; display: inline-block; box-shadow: 0 4px 14px rgba(37, 99, 235, 0.35);">Accept Invitation & Set Password</a>
            </div>
            <p style="color: #94a3b8; font-size: 12px; line-height: 1.5;">For security purposes, you will create your own password upon accepting the invitation.</p>
        </div>
        <p style="color: #64748b; font-size: 11px; margin-top: 24px; text-align: center; word-break: break-all;">Direct link: <a href="{invite_url}" style="color: #38bdf8;">{invite_url}</a></p>
    </div>
    """

    email_sent = False
    try:
        res = await send_email(
            to_email=email_clean,
            subject=f"Invitation to join {company_name} on CallZenza",
            html_content=html_content,
            business_id=tenant_id,
            db=db,
        )
        email_sent = res.get("success", False)
    except Exception as e:
        logger.warning(f"Failed to send invite email to {email_clean}: {e}")

    return {
        "message": "User invitation generated and sent successfully",
        "user_id": user.id,
        "email": user.email,
        "role": user.role,
        "email_sent": email_sent,
        "invite_link": invite_url,
    }
