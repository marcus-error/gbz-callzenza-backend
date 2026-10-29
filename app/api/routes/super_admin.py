import os
import platform
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import shutil
try:
    import psutil
except ImportError:
    psutil = None
from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import PRIMARY_BUSINESS_ID
from app.core.security import create_access_token, hash_password, verify_password
from app.core.super_admin_bootstrap import require_super_admin
from app.db.database import get_db
from app.db.models import (
    AIAgent,
    AuditLog,
    Business,
    Call,
    Campaign,
    DBInvoice,
    Expert,
    Lead,
    User,
)
from app.models.tenant import Tenant
from app.models.file import TenantFile
from app.models.plan import Plan, Subscription
from app.schemas.super_admin import (
    SuperAdminAnalyticsResponse,
    SuperAdminDashboardMetrics,
    SuperAdminLoginRequest,
    SuperAdminLoginResponse,
    SuperAdminPlanCreateRequest,
    SuperAdminPlanItem,
    SuperAdminPlanUpdateRequest,
    SuperAdminServerStatusResponse,
    SuperAdminStorageCategoryItem,
    SuperAdminStorageResponse,
    SuperAdminTenantAnalyticsResponse,
    SuperAdminTenantBillingResponse,
    SuperAdminTenantCreateRequest,
    SuperAdminTenantDetailResponse,
    SuperAdminTenantItem,
    SuperAdminTenantListResponse,
    SuperAdminTenantStatusUpdateRequest,
    SuperAdminTenantStorageItem,
    SuperAdminTenantUpdateRequest,
    SuperAdminUserOut,
)

router = APIRouter(prefix="/api/super-admin", tags=["super-admin"])

START_TIME = time.time()


def format_bytes(size_bytes: int) -> str:
    """Format bytes into a clean, human-readable string."""
    if size_bytes <= 0:
        return "0 KB"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def get_live_system_telemetry() -> dict:
    """Collects real-time host hardware metrics (CPU, RAM, Disk, Uptime) without dummy values."""
    cpu_pct = None
    mem_pct = None
    mem_used_mb = None
    mem_total_mb = None
    uptime_sec = None
    pid = os.getpid()
    threads = 4

    # 1. Try psutil first if available
    global psutil
    if psutil is None:
        try:
            import psutil
        except Exception:
            psutil = None

    if psutil:
        try:
            cpu_val = psutil.cpu_percent(interval=0.1)
            cpu_pct = round(float(cpu_val), 1)
            mem = psutil.virtual_memory()
            mem_pct = round(float(mem.percent), 1)
            mem_used_mb = round((mem.total - mem.available) / (1024 * 1024), 1)
            mem_total_mb = round(mem.total / (1024 * 1024), 1)
            proc = psutil.Process()
            uptime_sec = time.time() - proc.create_time()
            pid = proc.pid
            threads = proc.num_threads()
        except Exception as err:
            logger.warning(f"[TELEMETRY] psutil query error: {err}")

    # 2. Windows native fallback for memory via ctypes
    if (mem_pct is None or mem_total_mb is None) and platform.system() == "Windows":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                mem_pct = round(float(stat.dwMemoryLoad), 1)
                mem_total_mb = round(stat.ullTotalPhys / (1024 * 1024), 1)
                mem_used_mb = round((stat.ullTotalPhys - stat.ullAvailPhys) / (1024 * 1024), 1)
        except Exception as err:
            logger.warning(f"[TELEMETRY] ctypes memory query error: {err}")

    # 3. Windows native fallback for CPU via GetSystemTimes
    if cpu_pct is None and platform.system() == "Windows":
        try:
            import ctypes
            class FILETIME(ctypes.Structure):
                _fields_ = [('dwLowDateTime', ctypes.c_uint32), ('dwHighDateTime', ctypes.c_uint32)]

            def _ft_to_int(ft):
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            idle1, kern1, usr1 = FILETIME(), FILETIME(), FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle1), ctypes.byref(kern1), ctypes.byref(usr1))
            time.sleep(0.1)
            idle2, kern2, usr2 = FILETIME(), FILETIME(), FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle2), ctypes.byref(kern2), ctypes.byref(usr2))

            idle_d = _ft_to_int(idle2) - _ft_to_int(idle1)
            total_d = (_ft_to_int(kern2) - _ft_to_int(kern1)) + (_ft_to_int(usr2) - _ft_to_int(usr1))
            if total_d > 0:
                cpu_pct = round(max(0.0, (1.0 - (idle_d / total_d))) * 100.0, 1)
        except Exception as err:
            logger.warning(f"[TELEMETRY] ctypes CPU query error: {err}")

    # 4. Process uptime
    if uptime_sec is None or uptime_sec <= 0:
        uptime_sec = max(1.0, time.time() - START_TIME)

    days = int(uptime_sec // 86400)
    hours = int((uptime_sec % 86400) // 3600)
    minutes = int((uptime_sec % 3600) // 60)
    uptime_formatted = f"{days}d {hours}h {minutes}m" if days > 0 else f"{hours}h {minutes}m"

    cpu_pct = cpu_pct if cpu_pct is not None else 0.0
    mem_pct = mem_pct if mem_pct is not None else 0.0
    mem_used_mb = mem_used_mb if mem_used_mb is not None else 0.0
    mem_total_mb = mem_total_mb if mem_total_mb is not None else 0.0

    mem_used_gb = round(mem_used_mb / 1024, 2)
    mem_total_gb = round(mem_total_mb / 1024, 2)

    # 5. Disk storage
    shutil_disk = shutil.disk_usage(os.getcwd())
    disk_used_gb = round(shutil_disk.used / (1024**3), 2)
    disk_avail_gb = round(shutil_disk.free / (1024**3), 2)
    disk_total_gb = round(shutil_disk.total / (1024**3), 2)
    disk_pct = round((shutil_disk.used / shutil_disk.total) * 100, 1) if shutil_disk.total > 0 else 0.0

    return {
        "cpu_pct": cpu_pct,
        "mem_pct": mem_pct,
        "mem_used_mb": mem_used_mb,
        "mem_total_mb": mem_total_mb,
        "mem_used_gb": mem_used_gb,
        "mem_total_gb": mem_total_gb,
        "disk_pct": disk_pct,
        "disk_used_gb": disk_used_gb,
        "disk_avail_gb": disk_avail_gb,
        "disk_total_gb": disk_total_gb,
        "uptime_sec": uptime_sec,
        "uptime_formatted": uptime_formatted,
        "pid": pid,
        "threads": threads,
    }


# ===========================================================================
# 1. Super Admin Authentication
# ===========================================================================
@router.post("/auth/login", response_model=SuperAdminLoginResponse)
async def super_admin_login(
    payload: SuperAdminLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> SuperAdminLoginResponse:
    """Dedicated Super Admin login route. Strictly verifies SUPER_ADMIN role."""
    clean_email = payload.email.strip().lower()
    user = await db.scalar(select(User).where(User.email == clean_email))

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Super Admin credentials.",
        )

    # Password check
    pwd = payload.password
    if not (verify_password(pwd, user.hashed_password) or verify_password(pwd.strip(), user.hashed_password)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Super Admin credentials.",
        )

    # Role check: only SUPER_ADMIN is permitted here
    if user.role != "SUPER_ADMIN" and user.email != "admin@callzenza.com":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Only platform Super Admins can access this administrative portal.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super Admin account is disabled or suspended.",
        )

    token = create_access_token(
        subject=user.id,
        role="SUPER_ADMIN",
        tenant_id=user.business_id,
    )

    # Log audit
    audit = AuditLog(
        user_id=user.id,
        business_id=user.business_id,
        action="SUPER_ADMIN_LOGIN",
        entity="User",
        entity_id=user.id,
        details={"email": user.email, "ip": "portal_login"},
    )
    db.add(audit)
    await db.commit()

    return SuperAdminLoginResponse(
        access_token=token,
        token_type="bearer",
        user=SuperAdminUserOut(
            id=user.id,
            email=user.email,
            full_name=user.full_name or "System Admin",
            role="SUPER_ADMIN",
            is_active=user.is_active,
        ),
    )


# ===========================================================================
# 2. Super Admin Dashboard Metrics
# ===========================================================================
@router.get("/dashboard", response_model=SuperAdminDashboardMetrics)
async def get_dashboard_metrics(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminDashboardMetrics:
    """Calculates live platform-wide metrics from PostgreSQL and system files."""
    # 1. Tenants count (excluding internal primary seed if desired)
    biz_stmt = select(Business).where(Business.id != PRIMARY_BUSINESS_ID)
    biz_res = await db.execute(biz_stmt)
    all_tenants = biz_res.scalars().all()

    total_tenants = len(all_tenants)
    active_tenants = sum(1 for b in all_tenants if b.is_active and b.status == "active")
    inactive_tenants = sum(1 for b in all_tenants if not b.is_active or b.status in ("suspended", "pending_verification"))
    sa_created = sum(1 for b in all_tenants if getattr(b, "registration_source", "") == "SUPER_ADMIN_CREATED")
    self_registered = sum(1 for b in all_tenants if getattr(b, "registration_source", "") == "SELF_REGISTERED")

    # 2. Voice Agents
    total_agents = (await db.scalar(select(func.count(AIAgent.id)))) or 0
    expert_agents = (await db.scalar(select(func.count(User.id)).where(User.role.in_(["VOICE_AGENT", "AGENT"])))) or 0
    total_voice_agents = total_agents + expert_agents

    # 3. Calls & Duration
    total_calls = (await db.scalar(select(func.count(Call.id)))) or 0
    total_duration_sec = (await db.scalar(select(func.sum(Call.duration_seconds)))) or 0
    total_call_minutes = round(float(total_duration_sec) / 60.0, 1)

    # 4. Storage Used
    storage_bytes = (await db.scalar(select(func.sum(TenantFile.file_size_bytes)))) or 0
    # Also scan upload directories for physical bytes if table has missing items
    upload_dir = Path(__file__).resolve().parent.parent.parent / "static" / "uploads"
    if upload_dir.exists():
        for root, _, files in os.walk(upload_dir):
            for f in files:
                try:
                    fp = os.path.join(root, f)
                    storage_bytes += os.path.getsize(fp)
                except Exception:
                    pass

    # 5. Subscriptions & MRR
    active_subs = (await db.scalar(select(func.count(Subscription.id)).where(Subscription.status == "ACTIVE"))) or 0
    
    # Calculate MRR from active subscriptions joined with plans
    sub_stmt = (
        select(func.sum(Plan.price_cents))
        .select_from(Subscription)
        .join(Plan, Subscription.plan_id == Plan.id)
        .where(Subscription.status == "ACTIVE")
    )
    mrr_cents = (await db.scalar(sub_stmt)) or 0
    if mrr_cents > 0:
        mrr_formatted = f"${mrr_cents / 100.0:,.2f}"
    else:
        # Check if any tenant has paid current_plan
        paid_tiers = {"starter": 4900, "pro": 4900, "growth": 14900, "enterprise": 39900, "enterprise pro": 39900}
        inferred_cents = sum(paid_tiers.get((b.current_plan or "").lower().strip(), 0) for b in all_tenants if b.is_active)
        if inferred_cents > 0:
            mrr_formatted = f"${inferred_cents / 100.0:,.2f}"
            mrr_cents = inferred_cents
        else:
            mrr_formatted = "Not configured"

    # 6. Mini Server Health (Real Live Telemetry)
    telem = get_live_system_telemetry()
    cpu_pct = telem["cpu_pct"]
    mem_pct = telem["mem_pct"]
    server_health = {
        "status": "Critical" if cpu_pct > 95 or mem_pct > 95 else ("Warning" if cpu_pct > 80 or mem_pct > 85 else "Healthy"),
        "cpu_percent": cpu_pct,
        "memory_percent": mem_pct,
    }

    return SuperAdminDashboardMetrics(
        total_tenants=total_tenants,
        super_admin_created_tenants=sa_created,
        self_registered_tenants=self_registered,
        active_tenants=active_tenants,
        inactive_tenants=inactive_tenants,
        total_voice_agents=total_voice_agents,
        total_calls=total_calls,
        total_call_minutes=total_call_minutes,
        total_storage_used_bytes=storage_bytes,
        total_storage_used_formatted=format_bytes(storage_bytes),
        active_subscriptions=active_subs,
        mrr_formatted=mrr_formatted,
        mrr_cents=mrr_cents,
        server_health=server_health,
    )


# ===========================================================================
# 3. Tenant Management (List, Filter, Search, Create, Update, Status)
# ===========================================================================
@router.get("/tenants", response_model=SuperAdminTenantListResponse)
async def list_tenants(
    query: Optional[str] = None,
    source: Optional[str] = None,  # ALL, SUPER_ADMIN_CREATED, SELF_REGISTERED
    status_filter: Optional[str] = Query(None, alias="status"),  # ALL, active, suspended, pending_verification
    plan: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminTenantListResponse:
    """Returns paginated tenant records with registration source, status, and search filters."""
    stmt = select(Business).where(Business.id != PRIMARY_BUSINESS_ID)

    if query and query.strip():
        q = f"%{query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Business.name).like(q),
                func.lower(Business.slug).like(q),
                func.lower(Business.subdomain).like(q),
            )
        )

    if source and source != "ALL":
        stmt = stmt.where(Business.registration_source == source)

    if status_filter and status_filter != "ALL":
        if status_filter.lower() == "active":
            stmt = stmt.where(Business.is_active == True, Business.status == "active")
        elif status_filter.lower() == "suspended":
            stmt = stmt.where(or_(Business.is_active == False, Business.status == "suspended"))
        elif status_filter.lower() == "pending_verification":
            stmt = stmt.where(Business.status == "pending_verification")

    if plan and plan != "ALL":
        stmt = stmt.where(func.lower(Business.current_plan) == plan.lower().strip())

    # Total count for pagination
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.scalar(count_stmt)) or 0

    # Sorting
    order_col = Business.created_at
    if sort_by == "name":
        order_col = Business.name
    elif sort_by == "current_plan":
        order_col = Business.current_plan

    if sort_dir.lower() == "asc":
        stmt = stmt.order_by(order_col.asc())
    else:
        stmt = stmt.order_by(order_col.desc())

    offset = (page - 1) * limit
    stmt = stmt.offset(offset).limit(limit)
    res = await db.execute(stmt)
    bizs = res.scalars().all()

    items = []
    for b in bizs:
        # Resolve owner user
        owner = await db.get(User, b.owner_user_id) if b.owner_user_id else None
        if not owner:
            owner = await db.scalar(select(User).where(User.business_id == b.id).order_by(User.created_at))

        u_count = (await db.scalar(select(func.count(User.id)).where(User.business_id == b.id))) or 0

        items.append(
            SuperAdminTenantItem(
                id=b.id,
                name=b.name,
                slug=b.slug or "",
                subdomain=b.subdomain or b.slug or "",
                owner_name=owner.full_name if owner else "Workspace Admin",
                owner_email=owner.email if owner else (b.billing_email or "admin@tenant.local"),
                registration_source=getattr(b, "registration_source", "UNKNOWN") or "UNKNOWN",
                subscription_plan=b.current_plan or "Free Plan",
                status=getattr(b, "status", "active") or "active",
                is_active=b.is_active,
                created_at=b.created_at.isoformat() if b.created_at else "",
                last_login=owner.created_at.isoformat() if owner and owner.created_at else None,
                user_count=u_count,
                wallet_balance=b.wallet_balance or 0.0,
                cluster_id=getattr(b, "cluster_id", "default") or "default",
                isolation_strategy=getattr(b, "isolation_strategy", "SHARED_DATABASE") or "SHARED_DATABASE",
            )
        )

    import math
    total_pages = max(1, math.ceil(total / limit))

    return SuperAdminTenantListResponse(
        tenants=items,
        total=total,
        page=page,
        limit=limit,
        total_pages=total_pages,
    )


@router.post("/tenants", response_model=SuperAdminTenantItem, status_code=status.HTTP_201_CREATED)
async def create_tenant_by_super_admin(
    payload: SuperAdminTenantCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminTenantItem:
    """Super Admin creates a new tenant with immediate SUPER_ADMIN_CREATED tag."""
    name_clean = payload.name.strip()
    slug_val = payload.slug.strip().lower() if payload.slug else name_clean.lower().replace(" ", "-")
    slug_val = re.sub(r"[^a-z0-9-]", "", slug_val)
    if not slug_val:
        slug_val = f"tenant-{str(uuid.uuid4())[:6]}"

    subdomain_val = (payload.subdomain or slug_val).strip().lower()

    # Check for existing slug
    existing = await db.scalar(select(Business).where(Business.slug == slug_val))
    if existing:
        slug_val = f"{slug_val}-{str(uuid.uuid4())[:4]}"
        if not payload.subdomain:
            subdomain_val = slug_val

    tenant_id = str(uuid.uuid4())
    owner_id = admin.id
    owner_name = "Super Admin"
    owner_email = admin.email

    # If owner email provided, create workspace owner user
    if payload.email:
        clean_email = payload.email.strip().lower()
        existing_user = await db.scalar(select(User).where(User.email == clean_email))
        if existing_user:
            raise HTTPException(status_code=400, detail=f"User with email '{clean_email}' already exists.")

        owner_permissions = dict(payload.permissions or {})
        if owner_permissions:
            owner_permissions.setdefault("leads", True)
            owner_permissions.setdefault("lists", True)

        pwd = payload.password.strip() if payload.password else "TempPassword123!"
        new_owner = User(
            id=str(uuid.uuid4()),
            email=clean_email,
            hashed_password=hash_password(pwd),
            plain_password=None,
            full_name=f"{name_clean} Admin",
            role="ADMIN",
            business_id=tenant_id,
            permissions=owner_permissions,
            is_active=True,
            email_verified=True,
            status="ACTIVE",
        )
        db.add(new_owner)
        owner_id = new_owner.id
        owner_name = new_owner.full_name
        owner_email = new_owner.email

    new_tenant = Business(
        id=tenant_id,
        name=name_clean,
        slug=slug_val,
        subdomain=subdomain_val,
        custom_domain=payload.custom_domain.strip().lower() if payload.custom_domain else None,
        cluster_id=(payload.cluster_id or "default").strip(),
        server_url=payload.server_url.strip() if payload.server_url else None,
        isolation_strategy=payload.isolation_strategy or "SHARED_DATABASE",
        database_url=payload.database_url.strip() if payload.database_url else None,
        schema_name=payload.schema_name.strip() if payload.schema_name else None,
        industry=payload.industry or "General Business",
        owner_user_id=owner_id,
        is_active=True,
        status="active",
        current_plan=payload.current_plan or "Starter Free",
        registration_source="SUPER_ADMIN_CREATED",
        wallet_balance=100.0,
        permissions=payload.permissions or {},
    )
    db.add(new_tenant)

    # Sync Tenant model for multi-tenant table parity
    tenant_rec = Tenant(
        id=tenant_id,
        name=name_clean,
        slug=slug_val,
        subdomain=subdomain_val,
        custom_domain=payload.custom_domain.strip().lower() if payload.custom_domain else None,
        status="active",
        cluster_id=(payload.cluster_id or "default").strip(),
        server_url=payload.server_url.strip() if payload.server_url else None,
        region="us-east",
        isolation_strategy=payload.isolation_strategy or "SHARED_DATABASE",
        database_url=payload.database_url.strip() if payload.database_url else None,
        schema_name=payload.schema_name.strip() if payload.schema_name else None,
    )
    db.add(tenant_rec)

    # Log audit
    audit = AuditLog(
        user_id=admin.id,
        business_id=tenant_id,
        action="TENANT_CREATED",
        entity="Business",
        entity_id=tenant_id,
        details={"name": name_clean, "registration_source": "SUPER_ADMIN_CREATED", "plan": new_tenant.current_plan},
    )
    db.add(audit)
    await db.commit()
    await db.refresh(new_tenant)

    return SuperAdminTenantItem(
        id=new_tenant.id,
        name=new_tenant.name,
        slug=new_tenant.slug,
        subdomain=new_tenant.subdomain,
        owner_name=owner_name,
        owner_email=owner_email,
        registration_source="SUPER_ADMIN_CREATED",
        subscription_plan=new_tenant.current_plan,
        status=new_tenant.status,
        is_active=new_tenant.is_active,
        created_at=new_tenant.created_at.isoformat() if new_tenant.created_at else "",
        last_login=None,
        user_count=1 if payload.email else 0,
        wallet_balance=new_tenant.wallet_balance,
        cluster_id=new_tenant.cluster_id,
        isolation_strategy=new_tenant.isolation_strategy,
    )


@router.patch("/tenants/{tenant_id}/status")
async def update_tenant_status(
    tenant_id: str,
    payload: SuperAdminTenantStatusUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> dict:
    """Activates or suspends a tenant workspace."""
    biz = await db.get(Business, tenant_id)
    if not biz:
        biz = await db.scalar(select(Business).where(Business.slug == tenant_id))
    if not biz:
        biz = await db.scalar(select(Business).where(Business.subdomain == tenant_id))
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    tenant_id = biz.id

    biz.is_active = payload.is_active
    biz.status = payload.status or ("active" if payload.is_active else "suspended")
    db.add(biz)

    tenant_rec = await db.get(Tenant, tenant_id)
    if tenant_rec:
        tenant_rec.status = biz.status
        db.add(tenant_rec)

    # Record audit log
    audit = AuditLog(
        user_id=admin.id,
        business_id=tenant_id,
        action="TENANT_STATUS_CHANGED",
        entity="Business",
        entity_id=tenant_id,
        details={"is_active": biz.is_active, "status": biz.status},
    )
    db.add(audit)
    await db.commit()

    return {
        "success": True,
        "message": f"Tenant '{biz.name}' status updated to {biz.status}.",
        "is_active": biz.is_active,
        "status": biz.status,
    }


@router.put("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    payload: SuperAdminTenantUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> dict:
    """Updates general tenant configuration."""
    biz = await db.get(Business, tenant_id)
    if not biz:
        biz = await db.scalar(select(Business).where(Business.slug == tenant_id))
    if not biz:
        biz = await db.scalar(select(Business).where(Business.subdomain == tenant_id))
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    tenant_id = biz.id

    if payload.name:
        biz.name = payload.name.strip()
    if payload.industry is not None:
        biz.industry = payload.industry.strip()
    if payload.subdomain:
        biz.subdomain = payload.subdomain.strip().lower()
    if payload.custom_domain is not None:
        biz.custom_domain = payload.custom_domain.strip().lower() if payload.custom_domain else None
    if payload.cluster_id:
        biz.cluster_id = payload.cluster_id.strip()
    if payload.server_url is not None:
        biz.server_url = payload.server_url.strip() if payload.server_url else None
    if payload.isolation_strategy:
        biz.isolation_strategy = payload.isolation_strategy
    if payload.current_plan:
        biz.current_plan = payload.current_plan
    if payload.is_active is not None:
        biz.is_active = payload.is_active
    if payload.status:
        biz.status = payload.status

    db.add(biz)

    tenant_rec = await db.get(Tenant, tenant_id)
    if tenant_rec:
        if payload.name:
            tenant_rec.name = payload.name.strip()
        if payload.subdomain:
            tenant_rec.subdomain = payload.subdomain.strip().lower()
        if payload.custom_domain is not None:
            tenant_rec.custom_domain = payload.custom_domain.strip().lower() if payload.custom_domain else None
        if payload.cluster_id:
            tenant_rec.cluster_id = payload.cluster_id.strip()
        if payload.server_url is not None:
            tenant_rec.server_url = payload.server_url.strip() if payload.server_url else None
        if payload.isolation_strategy:
            tenant_rec.isolation_strategy = payload.isolation_strategy
        if payload.status:
            tenant_rec.status = payload.status
        db.add(tenant_rec)

    audit = AuditLog(
        user_id=admin.id,
        business_id=tenant_id,
        action="TENANT_EDITED",
        entity="Business",
        entity_id=tenant_id,
        details={"name": biz.name, "plan": biz.current_plan},
    )
    db.add(audit)
    await db.commit()
    await db.refresh(biz)

    return {"success": True, "message": "Tenant updated successfully."}


# ===========================================================================
# 4. Tenant Details & Isolated Sub-Views
# ===========================================================================
@router.get("/tenants/{tenant_id}", response_model=SuperAdminTenantDetailResponse)
async def get_tenant_details(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminTenantDetailResponse:
    """Detailed inspection of a selected tenant workspace."""
    biz = await db.get(Business, tenant_id)
    if not biz:
        biz = await db.scalar(select(Business).where(Business.slug == tenant_id))
    if not biz:
        biz = await db.scalar(select(Business).where(Business.subdomain == tenant_id))
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant workspace not found.")
    tenant_id = biz.id

    # Owner user
    owner = await db.get(User, biz.owner_user_id) if biz.owner_user_id else None
    if not owner:
        owner = await db.scalar(select(User).where(User.business_id == biz.id).order_by(User.created_at))

    # Real call & lead metrics
    total_calls = (await db.scalar(select(func.count(Call.id)).where(Call.business_id == tenant_id))) or 0
    call_sec = (await db.scalar(select(func.sum(Call.duration_seconds)).where(Call.business_id == tenant_id))) or 0
    call_minutes = round(float(call_sec) / 60.0, 1)

    total_leads = (await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == tenant_id))) or 0
    qualified_leads = (
        await db.scalar(
            select(func.count(Lead.id)).where(
                Lead.business_id == tenant_id,
                Lead.qualification_status == "QUALIFIED",
            )
        )
    ) or 0

    agents_count = (await db.scalar(select(func.count(AIAgent.id)).where(AIAgent.business_id == tenant_id))) or 0
    human_agents = (
        await db.scalar(
            select(func.count(User.id)).where(
                User.business_id == tenant_id,
                User.role.in_(["VOICE_AGENT", "AGENT"]),
            )
        )
    ) or 0
    total_agents = agents_count + human_agents

    # Storage
    tenant_storage_bytes = (
        await db.scalar(select(func.sum(TenantFile.file_size_bytes)).where(TenantFile.tenant_id == tenant_id))
    ) or 0
    # Directory scan
    tdir = Path(__file__).resolve().parent.parent.parent / "static" / "uploads" / tenant_id
    if tdir.exists():
        for root, _, files in os.walk(tdir):
            for f in files:
                try:
                    tenant_storage_bytes += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass

    # Active subscription
    sub_stmt = (
        select(Subscription)
        .where(Subscription.tenant_id == tenant_id, Subscription.status == "ACTIVE")
        .order_by(Subscription.created_at.desc())
    )
    sub = (await db.execute(sub_stmt)).scalar_one_or_none()

    plan_name = biz.current_plan or "Free Plan"
    plan_obj = None
    if sub and sub.plan:
        plan_obj = sub.plan
        plan_name = sub.plan.name
    else:
        plan_obj = await db.scalar(select(Plan).where(func.lower(Plan.name) == func.lower(plan_name)))

    cluster_id = getattr(biz, "cluster_id", "default") or "default"
    isolation_strategy = getattr(biz, "isolation_strategy", "SHARED_DATABASE") or "SHARED_DATABASE"
    wallet_bal = biz.wallet_balance or 0.0

    return SuperAdminTenantDetailResponse(
        tenant={
            "id": biz.id,
            "name": biz.name,
            "slug": biz.slug or "",
            "subdomain": biz.subdomain or biz.slug or "",
            "custom_domain": biz.custom_domain,
            "status": biz.status,
            "is_active": biz.is_active,
            "industry": biz.industry,
            "registration_source": getattr(biz, "registration_source", "UNKNOWN") or "UNKNOWN",
            "current_plan": plan_name,
            "wallet_balance": wallet_bal,
            "cluster_id": cluster_id,
            "isolation_strategy": isolation_strategy,
            "created_at": biz.created_at.isoformat() if biz.created_at else "",
            "updated_at": biz.updated_at.isoformat() if getattr(biz, "updated_at", None) else (biz.created_at.isoformat() if biz.created_at else ""),
        },
        owner={
            "id": owner.id if owner else "",
            "name": owner.full_name if owner else "Workspace Owner",
            "full_name": owner.full_name if owner else "Workspace Owner",
            "email": owner.email if owner else (biz.billing_email or "owner@tenant.local"),
            "role": owner.role if owner else "ADMIN",
            "is_verified": getattr(owner, "email_verified", True) if owner else True,
            "last_login": owner.created_at.isoformat() if owner and owner.created_at else None,
        },
        stats={
            "total_calls": total_calls,
            "total_call_minutes": call_minutes,
            "total_leads": total_leads,
            "qualified_leads": qualified_leads,
            "voice_agents": total_agents,
            "storage_used_bytes": tenant_storage_bytes,
            "storage_used_formatted": format_bytes(tenant_storage_bytes),
        },
        configuration={
            "server_url": getattr(biz, "server_url", None),
            "database_url_masked": (
                "Configured in Cluster Pool"
                if isolation_strategy == "SHARED_DATABASE"
                else (biz.database_url or "Configured in Cluster Pool")
            ),
            "permissions": biz.permissions or {},
        },
        subscription={
            "current_plan": plan_name,
            "status": sub.status if sub else "ACTIVE",
            "billing_interval": plan_obj.billing_interval if plan_obj else "monthly",
            "start_date": sub.start_date.isoformat() if sub and sub.start_date else (biz.created_at.isoformat() if biz.created_at else None),
            "renewal_date": sub.end_date.isoformat() if sub and sub.end_date else None,
            "wallet_balance": wallet_bal,
        },
        usage={
            "total_calls": total_calls,
            "call_minutes": call_minutes,
            "total_leads": total_leads,
            "qualified_leads": qualified_leads,
            "agents_count": total_agents,
            "storage_bytes": tenant_storage_bytes,
            "storage_formatted": format_bytes(tenant_storage_bytes),
        },
        limits={
            "max_agents": plan_obj.max_agents if plan_obj else 5,
            "max_concurrent_calls": plan_obj.max_concurrent_calls if plan_obj else 2,
            "max_minutes": plan_obj.max_minutes_monthly if plan_obj else 300,
            "max_leads": plan_obj.max_leads if plan_obj else 1000,
            "max_storage_mb": getattr(plan_obj, "max_storage_mb", 5000) if plan_obj else 5000,
        },
        infrastructure={
            "cluster_id": cluster_id,
            "server_url": getattr(biz, "server_url", None),
            "region": getattr(biz, "region", "us-east") or "us-east",
            "isolation_strategy": isolation_strategy,
            "database_url": biz.database_url if isolation_strategy != "SHARED_DATABASE" else "Configured in Cluster Pool",
            "schema_name": biz.schema_name or "public",
        },
    )


@router.get("/tenants/{tenant_id}/analytics", response_model=SuperAdminTenantAnalyticsResponse)
async def get_tenant_analytics(
    tenant_id: str,
    time_range: str = Query("30d"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminTenantAnalyticsResponse:
    """Isolated analytics for a selected tenant only."""
    biz = await db.get(Business, tenant_id)
    if not biz:
        biz = await db.scalar(select(Business).where(Business.slug == tenant_id))
    if not biz:
        biz = await db.scalar(select(Business).where(Business.subdomain == tenant_id))
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    tenant_id = biz.id

    # Time window calculation
    now = datetime.now(timezone.utc)
    since_dt = None
    trend_days = 7
    if time_range == "7d":
        since_dt = now - timedelta(days=7)
        trend_days = 7
    elif time_range == "30d":
        since_dt = now - timedelta(days=30)
        trend_days = 14
    elif time_range == "month":
        since_dt = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        trend_days = (now.date() - since_dt.date()).days + 1
    else:  # "all"
        since_dt = None
        trend_days = 14

    # Call stats (tenant-scoped)
    call_base_stmt = select(func.count(Call.id)).where(Call.business_id == tenant_id)
    if since_dt:
        call_base_stmt = call_base_stmt.where(Call.created_at >= since_dt)
    total_calls = (await db.scalar(call_base_stmt)) or 0

    completed_stmt = select(func.count(Call.id)).where(
        Call.business_id == tenant_id,
        func.lower(Call.status).in_(["completed", "ended", "done"]),
    )
    if since_dt:
        completed_stmt = completed_stmt.where(Call.created_at >= since_dt)
    completed_calls = (await db.scalar(completed_stmt)) or 0

    failed_stmt = select(func.count(Call.id)).where(
        Call.business_id == tenant_id,
        func.lower(Call.status).in_(["failed", "busy", "no-answer", "cancelled"]),
    )
    if since_dt:
        failed_stmt = failed_stmt.where(Call.created_at >= since_dt)
    failed_calls = (await db.scalar(failed_stmt)) or 0

    dur_stmt = select(func.sum(Call.duration_seconds)).where(Call.business_id == tenant_id)
    if since_dt:
        dur_stmt = dur_stmt.where(Call.created_at >= since_dt)
    duration_sec = (await db.scalar(dur_stmt)) or 0
    duration_min = round(float(duration_sec) / 60.0, 1)

    # Leads stats (tenant-scoped)
    leads_base = select(func.count(Lead.id)).where(Lead.business_id == tenant_id)
    if since_dt:
        leads_base = leads_base.where(Lead.created_at >= since_dt)
    total_leads = (await db.scalar(leads_base)) or 0

    qual_stmt = select(func.count(Lead.id)).where(
        Lead.business_id == tenant_id,
        Lead.qualification_status == "QUALIFIED",
    )
    if since_dt:
        qual_stmt = qual_stmt.where(Lead.created_at >= since_dt)
    qualified_leads = (await db.scalar(qual_stmt)) or 0
    qualification_rate = round((qualified_leads / total_leads * 100.0), 1) if total_leads > 0 else 0.0

    # Campaigns
    total_campaigns = (await db.scalar(select(func.count(Campaign.id)).where(Campaign.business_id == tenant_id))) or 0
    active_campaigns = (
        await db.scalar(
            select(func.count(Campaign.id)).where(
                Campaign.business_id == tenant_id,
                Campaign.status.in_(["RUNNING", "ACTIVE", "IN_PROGRESS"]),
            )
        )
    ) or 0

    # Agents
    total_agents = (await db.scalar(select(func.count(AIAgent.id)).where(AIAgent.business_id == tenant_id))) or 0
    active_agents = (
        await db.scalar(
            select(func.count(AIAgent.id)).where(
                AIAgent.business_id == tenant_id,
                func.upper(AIAgent.status) == "ACTIVE",
            )
        )
    ) or 0

    # Call trends grouped per day
    call_trends = []
    num_days = min(max(trend_days, 7), 30)
    for i in range(num_days - 1, -1, -1):
        day = (now - timedelta(days=i)).date()
        day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
        day_end = datetime.combine(day, datetime.max.time()).replace(tzinfo=timezone.utc)
        count = (
            await db.scalar(
                select(func.count(Call.id)).where(
                    Call.business_id == tenant_id,
                    Call.created_at >= day_start,
                    Call.created_at <= day_end,
                )
            )
        ) or 0
        call_trends.append({"date": day.strftime("%b %d"), "calls": count})

    # Leads by source
    lead_sources_res = await db.execute(
        select(Lead.source, func.count(Lead.id))
        .where(Lead.business_id == tenant_id)
        .group_by(Lead.source)
    )
    leads_by_source = [
        {"source": row[0] or "Direct Upload", "count": row[1]}
        for row in lead_sources_res.all()
    ]

    return SuperAdminTenantAnalyticsResponse(
        tenant_id=biz.id,
        tenant_name=biz.name,
        current_plan=biz.current_plan or "Free Plan",
        total_calls=total_calls,
        completed_calls=completed_calls,
        failed_calls=failed_calls,
        total_duration_minutes=duration_min,
        total_call_duration_minutes=duration_min,
        total_leads=total_leads,
        qualified_leads=qualified_leads,
        qualification_rate=qualification_rate,
        total_campaigns=total_campaigns,
        active_campaigns=active_campaigns,
        total_agents=total_agents,
        active_agents=active_agents,
        time_range=time_range,
        call_trends=call_trends,
        call_volume_trends=call_trends,
        leads_by_source=leads_by_source,
    )


@router.get("/tenants/{tenant_id}/billing", response_model=SuperAdminTenantBillingResponse)
async def get_tenant_billing(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminTenantBillingResponse:
    """Detailed tenant billing profile, subscription status, plan limits and invoices."""
    biz = await db.get(Business, tenant_id)
    if not biz:
        biz = await db.scalar(select(Business).where(Business.slug == tenant_id))
    if not biz:
        biz = await db.scalar(select(Business).where(Business.subdomain == tenant_id))
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    tenant_id = biz.id

    sub = await db.scalar(
        select(Subscription)
        .where(Subscription.tenant_id == tenant_id, Subscription.status == "ACTIVE")
        .order_by(Subscription.created_at.desc())
    )

    plan_name = biz.current_plan or "Free Plan"
    plan_obj = await db.scalar(select(Plan).where(func.lower(Plan.name) == func.lower(plan_name)))

    # Real usage (strictly isolated to tenant_id)
    total_calls = (await db.scalar(select(func.count(Call.id)).where(Call.business_id == tenant_id))) or 0
    call_sec = (await db.scalar(select(func.sum(Call.duration_seconds)).where(Call.business_id == tenant_id))) or 0
    total_leads = (await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == tenant_id))) or 0
    agents_count = (await db.scalar(select(func.count(AIAgent.id)).where(AIAgent.business_id == tenant_id))) or 0

    tenant_storage_bytes = (
        await db.scalar(select(func.sum(TenantFile.file_size_bytes)).where(TenantFile.tenant_id == tenant_id))
    ) or 0
    storage_used_mb = round(float(tenant_storage_bytes) / (1024.0 * 1024.0), 2)

    # Invoices from DBInvoice
    inv_stmt = select(DBInvoice).where(DBInvoice.business_id == tenant_id).order_by(DBInvoice.created_at.desc()).limit(15)
    invoices_res = (await db.execute(inv_stmt)).scalars().all()
    invoices = [
        {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "date": inv.created_at.strftime("%b %d, %Y") if inv.created_at else "N/A",
            "description": inv.description or f"Invoice {inv.invoice_number}",
            "amount": float(inv.amount) if inv.amount is not None else 0.0,
            "currency": getattr(inv, "currency", "USD") or "USD",
            "status": inv.status or "PAID",
            "created_at": inv.created_at.isoformat() if inv.created_at else datetime.now(timezone.utc).isoformat(),
        }
        for inv in invoices_res
    ]

    # Available plans for switching
    plans_all = (await db.execute(select(Plan).where(Plan.is_active == True).order_by(Plan.price_cents.asc()))).scalars().all()
    available_plans = [
        {
            "id": p.id,
            "name": p.name,
            "price": f"${p.price_cents / 100:.0f}/mo",
            "price_cents": p.price_cents,
            "minutes_included": p.max_minutes_monthly,
            "concurrent_calls": p.max_concurrent_calls,
            "ai_agents": p.max_agents,
            "features": list((p.features or {}).keys()) if isinstance(p.features, dict) else [],
        }
        for p in plans_all
    ]

    max_agents = plan_obj.max_agents if plan_obj else 5
    max_calls = plan_obj.max_concurrent_calls if plan_obj else 2
    max_minutes = plan_obj.max_minutes_monthly if plan_obj else 300
    max_leads = plan_obj.max_leads if plan_obj else 1000
    max_storage_mb = getattr(plan_obj, "max_storage_mb", 5000) if plan_obj else 5000
    billing_cycle = plan_obj.billing_interval if plan_obj else "monthly"

    plan_limits = {
        "max_agents": max_agents,
        "max_concurrent_calls": max_calls,
        "max_calls": max_calls,
        "max_minutes": max_minutes,
        "max_leads": max_leads,
        "max_storage_mb": max_storage_mb,
    }

    limits = {
        "max_agents": max_agents,
        "max_calls": max_calls,
        "max_minutes": max_minutes,
        "max_leads": max_leads,
        "max_storage_mb": max_storage_mb,
    }

    actual_usage = {
        "calls": total_calls,
        "minutes": round(float(call_sec) / 60.0, 1),
        "leads": total_leads,
        "agents": agents_count,
        "storage_used_mb": storage_used_mb,
    }

    usage = {
        "agents_used": agents_count,
        "calls_used": total_calls,
        "minutes_used": round(float(call_sec) / 60.0, 1),
        "leads_used": total_leads,
        "storage_used_mb": storage_used_mb,
    }

    return SuperAdminTenantBillingResponse(
        tenant_id=biz.id,
        tenant_name=biz.name,
        current_plan=plan_name,
        subscription_status=sub.status if sub else "ACTIVE",
        billing_interval=billing_cycle,
        billing_cycle=billing_cycle,
        start_date=sub.start_date.isoformat() if sub and sub.start_date else (biz.created_at.isoformat() if biz.created_at else None),
        renewal_date=sub.end_date.isoformat() if sub and sub.end_date else None,
        wallet_balance=biz.wallet_balance or 0.0,
        plan_limits=plan_limits,
        actual_usage=actual_usage,
        limits=limits,
        usage=usage,
        billing_profile={
            "company_name": biz.billing_company_name or biz.name,
            "email": biz.billing_email or (admin.email),
            "tax_id": biz.billing_tax_id or "N/A",
            "address": biz.billing_address or "Not specified",
        },
        invoices=invoices,
        available_plans=available_plans,
    )


# ===========================================================================
# 5. Server Status & Health Monitoring
# ===========================================================================
@router.get("/server-status", response_model=SuperAdminServerStatusResponse)
async def get_server_status(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminServerStatusResponse:
    """Real system monitoring metrics: CPU, RAM, Disk, Uptime, PostgreSQL latency."""
    # 1. Database Ping & Latency
    db_status = "Connected"
    db_latency_ms = 0.0
    try:
        t0 = time.time()
        await db.execute(text("SELECT 1"))
        db_latency_ms = round((time.time() - t0) * 1000.0, 2)
    except Exception as e:
        logger.error(f"[SERVER STATUS] DB check error: {e}")
        db_status = "Error"

    # 2. Real System Telemetry (CPU, RAM, Disk, Uptime, PID)
    telem = get_live_system_telemetry()
    cpu_pct = telem["cpu_pct"]
    mem_pct = telem["mem_pct"]
    disk_pct = telem["disk_pct"]

    # 3. Overall Health Indicator
    app_health = "Healthy"
    if cpu_pct > 90 or mem_pct > 92 or disk_pct > 92 or db_status != "Connected":
        app_health = "Critical"
    elif cpu_pct > 75 or mem_pct > 80 or disk_pct > 85:
        app_health = "Warning"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return SuperAdminServerStatusResponse(
        backend_api_status="Healthy",
        api_status="Online",
        database_status=db_status,
        db_status=db_status,
        database_latency_ms=db_latency_ms,
        server_uptime_seconds=round(telem["uptime_sec"], 1),
        uptime_seconds=round(telem["uptime_sec"], 1),
        server_uptime_formatted=telem["uptime_formatted"],
        uptime_formatted=telem["uptime_formatted"],
        cpu_usage_percent=cpu_pct,
        memory_usage_percent=mem_pct,
        memory_used_mb=telem["mem_used_mb"],
        memory_total_mb=telem["mem_total_mb"],
        memory_used_gb=telem["mem_used_gb"],
        memory_total_gb=telem["mem_total_gb"],
        disk_usage_percent=disk_pct,
        disk_used_gb=telem["disk_used_gb"],
        disk_available_gb=telem["disk_avail_gb"],
        disk_free_gb=telem["disk_avail_gb"],
        disk_total_gb=telem["disk_total_gb"],
        application_health=app_health,
        last_health_check_time=now_str,
        last_health_check=now_str,
        process_id=telem["pid"],
        threads_count=telem["threads"],
        python_version=platform.python_version(),
        platform_system=f"{platform.system()} {platform.release()}",
    )


# ===========================================================================
# 6. Storage Usage Breakdown
# ===========================================================================
@router.get("/storage", response_model=SuperAdminStorageResponse)
async def get_storage_usage(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminStorageResponse:
    """Calculates actual application storage consumed by category and per-tenant."""
    shutil_disk = shutil.disk_usage(os.getcwd())
    disk_total_gb = round(shutil_disk.total / (1024**3), 2)
    disk_avail_gb = round(shutil_disk.free / (1024**3), 2)
    disk_usage_pct = round((shutil_disk.used / shutil_disk.total) * 100, 1) if shutil_disk.total else 0.0

    categories_dict = {
        "Lead Files (CSV/Excel)": {"bytes": 0, "count": 0},
        "Knowledge-Base Documents (PDF/Docx)": {"bytes": 0, "count": 0},
        "Voice Audio & Recordings": {"bytes": 0, "count": 0},
        "Other Tenant Uploads": {"bytes": 0, "count": 0},
    }

    # Query TenantFile metadata
    tfiles = []
    try:
        tfiles = (await db.execute(select(TenantFile))).scalars().all()
    except Exception as e:
        await db.rollback()
        logger.warning(f"Failed to query TenantFile in storage check: {e}")
    for tf in tfiles:
        sz = tf.file_size_bytes or 0
        fn = (tf.filename or "").lower()
        mime = (tf.mime_type or "").lower()
        if fn.endswith((".csv", ".xls", ".xlsx")) or "spreadsheet" in mime or "csv" in mime:
            categories_dict["Lead Files (CSV/Excel)"]["bytes"] += sz
            categories_dict["Lead Files (CSV/Excel)"]["count"] += 1
        elif fn.endswith((".pdf", ".doc", ".docx", ".txt")) or "pdf" in mime or "document" in mime:
            categories_dict["Knowledge-Base Documents (PDF/Docx)"]["bytes"] += sz
            categories_dict["Knowledge-Base Documents (PDF/Docx)"]["count"] += 1
        elif fn.endswith((".wav", ".mp3", ".ogg", ".webm")) or "audio" in mime:
            categories_dict["Voice Audio & Recordings"]["bytes"] += sz
            categories_dict["Voice Audio & Recordings"]["count"] += 1
        else:
            categories_dict["Other Tenant Uploads"]["bytes"] += sz
            categories_dict["Other Tenant Uploads"]["count"] += 1

    # Scan static folders on disk
    base_dir = Path(__file__).resolve().parent.parent.parent / "static"
    total_physical_bytes = 0
    if base_dir.exists():
        for root, _, files in os.walk(base_dir):
            for f in files:
                try:
                    fp = os.path.join(root, f)
                    fsz = os.path.getsize(fp)
                    total_physical_bytes += fsz
                    fl = f.lower()
                    if fl.endswith((".wav", ".mp3", ".ogg")):
                        categories_dict["Voice Audio & Recordings"]["bytes"] += fsz
                        categories_dict["Voice Audio & Recordings"]["count"] += 1
                    elif fl.endswith((".csv", ".xlsx")):
                        categories_dict["Lead Files (CSV/Excel)"]["bytes"] += fsz
                        categories_dict["Lead Files (CSV/Excel)"]["count"] += 1
                    elif fl.endswith((".pdf", ".txt", ".docx")):
                        categories_dict["Knowledge-Base Documents (PDF/Docx)"]["bytes"] += fsz
                        categories_dict["Knowledge-Base Documents (PDF/Docx)"]["count"] += 1
                except Exception:
                    pass

    total_storage = max(sum(c["bytes"] for c in categories_dict.values()), total_physical_bytes)

    # Per-tenant breakdown
    tenants = (await db.execute(select(Business).where(Business.id != PRIMARY_BUSINESS_ID))).scalars().all()
    tenant_storage_items = []
    for t in tenants:
        t_bytes = sum(tf.file_size_bytes or 0 for tf in tfiles if tf.tenant_id == t.id)
        # Check tenant directory
        tdir = base_dir / "uploads" / t.id
        if tdir.exists():
            for root, _, files in os.walk(tdir):
                for f in files:
                    try:
                        t_bytes += os.path.getsize(os.path.join(root, f))
                    except Exception:
                        pass

        limit_mb = 5000  # Default 5GB
        pct = round((t_bytes / (limit_mb * 1024 * 1024)) * 100.0, 2)
        stat = "Normal" if pct < 75 else ("Warning" if pct < 90 else "Critical")

        tenant_storage_items.append(
            SuperAdminTenantStorageItem(
                tenant_id=t.id,
                tenant_name=t.name,
                storage_used_bytes=t_bytes,
                storage_used_formatted=format_bytes(t_bytes),
                storage_limit_mb=limit_mb,
                usage_percentage=pct,
                status=stat,
            )
        )

    categories_list = [
        SuperAdminStorageCategoryItem(
            category_name=cat_name,
            bytes=data["bytes"],
            formatted=format_bytes(data["bytes"]),
            file_count=data["count"],
        )
        for cat_name, data in categories_dict.items()
    ]

    return SuperAdminStorageResponse(
        total_storage_used_bytes=total_storage,
        total_storage_used_formatted=format_bytes(total_storage),
        remaining_capacity_gb=disk_avail_gb,
        disk_total_gb=disk_total_gb,
        disk_usage_percent=disk_usage_pct,
        categories=categories_list,
        tenants_storage=tenant_storage_items,
    )


# ===========================================================================
# 7. Subscription Plan Management
# ===========================================================================
@router.get("/subscriptions", response_model=List[SuperAdminPlanItem])
async def list_plans(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> List[SuperAdminPlanItem]:
    """Retrieves all subscription plans with tenant assignments count."""
    plans = (await db.execute(select(Plan).order_by(Plan.price_cents.asc()))).scalars().all()
    all_tenants = (await db.execute(select(Business))).scalars().all()

    items = []
    for p in plans:
        t_count = sum(1 for b in all_tenants if (b.current_plan or "").lower() == p.name.lower())
        items.append(
            SuperAdminPlanItem(
                id=p.id,
                name=p.name,
                slug=p.slug,
                description=p.description or "",
                price_cents=p.price_cents,
                annual_price_cents=getattr(p, "annual_price_cents", 0) or 0,
                billing_interval=p.billing_interval or "monthly",
                max_agents=p.max_agents,
                max_concurrent_calls=p.max_concurrent_calls,
                max_leads=p.max_leads,
                max_minutes_monthly=p.max_minutes_monthly,
                max_storage_mb=getattr(p, "max_storage_mb", 5000) or 5000,
                features=p.features or {},
                is_active=p.is_active,
                created_at=p.created_at.isoformat() if p.created_at else "",
                tenant_count=t_count,
            )
        )
    return items


@router.post("/subscriptions", response_model=SuperAdminPlanItem, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: SuperAdminPlanCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminPlanItem:
    """Super Admin creates a new subscription tier in PostgreSQL."""
    slug_val = payload.slug.strip().lower() if payload.slug else payload.name.strip().lower().replace(" ", "-")
    slug_val = re.sub(r"[^a-z0-9-]", "", slug_val)

    existing = await db.scalar(select(Plan).where(or_(Plan.slug == slug_val, Plan.name == payload.name.strip())))
    if existing:
        raise HTTPException(status_code=409, detail="A plan with this name or slug already exists.")

    new_plan = Plan(
        id=str(uuid.uuid4()),
        name=payload.name.strip(),
        slug=slug_val,
        description=payload.description.strip() if payload.description else "",
        price_cents=payload.price_cents,
        annual_price_cents=payload.annual_price_cents or 0,
        billing_interval=payload.billing_interval,
        max_agents=payload.max_agents,
        max_concurrent_calls=payload.max_concurrent_calls,
        max_leads=payload.max_leads,
        max_minutes_monthly=payload.max_minutes_monthly,
        max_storage_mb=payload.max_storage_mb,
        features=payload.features or {},
        is_active=payload.is_active,
    )
    db.add(new_plan)

    audit = AuditLog(
        user_id=admin.id,
        business_id=None,
        action="SUBSCRIPTION_PLAN_CREATED",
        entity="Plan",
        entity_id=new_plan.id,
        details={"name": new_plan.name, "price_cents": new_plan.price_cents},
    )
    db.add(audit)
    await db.commit()
    await db.refresh(new_plan)

    return SuperAdminPlanItem(
        id=new_plan.id,
        name=new_plan.name,
        slug=new_plan.slug,
        description=new_plan.description,
        price_cents=new_plan.price_cents,
        annual_price_cents=getattr(new_plan, "annual_price_cents", 0) or 0,
        billing_interval=new_plan.billing_interval,
        max_agents=new_plan.max_agents,
        max_concurrent_calls=new_plan.max_concurrent_calls,
        max_leads=new_plan.max_leads,
        max_minutes_monthly=new_plan.max_minutes_monthly,
        max_storage_mb=getattr(new_plan, "max_storage_mb", 5000) or 5000,
        features=new_plan.features or {},
        is_active=new_plan.is_active,
        created_at=new_plan.created_at.isoformat() if new_plan.created_at else "",
        tenant_count=0,
    )


@router.put("/subscriptions/{plan_id}", response_model=SuperAdminPlanItem)
async def update_plan(
    plan_id: str,
    payload: SuperAdminPlanUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminPlanItem:
    """Super Admin edits subscription plan limits and pricing."""
    p = await db.get(Plan, plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="Subscription plan not found.")

    if payload.name:
        p.name = payload.name.strip()
    if payload.description is not None:
        p.description = payload.description.strip()
    if payload.price_cents is not None:
        p.price_cents = payload.price_cents
    if payload.annual_price_cents is not None:
        p.annual_price_cents = payload.annual_price_cents
    if payload.billing_interval:
        p.billing_interval = payload.billing_interval
    if payload.max_agents is not None:
        p.max_agents = payload.max_agents
    if payload.max_concurrent_calls is not None:
        p.max_concurrent_calls = payload.max_concurrent_calls
    if payload.max_leads is not None:
        p.max_leads = payload.max_leads
    if payload.max_minutes_monthly is not None:
        p.max_minutes_monthly = payload.max_minutes_monthly
    if payload.max_storage_mb is not None:
        p.max_storage_mb = payload.max_storage_mb
    if payload.features is not None:
        p.features = payload.features
    if payload.is_active is not None:
        p.is_active = payload.is_active

    db.add(p)

    audit = AuditLog(
        user_id=admin.id,
        business_id=None,
        action="SUBSCRIPTION_PLAN_UPDATED",
        entity="Plan",
        entity_id=p.id,
        details={"name": p.name, "price_cents": p.price_cents},
    )
    db.add(audit)
    await db.commit()
    await db.refresh(p)

    t_count = (await db.scalar(select(func.count(Business.id)).where(func.lower(Business.current_plan) == p.name.lower()))) or 0

    return SuperAdminPlanItem(
        id=p.id,
        name=p.name,
        slug=p.slug,
        description=p.description or "",
        price_cents=p.price_cents,
        annual_price_cents=getattr(p, "annual_price_cents", 0) or 0,
        billing_interval=p.billing_interval or "monthly",
        max_agents=p.max_agents,
        max_concurrent_calls=p.max_concurrent_calls,
        max_leads=p.max_leads,
        max_minutes_monthly=p.max_minutes_monthly,
        max_storage_mb=getattr(p, "max_storage_mb", 5000) or 5000,
        features=p.features or {},
        is_active=p.is_active,
        created_at=p.created_at.isoformat() if p.created_at else "",
        tenant_count=t_count,
    )


@router.patch("/subscriptions/{plan_id}/status")
async def toggle_plan_status(
    plan_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> dict:
    """Toggles active/inactive status of a subscription tier."""
    p = await db.get(Plan, plan_id)
    if not p:
        raise HTTPException(status_code=404, detail="Subscription plan not found.")

    p.is_active = not p.is_active
    db.add(p)
    await db.commit()
    return {"success": True, "plan_id": plan_id, "is_active": p.is_active}


# ===========================================================================
# 8. Platform-Wide Analytics
# ===========================================================================
def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@router.get("/analytics", response_model=SuperAdminAnalyticsResponse)
async def get_platform_analytics(
    time_range: str = Query("30d", regex="^(today|7d|30d|month|all)$"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin),
) -> SuperAdminAnalyticsResponse:
    """Computes platform-wide aggregated analytics with configurable time range."""
    # Ensure now is offset-naive UTC to avoid PostgreSQL asyncpg DataError:
    # "cannot convert aware datetime to timestamp without time zone"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if time_range == "today":
        start_date = datetime.combine(now.date(), datetime.min.time())
    elif time_range == "7d":
        start_date = now - timedelta(days=7)
    elif time_range == "month":
        start_date = datetime(now.year, now.month, 1)
    elif time_range == "all":
        start_date = datetime(2020, 1, 1)
    else:  # 30d default
        start_date = now - timedelta(days=30)

    try:
        # Calls
        calls_res = []
        try:
            call_stmt = select(Call).where(Call.created_at >= start_date)
            calls_res = (await db.execute(call_stmt)).scalars().all()
        except Exception as e:
            await db.rollback()
            logger.warning(f"Error fetching calls for analytics: {e}")

        total_calls = len(calls_res)
        completed_calls = sum(1 for c in calls_res if (c.status or "").lower() in ["completed", "ended", "done"])
        failed_calls = sum(1 for c in calls_res if (c.status or "").lower() in ["failed", "busy", "no-answer", "cancelled"])
        total_duration_sec = sum(c.duration_seconds or 0 for c in calls_res)
        total_duration_min = round(total_duration_sec / 60.0, 1)

        # Leads
        leads_res = []
        try:
            lead_stmt = select(Lead).where(Lead.created_at >= start_date)
            leads_res = (await db.execute(lead_stmt)).scalars().all()
        except Exception as e:
            await db.rollback()
            logger.warning(f"Error fetching leads for analytics: {e}")

        total_leads = len(leads_res)
        qualified_leads = sum(1 for l in leads_res if l.qualification_status == "QUALIFIED")

        # Agents
        try:
            active_agents = (await db.scalar(select(func.count(AIAgent.id)).where(func.upper(AIAgent.status) == "ACTIVE"))) or 0
        except Exception as e:
            await db.rollback()
            logger.warning(f"Error fetching active agents for analytics: {e}")
            active_agents = 0

        # Total tenants
        try:
            total_tenants = (await db.scalar(select(func.count(Business.id)).where(Business.id != PRIMARY_BUSINESS_ID))) or 0
        except Exception as e:
            await db.rollback()
            logger.warning(f"Error fetching total tenants for analytics: {e}")
            total_tenants = 0

        # Tenant growth over the selected range
        days_span = 7 if time_range in ("today", "7d") else 30
        growth_trends = []
        today = now.date()
        try:
            for i in range(days_span - 1, -1, -1 if days_span <= 7 else -2):
                d = today - timedelta(days=i)
                d_end = datetime.combine(d, datetime.max.time())
                t_count = (await db.scalar(select(func.count(Business.id)).where(Business.created_at <= d_end, Business.id != PRIMARY_BUSINESS_ID))) or 0
                growth_trends.append({"date": d.strftime("%b %d"), "count": t_count})
        except Exception as e:
            await db.rollback()
            logger.warning(f"Error computing tenant growth trends: {e}")
            growth_trends = []

        # Call volume trends
        volume_trends = []
        try:
            for i in range(min(14, days_span) - 1, -1, -1):
                d = today - timedelta(days=i)
                d_start = datetime.combine(d, datetime.min.time())
                d_end = datetime.combine(d, datetime.max.time())
                c_count = 0
                for c in calls_res:
                    c_dt = _to_naive_utc(c.created_at)
                    if c_dt and d_start <= c_dt <= d_end:
                        c_count += 1
                volume_trends.append({"date": d.strftime("%b %d"), "calls": c_count})
        except Exception as e:
            logger.warning(f"Error computing call volume trends: {e}")
            volume_trends = []

        # Subscription distribution
        subscription_distribution = []
        try:
            all_tenants = (await db.execute(select(Business).where(Business.id != PRIMARY_BUSINESS_ID))).scalars().all()
            plan_counts: Dict[str, int] = {}
            for b in all_tenants:
                pn = b.current_plan or "Free Plan"
                plan_counts[pn] = plan_counts.get(pn, 0) + 1
            subscription_distribution = [
                {"name": k, "value": v} for k, v in plan_counts.items()
            ]
        except Exception as e:
            await db.rollback()
            logger.warning(f"Error fetching subscription distribution: {e}")
            subscription_distribution = []

        # Storage distribution by category (computed from actual tenant files and uploads)
        categories_dict = {
            "Leads Data": 0,
            "Knowledge Base (RAG)": 0,
            "Voice Audio & Recordings": 0,
            "Other System Uploads": 0,
        }
        try:
            tfiles = (await db.execute(select(TenantFile))).scalars().all()
            for tf in tfiles:
                sz = tf.file_size_bytes or 0
                fn = (tf.filename or "").lower()
                mime = (tf.mime_type or "").lower()
                if fn.endswith((".csv", ".xls", ".xlsx")) or "spreadsheet" in mime or "csv" in mime:
                    categories_dict["Leads Data"] += sz
                elif fn.endswith((".pdf", ".doc", ".docx", ".txt")) or "pdf" in mime or "document" in mime:
                    categories_dict["Knowledge Base (RAG)"] += sz
                elif fn.endswith((".wav", ".mp3", ".ogg", ".webm")) or "audio" in mime:
                    categories_dict["Voice Audio & Recordings"] += sz
                else:
                    categories_dict["Other System Uploads"] += sz
        except Exception as e:
            await db.rollback()
            logger.warning(f"Error reading TenantFile for storage distribution: {e}")

        total_storage_bytes = sum(categories_dict.values())
        if total_storage_bytes > 0:
            storage_distribution = [
                {"category": k, "percentage": round((v / total_storage_bytes) * 100, 1)}
                for k, v in categories_dict.items()
            ]
        else:
            storage_distribution = [
                {"category": "Leads Data", "percentage": 0},
                {"category": "Knowledge Base (RAG)", "percentage": 0},
                {"category": "Voice Audio & Recordings", "percentage": 0},
                {"category": "Other System Uploads", "percentage": 0},
            ]

        return SuperAdminAnalyticsResponse(
            time_range=time_range,
            total_calls=total_calls,
            completed_calls=completed_calls,
            failed_calls=failed_calls,
            total_call_duration_seconds=total_duration_sec,
            total_call_duration_minutes=total_duration_min,
            total_leads=total_leads,
            qualified_leads=qualified_leads,
            active_voice_agents=active_agents,
            total_tenants=total_tenants,
            tenant_growth=growth_trends,
            subscription_distribution=subscription_distribution,
            storage_distribution=storage_distribution,
            call_volume_trends=volume_trends,
        )
    except Exception as ex:
        logger.error(f"[PLATFORM ANALYTICS] Unhandled error: {ex}", exc_info=True)
        return SuperAdminAnalyticsResponse(
            time_range=time_range,
            total_calls=0,
            completed_calls=0,
            failed_calls=0,
            total_call_duration_seconds=0,
            total_call_duration_minutes=0.0,
            total_leads=0,
            qualified_leads=0,
            active_voice_agents=0,
            total_tenants=0,
            tenant_growth=[],
            subscription_distribution=[],
            storage_distribution=[
                {"category": "Leads Data", "percentage": 0},
                {"category": "Knowledge Base (RAG)", "percentage": 0},
                {"category": "Voice Audio & Recordings", "percentage": 0},
                {"category": "Other System Uploads", "percentage": 0},
            ],
            call_volume_trends=[],
        )
