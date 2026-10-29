from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, require_role
from app.db.database import get_db
from app.db.models import User
from app.schemas.user import UserOut

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def read_me(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from loguru import logger
    logger.info("[API_USERS_ME] REQUEST_START")
    logger.info("[API_USERS_ME] AUTH_START")
    try:
        user_id = str(current_user.get("sub") or "")
        logger.info(f"[API_USERS_ME] AUTH_SUCCESS user_id={user_id}")
        user = await db.get(User, user_id)
        if not user:
            email = current_user.get("email")
            if email:
                res = await db.execute(select(User).where(User.email == email))
                user = res.scalar_one_or_none()
        if not user:
            logger.warning("[API_USERS_ME] ERROR status_code=404 detail=\"User profile not found\"")
            raise HTTPException(status_code=404, detail="User profile not found")
        perms = getattr(user, "permissions", {}) or {}
        if isinstance(perms, str):
            try:
                import json
                perms = json.loads(perms)
            except Exception:
                perms = {}
        res_obj = UserOut(
            id=str(user.id),
            email=user.email,
            full_name=getattr(user, "full_name", "") or "",
            role=getattr(user, "role", "USER") or "USER",
            business_id=getattr(user, "business_id", None),
            tenant_id=getattr(user, "business_id", None),
            permissions=perms if isinstance(perms, dict) else {},
        )
        logger.info(f"[API_USERS_ME] RESPONSE_SUCCESS email={user.email} role={user.role}")
        return res_obj
    except HTTPException as he:
        logger.warning(f"[API_USERS_ME] ERROR status_code={he.status_code} detail=\"{he.detail}\"")
        raise he
    except Exception as e:
        logger.error(f"[API_USERS_ME] ERROR exception={e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load user profile: {str(e)}")


from typing import Optional


@router.get("", response_model=list[UserOut])
async def list_users(
    role: Optional[str] = None,
    _: dict = Depends(require_role("admin", "ADMIN", "SUPER_ADMIN")),
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    """Admin-only endpoint — demonstrates RBAC (TS-012)."""
    query = select(User)
    if role:
        query = query.where(User.role == role)
    else:
        query = query.where(User.role != "VOICE_AGENT")
    result = await db.scalars(query)
    return list(result.all())
