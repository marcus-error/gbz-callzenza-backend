from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import Optional
from loguru import logger

from app.core.deps import require_supervisor_or_admin
from app.db.database import get_db
from app.db.models import User, Business, Expert, Notification
from app.core.security import hash_password

router = APIRouter(prefix="/api/admin/users", tags=["admin_users"])


class UserInvitePayload(BaseModel):
    email: EmailStr
    password: Optional[str] = "password123"
    role: Optional[str] = "USER"
    full_name: str = ""
    business_id: Optional[str] = None
    permissions: Optional[dict] = None


class UserAdminUpdatePayload(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None
    permissions: Optional[dict] = None
    full_name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None


class UserAdminOut(BaseModel):
    id: str
    email: str
    full_name: Optional[str] = ""
    role: str
    is_active: bool = True
    business_id: Optional[str] = None
    permissions: Optional[dict] = None
    plain_password: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        extra = "ignore"


SUPERVISOR_BLOCKED_ROLES = ["ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER"]


@router.get("", response_model=list[UserAdminOut])
async def list_users(
    role: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> list[User]:
    try:
        query = select(User)
        
        if current_user.role.upper() == "SUPERVISOR":
            query = query.where(User.role.notin_(SUPERVISOR_BLOCKED_ROLES))
            if current_user.business_id:
                query = query.where(User.business_id == current_user.business_id)
            if role:
                role_clean = role.strip().upper()
                if role_clean in SUPERVISOR_BLOCKED_ROLES:
                    return []
                query = query.where(User.role == role)
        else:
            if role:
                query = query.where(User.role == role)
            if current_user.business_id:
                query = query.where(User.business_id == current_user.business_id)

        query = query.order_by(User.created_at.desc())
        result = await db.scalars(query)
        return list(result.all())
    except Exception as e:
        await db.rollback()
        logger.error(f"[LIST USERS ERROR] {e}")
        return []


@router.get("/{id}", response_model=UserAdminOut)
async def get_user_by_id(
    id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> User:
    try:
        user = await db.get(User, id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if current_user.role.upper() != "SUPER_ADMIN":
            if current_user.business_id and user.business_id and user.business_id != current_user.business_id:
                raise HTTPException(status_code=403, detail="Forbidden: Cannot view users from other organizations")
        if current_user.role.upper() == "SUPERVISOR":
            if user.role.upper() in SUPERVISOR_BLOCKED_ROLES:
                raise HTTPException(status_code=403, detail="Forbidden: You do not have permission to view this account")
        return user
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"[GET USER ERROR] {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/invite", response_model=UserAdminOut, status_code=status.HTTP_201_CREATED)
async def invite_user(
    payload: UserInvitePayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> User:
    try:
        role_val = (payload.role or "USER").strip().upper()
        if role_val == "AGENT":
            role_val = "VOICE_AGENT"

        if current_user.role.upper() == "SUPERVISOR":
            if role_val in SUPERVISOR_BLOCKED_ROLES:
                raise HTTPException(status_code=403, detail="Supervisors cannot create admin or supervisor users")

        if not payload.password or not payload.password.strip():
            raise HTTPException(status_code=400, detail="Password is required to create a new user account")

        normalized_email = payload.email.strip().lower()
        existing = await db.scalar(select(User).where(User.email == normalized_email))
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Safely resolve target business_id to avoid FK constraint violations
        if current_user.role.upper() == "SUPER_ADMIN":
            target_biz_id = payload.business_id or current_user.business_id
        else:
            target_biz_id = current_user.business_id

        if not target_biz_id or target_biz_id.strip() == "":
            target_biz_id = await db.scalar(select(Business.id).limit(1))
        
        target_biz_id_clean = target_biz_id if (target_biz_id and target_biz_id.strip() != "") else None

        clean_pwd = (payload.password or "").strip() or "password123"
        user = User(
            email=normalized_email,
            hashed_password=hash_password(clean_pwd),
            full_name=payload.full_name or normalized_email.split("@")[0].capitalize(),
            role=role_val,
            is_active=True,
            email_verified=True,
            business_id=target_biz_id_clean,
            permissions=payload.permissions or {},
            plain_password=clean_pwd,
        )
        db.add(user)
        await db.flush()

        if role_val in ("VOICE_AGENT", "AGENT"):
            import uuid
            existing_expert = await db.scalar(select(Expert).where((Expert.user_id == user.id) | (Expert.email == normalized_email)))
            if not existing_expert:
                expert = Expert(
                    id=str(uuid.uuid4()),
                    business_id=target_biz_id_clean,
                    user_id=user.id,
                    name=user.full_name or user.email,
                    email=user.email,
                    status="AVAILABLE",
                    active_lead_count=0
                )
                db.add(expert)
            else:
                existing_expert.user_id = user.id
                existing_expert.business_id = target_biz_id_clean

        # Auto-sync newly created user to appropriate User Group
        if target_biz_id_clean:
            try:
                from app.api.routes.user_groups import ensure_default_user_groups
                await ensure_default_user_groups(db, target_biz_id_clean)
            except Exception as ug_err:
                logger.warning(f"Could not auto-sync user to user group: {ug_err}")

        # Create secure 7-day UserInvitation record
        import secrets
        import hashlib
        from datetime import timedelta, timezone
        from app.db.models import UserInvitation
        from app.services.email_service import send_email
        from app.core.config import get_settings
        app_settings = get_settings()

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).replace(tzinfo=None)

        inv = UserInvitation(
            tenant_id=target_biz_id_clean,
            business_id=target_biz_id_clean,
            user_id=user.id,
            email=normalized_email,
            role=role_val,
            token_hash=token_hash,
            expires_at=expires_at,
            status="PENDING",
        )
        db.add(inv)

        invite_url = f"{app_settings.frontend_url}/accept-invitation?token={raw_token}"
        logger.info(f"[SAAS INVITATION LINK] Admin invited {normalized_email}: {invite_url}")

        biz_name = "CallZenza Workspace"
        if target_biz_id_clean:
            biz_obj = await db.get(Business, target_biz_id_clean)
            if biz_obj:
                biz_name = biz_obj.name

        html_content = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: auto; padding: 32px 24px; background: #0b0f19; color: #f8fafc; border-radius: 12px; border: 1px solid #1e293b;">
            <div style="margin-bottom: 24px; text-align: center;">
                <h1 style="font-size: 26px; font-weight: 700; color: #ffffff; margin: 0;">CallZenza</h1>
                <p style="color: #94a3b8; font-size: 14px; margin-top: 4px;">Enterprise AI Voice & Communication Platform</p>
            </div>
            <div style="background: #131b2e; padding: 28px; border-radius: 10px; border: 1px solid #1e293b;">
                <h2 style="color: #38bdf8; font-size: 20px; margin-top: 0;">You've Been Invited to Join {biz_name}</h2>
                <p style="color: #cbd5e1; font-size: 14px; line-height: 1.6;">Hello {user.full_name or 'there'},</p>
                <p style="color: #cbd5e1; font-size: 14px; line-height: 1.6;">You have been invited by an administrator to join {biz_name} on CallZenza as a <strong>{user.role}</strong>.</p>
                <p style="color: #cbd5e1; font-size: 14px; line-height: 1.6;">Click the button below to accept the invitation and set your personal password. This link is valid for 7 days.</p>
                <div style="text-align: center; margin: 32px 0;">
                    <a href="{invite_url}" style="background: linear-gradient(135deg, #2563eb, #3b82f6); color: #ffffff; padding: 14px 32px; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 15px; display: inline-block;">Accept Invitation & Set Password</a>
                </div>
            </div>
            <p style="color: #64748b; font-size: 11px; margin-top: 24px; text-align: center; word-break: break-all;">Direct link: <a href="{invite_url}" style="color: #38bdf8;">{invite_url}</a></p>
        </div>
        """
        try:
            await send_email(
                to_email=normalized_email,
                subject=f"Invitation to join {biz_name} on CallZenza",
                html_content=html_content,
                business_id=target_biz_id_clean,
                db=db,
            )
        except Exception as mail_err:
            logger.warning(f"Could not send email invite: {mail_err}")

        await db.commit()
        await db.refresh(user)
        return user
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"[INVITE USER ERROR] Failed to invite/create user: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create user account: {str(e)}")


@router.patch("/{id}", response_model=UserAdminOut)
async def update_user(
    id: str,
    payload: UserAdminUpdatePayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> User:
    try:
        user = await db.get(User, id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if current_user.role.upper() != "SUPER_ADMIN":
            if current_user.business_id and user.business_id and user.business_id != current_user.business_id:
                raise HTTPException(status_code=403, detail="Forbidden: Cannot modify users from other organizations")

        if current_user.role.upper() == "SUPERVISOR":
            if user.role.upper() in SUPERVISOR_BLOCKED_ROLES:
                raise HTTPException(status_code=403, detail="Supervisors cannot modify admin or supervisor accounts")
            if payload.role and payload.role.strip().upper() in SUPERVISOR_BLOCKED_ROLES:
                raise HTTPException(status_code=403, detail="Supervisors cannot assign admin or supervisor roles")

        if payload.full_name is not None:
            user.full_name = payload.full_name
        if payload.email is not None:
            user.email = payload.email.strip().lower()
        if payload.password and payload.password.strip():
            user.hashed_password = hash_password(payload.password.strip())
            user.plain_password = payload.password.strip()
        if payload.is_active is not None:
            user.is_active = payload.is_active
        if payload.permissions is not None:
            user.permissions = payload.permissions
        if payload.role is not None:
            new_role = payload.role.strip().upper()
            if new_role == "AGENT":
                new_role = "VOICE_AGENT"
            user.role = new_role
            if new_role in ("VOICE_AGENT", "AGENT"):
                import uuid
                existing_expert = await db.scalar(select(Expert).where((Expert.user_id == user.id) | (Expert.email == user.email)))
                biz_target = user.business_id or current_user.business_id
                if not biz_target or biz_target.strip() == "":
                    biz_target = await db.scalar(select(Business.id).limit(1))
                biz_target_clean = biz_target if (biz_target and biz_target.strip() != "") else None

                if not existing_expert:
                    expert = Expert(
                        id=str(uuid.uuid4()),
                        business_id=biz_target_clean,
                        user_id=user.id,
                        name=user.full_name or user.email,
                        email=user.email,
                        status="AVAILABLE",
                        active_lead_count=0
                    )
                    db.add(expert)
                else:
                    existing_expert.user_id = user.id

        linked_expert = await db.scalar(select(Expert).where(Expert.user_id == user.id))
        if linked_expert:
            if payload.full_name is not None:
                linked_expert.name = user.full_name
            if payload.email is not None:
                linked_expert.email = user.email

        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"[UPDATE USER ERROR] Failed to update user {id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_user(
    id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> Response:
    try:
        if current_user.id == id:
            raise HTTPException(status_code=400, detail="Cannot delete yourself")
        user = await db.get(User, id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if current_user.role.upper() != "SUPER_ADMIN":
            if current_user.business_id and user.business_id and user.business_id != current_user.business_id:
                raise HTTPException(status_code=403, detail="Forbidden: Cannot delete users from other organizations")
            
        if current_user.role.upper() == "SUPERVISOR":
            if user.role.upper() in SUPERVISOR_BLOCKED_ROLES:
                raise HTTPException(status_code=403, detail="Supervisors cannot delete admin or supervisor accounts")

        from sqlalchemy import update, delete
        from app.db.models import (
            UserInvitation,
            EmailVerificationToken,
            PasswordResetToken,
        )
        await db.execute(update(Expert).where(Expert.user_id == id).values(user_id=None))
        await db.execute(delete(UserInvitation).where(UserInvitation.user_id == id))
        await db.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == id))
        await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == id))
        await db.execute(delete(Notification).where(Notification.user_id == id))

        await db.delete(user)
        await db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"[DELETE USER ERROR] Failed to delete user {id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
