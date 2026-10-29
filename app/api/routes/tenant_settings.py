from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User
from app.dependencies.tenant_context import get_current_tenant_id
from app.models.tenant_settings import TenantSettings
from app.schemas.tenant_settings import TenantSettingsOut, TenantSettingsUpdate

router = APIRouter(prefix="/api/tenant/settings", tags=["tenant-settings"])


@router.get("", response_model=TenantSettingsOut)
async def get_tenant_settings(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> TenantSettingsOut:
    """Retrieve operational settings for the current tenant."""
    stmt = select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    res = await db.execute(stmt)
    settings = res.scalar_one_or_none()

    if not settings:
        settings = TenantSettings(tenant_id=tenant_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

    return settings


@router.put("", response_model=TenantSettingsOut)
async def update_tenant_settings(
    payload: TenantSettingsUpdate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> TenantSettingsOut:
    """Update operational settings for the current tenant."""
    stmt = select(TenantSettings).where(TenantSettings.tenant_id == tenant_id)
    res = await db.execute(stmt)
    settings = res.scalar_one_or_none()

    if not settings:
        settings = TenantSettings(tenant_id=tenant_id)
        db.add(settings)

    update_data = payload.model_dump(exclude_unset=True)
    for key, val in update_data.items():
        if val is not None:
            setattr(settings, key, val)

    await db.commit()
    await db.refresh(settings)
    return settings
