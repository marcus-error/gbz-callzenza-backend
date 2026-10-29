from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User
from app.models.role import Permission
from app.schemas.role import PermissionOut

router = APIRouter(prefix="/api/permissions", tags=["permissions"])


@router.get("", response_model=List[PermissionOut])
async def list_permissions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> List[PermissionOut]:
    """List all available system permissions."""
    stmt = select(Permission).order_by(Permission.module.asc(), Permission.name.asc())
    result = await db.execute(stmt)
    return result.scalars().all()
