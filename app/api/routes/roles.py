from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User
from app.dependencies.tenant_context import get_current_tenant_id
from app.models.role import Permission, Role
from app.schemas.role import RoleCreate, RoleOut, RoleUpdate

router = APIRouter(prefix="/api/roles", tags=["roles"])


@router.get("", response_model=List[RoleOut])
async def list_roles(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> List[RoleOut]:
    """List system default roles plus any custom roles created for this tenant."""
    stmt = (
        select(Role)
        .where(or_(Role.tenant_id == tenant_id, Role.tenant_id.is_(None)))
        .order_by(Role.is_system.desc(), Role.name.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> RoleOut:
    role_name = payload.name.strip()
    # Check conflict in same tenant
    stmt = select(Role).where(
        Role.name.ilike(role_name),
        or_(Role.tenant_id == tenant_id, Role.tenant_id.is_(None)),
    )
    res = await db.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Role name already in use")

    role = Role(
        tenant_id=tenant_id,
        name=role_name,
        description=payload.description.strip(),
        is_system=False,
    )

    if payload.permission_ids:
        p_stmt = select(Permission).where(Permission.id.in_(payload.permission_ids))
        p_res = await db.execute(p_stmt)
        role.permissions = list(p_res.scalars().all())

    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


@router.get("/{role_id}", response_model=RoleOut)
async def get_role(
    role_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> RoleOut:
    stmt = select(Role).where(
        Role.id == role_id,
        or_(Role.tenant_id == tenant_id, Role.tenant_id.is_(None)),
    )
    res = await db.execute(stmt)
    role = res.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return role


@router.put("/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> RoleOut:
    stmt = select(Role).where(Role.id == role_id, Role.tenant_id == tenant_id)
    res = await db.execute(stmt)
    role = res.scalar_one_or_none()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found or is a protected system role",
        )

    if payload.name is not None:
        role.name = payload.name.strip()
    if payload.description is not None:
        role.description = payload.description.strip()

    if payload.permission_ids is not None:
        p_stmt = select(Permission).where(Permission.id.in_(payload.permission_ids))
        p_res = await db.execute(p_stmt)
        role.permissions = list(p_res.scalars().all())

    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


@router.delete("/{role_id}", status_code=status.HTTP_200_OK)
async def delete_role(
    role_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> dict:
    stmt = select(Role).where(Role.id == role_id, Role.tenant_id == tenant_id)
    res = await db.execute(stmt)
    role = res.scalar_one_or_none()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found or is a protected system role",
        )

    await db.delete(role)
    await db.commit()
    return {"message": "Role deleted successfully", "id": role_id}
