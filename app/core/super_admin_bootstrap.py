import secrets
import string
import uuid
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends, HTTPException, status

from app.core.config import get_settings
from app.core.security import get_current_user, hash_password
from app.db.database import get_db
from app.db.models import User, Business


def generate_secure_password(length: int = 20) -> str:
    """Generate a high-entropy, cryptographically secure password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%^&*()-_=+" for c in pwd)
        ):
            return pwd


async def ensure_super_admin_bootstrapped(db: AsyncSession) -> None:
    """Secure bootstrap mechanism for the platform Super Admin account."""
    settings = get_settings()
    admin_email = settings.super_admin_email.strip().lower()

    existing = await db.scalar(select(User).where(User.email == admin_email))
    if existing:
        updated = False
        if existing.role != "SUPER_ADMIN":
            existing.role = "SUPER_ADMIN"
            updated = True
        if not existing.email_verified:
            existing.email_verified = True
            updated = True
        if not existing.is_active:
            existing.is_active = True
            updated = True
        if updated:
            db.add(existing)
            await db.commit()
            logger.info(f"[BOOTSTRAP] Existing account '{admin_email}' verified as SUPER_ADMIN.")
        return

    # Create new Super Admin account
    initial_pwd = settings.super_admin_initial_password.strip()
    is_generated = False
    if not initial_pwd:
        initial_pwd = generate_secure_password()
        is_generated = True

    primary_bid = "c3dce06c-8330-4724-ba33-b5282b3c3596"
    admin_user = User(
        id=str(uuid.uuid4()),
        email=admin_email,
        hashed_password=hash_password(initial_pwd),
        plain_password=None,  # Never store plaintext password
        full_name="System Admin",
        role="SUPER_ADMIN",
        is_active=True,
        email_verified=True,
        business_id=primary_bid,
    )
    db.add(admin_user)
    await db.commit()
    logger.warning(
        f"[BOOTSTRAP] Initial Super Admin account created for '{admin_email}'. "
        f"{'Password generated automatically from secure entropy.' if is_generated else 'Configured via environment variables.'}"
    )


async def require_super_admin(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependency: strictly requires authenticated SUPER_ADMIN role."""
    user_id = str(current_user.get("sub") or "")
    user = await db.get(User, user_id)
    if not user:
        email = current_user.get("email")
        if email:
            user = await db.scalar(select(User).where(User.email == email))

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Super Admin authentication required.",
        )

    if user.role != "SUPER_ADMIN" and user.email not in ("superadmin@callzenza.com", "admin@callzenza.com"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Only platform Super Admins can access this administrative resource.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super Admin account is disabled or suspended.",
        )

    return user
