from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import AuditLog, User
from app.dependencies.tenant_context import get_current_tenant_id

router = APIRouter(prefix="/api/audit-logs", tags=["audit-logs"])


class AuditLogOut(BaseModel):
    id: str
    business_id: Optional[str] = None
    user_id: Optional[str] = None
    action: str
    entity: str
    entity_id: str
    details: Dict[str, Any] = {}
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=List[AuditLogOut])
async def list_audit_logs(
    entity: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> List[AuditLogOut]:
    """Retrieve audit trail logs strictly scoped to the authenticated tenant."""
    stmt = select(AuditLog).where(AuditLog.business_id == tenant_id)
    if entity:
        stmt = stmt.where(AuditLog.entity.ilike(entity))
    if action:
        stmt = stmt.where(AuditLog.action.ilike(action))
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)

    stmt = stmt.order_by(desc(AuditLog.created_at)).offset(offset).limit(limit)
    res = await db.execute(stmt)
    return res.scalars().all()
