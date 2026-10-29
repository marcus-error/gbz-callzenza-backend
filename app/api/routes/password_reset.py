from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError, jwt
from loguru import logger
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.database import get_db
from app.db.models import User
from app.services.email_service import send_email

settings = get_settings()
router = APIRouter(prefix="/api/auth", tags=["auth-password-reset"])


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=10)
    new_password: str = Field(..., min_length=8)


def create_password_reset_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=2)
    payload = {
        "sub": email,
        "scope": "password_reset",
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def verify_password_reset_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        if payload.get("scope") != "password_reset":
            return None
        return payload.get("sub")
    except JWTError:
        return None


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Request a password reset link sent to the user's email."""
    email_clean = payload.email.strip().lower()
    stmt = select(User).where(User.email == email_clean)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()

    # Always return success message to avoid email enumeration
    if not user:
        return {
            "message": "If an account exists with this email, a password reset link has been sent.",
            "success": True,
        }

    token = create_password_reset_token(email_clean)
    reset_link = f"{settings.frontend_url}/reset-password?token={token}"

    import hashlib
    from app.db.models import PasswordResetToken
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db_token = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=2)).replace(tzinfo=None),
        is_used=False
    )
    db.add(db_token)
    await db.commit()

    logger.info(f"[PASSWORD RESET LINK] Reset URL for {email_clean}: {reset_link}")

    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 600px; margin: auto; padding: 32px 24px; background: #0f172a; color: #f8fafc; border-radius: 12px; border: 1px solid #1e293b;">
        <div style="margin-bottom: 24px; text-align: center;">
            <h1 style="font-size: 24px; font-weight: 700; color: #ffffff; margin: 0; letter-spacing: -0.5px;">CallZenza</h1>
            <p style="color: #94a3b8; font-size: 14px; margin-top: 4px;">Enterprise AI Voice & Communication Platform</p>
        </div>
        <div style="background: #1e293b; padding: 24px; border-radius: 8px; border: 1px solid #334155;">
            <h2 style="color: #38bdf8; font-size: 18px; margin-top: 0;">Password Reset Request</h2>
            <p style="color: #cbd5e1; font-size: 14px; line-height: 1.6;">Hello {user.full_name or 'there'},</p>
            <p style="color: #cbd5e1; font-size: 14px; line-height: 1.6;">We received a request to reset your password for your CallZenza account. Click the button below to set a new password. This link is valid for 2 hours.</p>
            <div style="text-align: center; margin: 28px 0;">
                <a href="{reset_link}" style="background: linear-gradient(135deg, #2563eb, #3b82f6); color: #ffffff; padding: 12px 28px; text-decoration: none; border-radius: 6px; font-weight: 600; font-size: 14px; display: inline-block; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.25);">Reset Password</a>
            </div>
            <p style="color: #94a3b8; font-size: 12px; line-height: 1.5;">If you did not request this password reset, please ignore this email or contact support if you suspect unauthorized access.</p>
        </div>
        <p style="color: #64748b; font-size: 11px; margin-top: 20px; text-align: center; word-break: break-all;">Direct link: <a href="{reset_link}" style="color: #38bdf8;">{reset_link}</a></p>
    </div>
    """

    try:
        await send_email(
            to_email=email_clean,
            subject="Reset Your CallZenza Password",
            html_content=html_content,
            business_id=getattr(user, "business_id", None),
            db=db,
        )
    except Exception as e:
        logger.warning(f"Could not send reset email to {email_clean}: {e}")

    return {
        "message": "If an account exists with this email, a password reset link has been sent.",
        "success": True,
    }


@router.get("/verify-reset-token", status_code=status.HTTP_200_OK)
async def verify_reset_token(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Validate a password reset token and return associated user email for UI pre-fill."""
    import hashlib
    from app.db.models import PasswordResetToken

    token_str = token.strip()
    token_hash = hashlib.sha256(token_str.encode()).hexdigest()

    db_token = await db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.is_used == False
        )
    )

    user = None
    if db_token:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if db_token.expires_at < now_naive:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password reset link has expired.")
        user = await db.get(User, db_token.user_id)
    else:
        email = verify_password_reset_token(token_str)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token",
            )
        user = await db.scalar(select(User).where(User.email == email))

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User account not found",
        )

    return {"valid": True, "email": user.email, "full_name": user.full_name or ""}


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reset password using a verified token."""
    import hashlib
    from app.db.models import PasswordResetToken
    
    token_str = payload.token.strip()
    token_hash = hashlib.sha256(token_str.encode()).hexdigest()
    
    # Check db-persisted token first
    db_token = await db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.is_used == False
        )
    )
    
    user = None
    if db_token:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if db_token.expires_at < now_naive:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password reset link has expired.")
        user = await db.get(User, db_token.user_id)
        db_token.is_used = True
        db_token.used_at = now_naive
        db.add(db_token)
    else:
        # Fallback to JWT decoding
        email = verify_password_reset_token(token_str)
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset token",
            )
        user = await db.scalar(select(User).where(User.email == email))

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User account not found",
        )

    clean_pwd = payload.new_password.strip()
    user.hashed_password = hash_password(clean_pwd)
    user.email_verified = True
    if hasattr(user, "plain_password"):
        user.plain_password = clean_pwd

    db.add(user)
    await db.commit()

    logger.info(f"[PASSWORD RESET] Password successfully updated for user {user.email}")
    return {
        "message": "Password has been successfully reset. You can now login.",
        "success": True,
        "email": user.email,
    }
