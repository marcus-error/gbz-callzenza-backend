from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import require_admin
from app.db.database import get_db
from app.db.models import User, Business
from app.models.tenant import Tenant
from app.schemas.tenant import TenantCreate, TenantOut, TenantUpdate, TenantRoutingInfo

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


@router.get("/routing/{subdomain}", response_model=TenantRoutingInfo)
async def get_tenant_routing(
    subdomain: str,
    db: AsyncSession = Depends(get_db),
) -> TenantRoutingInfo:
    """Ingress Gateway routing resolver: checks which cluster/server hosts the given tenant subdomain."""
    clean_sub = subdomain.strip().lower()
    settings = get_settings()

    # Query Tenant table
    stmt = select(Tenant).where(
        (Tenant.subdomain == clean_sub) | (Tenant.slug == clean_sub)
    ).limit(1)
    res = await db.execute(stmt)
    tenant = res.scalar_one_or_none()

    if not tenant:
        # Fallback to Business
        b_stmt = select(Business).where(
            (Business.subdomain == clean_sub) | (Business.slug == clean_sub)
        ).limit(1)
        b_res = await db.execute(b_stmt)
        biz = b_res.scalar_one_or_none()
        if not biz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Tenant subdomain '{clean_sub}' not found")
        
        c_id = getattr(biz, "cluster_id", "default") or "default"
        return TenantRoutingInfo(
            tenant_id=biz.id,
            name=biz.name,
            slug=biz.slug or clean_sub,
            subdomain=clean_sub,
            custom_domain=getattr(biz, "custom_domain", None),
            cluster_id=c_id,
            server_url=getattr(biz, "server_url", None),
            region=getattr(biz, "region", "us-east") or "us-east",
            isolation_strategy=getattr(biz, "isolation_strategy", "SHARED_DATABASE") or "SHARED_DATABASE",
            is_local_cluster=(c_id == settings.cluster_id),
        )

    cluster_id = tenant.cluster_id or "default"
    return TenantRoutingInfo(
        tenant_id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        subdomain=tenant.subdomain or tenant.slug,
        custom_domain=tenant.custom_domain,
        cluster_id=cluster_id,
        server_url=tenant.server_url,
        region=tenant.region or "us-east",
        isolation_strategy=tenant.isolation_strategy or "SHARED_DATABASE",
        is_local_cluster=(cluster_id == settings.cluster_id),
    )


@router.get("/by-subdomain/{subdomain}", response_model=TenantOut)
async def get_tenant_by_subdomain(
    subdomain: str,
    db: AsyncSession = Depends(get_db),
) -> TenantOut:
    """Resolves tenant profile by subdomain or slug."""
    clean_sub = subdomain.strip().lower()
    stmt = select(Tenant).where(
        (Tenant.subdomain == clean_sub) | (Tenant.slug == clean_sub)
    ).limit(1)
    res = await db.execute(stmt)
    tenant = res.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found for subdomain")
    return tenant


@router.post("", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    payload: TenantCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TenantOut:
    clean_slug = payload.slug.strip().lower()
    clean_sub = (payload.subdomain or payload.slug).strip().lower()

    # Check for existing slug or subdomain
    existing = await db.execute(
        select(Tenant).where((Tenant.slug == clean_slug) | (Tenant.subdomain == clean_sub))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant with this slug or subdomain already exists")

    tenant = Tenant(
        name=payload.name.strip(),
        slug=clean_slug,
        subdomain=clean_sub,
        custom_domain=payload.custom_domain.strip().lower() if payload.custom_domain else None,
        status="active",
        cluster_id=(payload.cluster_id or "default").strip(),
        server_url=payload.server_url.strip() if payload.server_url else None,
        region=(payload.region or "us-east").strip(),
        isolation_strategy=payload.isolation_strategy or "SHARED_DATABASE",
        database_url=payload.database_url.strip() if payload.database_url else None,
        schema_name=payload.schema_name.strip() if payload.schema_name else None,
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.get("", response_model=List[TenantOut])
async def list_tenants(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> List[TenantOut]:
    stmt = select(Tenant)
    if status_filter:
        stmt = stmt.where(Tenant.status == status_filter)
    else:
        # By default exclude deleted unless explicitly asked
        stmt = stmt.where(Tenant.status != "deleted")
    stmt = stmt.order_by(Tenant.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TenantOut:
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


@router.put("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TenantOut:
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    if payload.slug is not None:
        new_slug = payload.slug.strip().lower()
        if new_slug != tenant.slug:
            existing = await db.execute(select(Tenant).where(Tenant.slug == new_slug, Tenant.id != tenant_id))
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already in use")
            tenant.slug = new_slug

    if payload.name is not None:
        tenant.name = payload.name.strip()
    if payload.status is not None:
        tenant.status = payload.status
    if payload.subdomain is not None:
        tenant.subdomain = payload.subdomain.strip().lower()
    if payload.custom_domain is not None:
        tenant.custom_domain = payload.custom_domain.strip().lower() if payload.custom_domain else None
    if payload.cluster_id is not None:
        tenant.cluster_id = payload.cluster_id.strip()
    if payload.server_url is not None:
        tenant.server_url = payload.server_url.strip() if payload.server_url else None
    if payload.region is not None:
        tenant.region = payload.region.strip()
    if payload.isolation_strategy is not None:
        tenant.isolation_strategy = payload.isolation_strategy
    if payload.database_url is not None:
        tenant.database_url = payload.database_url.strip() if payload.database_url else None
    if payload.schema_name is not None:
        tenant.schema_name = payload.schema_name.strip() if payload.schema_name else None

    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.delete("/{tenant_id}", status_code=status.HTTP_200_OK)
async def soft_delete_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict:
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    tenant.status = "deleted"
    db.add(tenant)
    await db.commit()
    return {"message": "Tenant deleted successfully", "id": tenant_id, "status": "deleted"}
