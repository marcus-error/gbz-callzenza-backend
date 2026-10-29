from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User
from app.dependencies.tenant_context import get_current_tenant_id
from app.models.branding import TenantBranding
from app.schemas.branding import TenantBrandingOut, TenantBrandingUpdate

router = APIRouter(prefix="/api/branding", tags=["branding"])


@router.get("", response_model=TenantBrandingOut)
async def get_tenant_branding(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> TenantBrandingOut:
    """Get the branding settings for the current tenant."""
    stmt = select(TenantBranding).where(TenantBranding.tenant_id == tenant_id)
    res = await db.execute(stmt)
    branding = res.scalar_one_or_none()

    if not branding:
        branding = TenantBranding(
            tenant_id=tenant_id,
            company_name="CallZenza AI",
            logo_url="/logo.png",
            primary_color="#4F46E5",
            secondary_color="#06B6D4",
            accent_color="#10B981",
        )
        db.add(branding)
        await db.commit()
        await db.refresh(branding)

    return branding


@router.put("", response_model=TenantBrandingOut)
async def update_tenant_branding(
    payload: TenantBrandingUpdate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> TenantBrandingOut:
    """Update branding configuration for the current tenant."""
    stmt = select(TenantBranding).where(TenantBranding.tenant_id == tenant_id)
    res = await db.execute(stmt)
    branding = res.scalar_one_or_none()

    if not branding:
        branding = TenantBranding(tenant_id=tenant_id)
        db.add(branding)

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if value is not None:
            setattr(branding, key, value)

    await db.commit()
    await db.refresh(branding)
    return branding
