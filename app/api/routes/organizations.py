from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.dependencies.tenant_context import get_current_tenant_id
from app.models.organization import Organization
from app.schemas.organization import (
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
)

router = APIRouter(prefix="/api/organizations", tags=["organizations"])


@router.post("", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> OrganizationOut:
    org = Organization(
        tenant_id=tenant_id,
        name=payload.name.strip(),
        org_type=payload.org_type,
        code=payload.code,
        parent_id=payload.parent_id,
        phone=payload.phone,
        email=payload.email,
        address=payload.address,
        details=payload.details or {},
        status=payload.status or "active",
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


@router.get("", response_model=List[OrganizationOut])
async def list_organizations(
    org_type: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> List[OrganizationOut]:
    stmt = select(Organization).where(Organization.tenant_id == tenant_id)
    if org_type:
        stmt = stmt.where(Organization.org_type == org_type)
    if status_filter:
        stmt = stmt.where(Organization.status == status_filter)
    stmt = stmt.order_by(Organization.created_at.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{org_id}", response_model=OrganizationOut)
async def get_organization(
    org_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> OrganizationOut:
    stmt = select(Organization).where(
        Organization.id == org_id,
        Organization.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


@router.put("/{org_id}", response_model=OrganizationOut)
async def update_organization(
    org_id: str,
    payload: OrganizationUpdate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> OrganizationOut:
    stmt = select(Organization).where(
        Organization.id == org_id,
        Organization.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if value is not None:
            setattr(org, key, value)

    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


@router.delete("/{org_id}", status_code=status.HTTP_200_OK)
async def delete_organization(
    org_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(Organization).where(
        Organization.id == org_id,
        Organization.tenant_id == tenant_id,
    )
    result = await db.execute(stmt)
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    await db.delete(org)
    await db.commit()
    return {"message": "Organization deleted successfully", "id": org_id}
