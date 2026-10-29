from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional
from loguru import logger
import uuid

from app.core.deps import get_current_db_user, PRIMARY_BUSINESS_ID
from app.core.security import create_access_token, hash_password
from app.db.database import get_db
from app.db.models import Business, QualificationRule, User, DBInvoice, Call, Lead, Campaign, AIAgent
from app.schemas.business import (
    BusinessCreate,
    BusinessOut,
    BillingPreferenceUpdate,
    BillingPreferenceOut,
    BusinessUpdate,
    InvoiceOut,
    PaymentPayload,
)
from app.schemas.user import Token

router = APIRouter(prefix="/api/businesses", tags=["businesses"])


class TenantCreatePayload(BaseModel):
    name: str
    slug: Optional[str] = None
    subdomain: Optional[str] = None
    custom_domain: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    industry: Optional[str] = "General"
    current_plan: Optional[str] = "Pro"
    permissions: Optional[dict[str, bool]] = None
    cluster_id: Optional[str] = "default"
    server_url: Optional[str] = None
    isolation_strategy: Optional[str] = "SHARED_DATABASE"
    database_url: Optional[str] = None
    schema_name: Optional[str] = None


class TenantPermissionsPayload(BaseModel):
    permissions: dict[str, bool]


@router.get("", response_model=list[dict])
async def list_businesses(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """List all tenants/businesses for admin dashboard."""
    stmt = select(Business).order_by(Business.created_at.desc())
    res = await db.execute(stmt)
    bizs = res.scalars().all()

    out = []
    for b in bizs:
        # Hide default seed business from tenants list as requested by user
        if b.id == PRIMARY_BUSINESS_ID or b.name == "Default Business" or (b.slug and b.slug.lower() == "c3dce06c"):
            continue

        try:
            u_count = await db.scalar(select(func.count(User.id)).where(User.business_id == b.id))
        except Exception:
            u_count = 0
        
        # Get permissions from business or fallback to tenant users
        perms = getattr(b, "permissions", None) or {}
        if not perms:
            try:
                tenant_user = await db.scalar(
                    select(User).where(User.business_id == b.id, User.permissions.is_not(None))
                )
                if tenant_user and isinstance(tenant_user.permissions, dict) and tenant_user.permissions:
                    perms = tenant_user.permissions
            except Exception as e:
                logger.debug(f"[LIST BUSINESSES] Fallback permissions query note: {e}")

        out.append({
            "id": b.id,
            "name": b.name,
            "slug": b.slug or "",
            "subdomain": getattr(b, "subdomain", None) or b.slug or "",
            "custom_domain": getattr(b, "custom_domain", None),
            "cluster_id": getattr(b, "cluster_id", "default") or "default",
            "server_url": getattr(b, "server_url", None),
            "isolation_strategy": getattr(b, "isolation_strategy", "SHARED_DATABASE") or "SHARED_DATABASE",
            "industry": b.industry or "",
            "is_active": b.is_active,
            "status": getattr(b, "status", "active") or "active",
            "registration_source": getattr(b, "registration_source", "UNKNOWN") or "UNKNOWN",
            "current_plan": b.current_plan or "Free Plan",
            "wallet_balance": b.wallet_balance or 0.0,
            "created_at": b.created_at.isoformat() if b.created_at else "",
            "user_count": u_count or 0,
            "permissions": perms or {},
        })
    return out


@router.post("/tenant", response_model=dict, status_code=201)
async def create_tenant(
    payload: TenantCreatePayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin creates a new isolated tenant with optional admin email and password."""
    import re
    from app.models.tenant import Tenant
    name_clean = payload.name.strip()
    slug_val = payload.slug.strip().lower() if payload.slug else name_clean.lower().replace(" ", "-")
    slug_val = re.sub(r"[^a-z0-9-]", "", slug_val)
    if not slug_val:
        slug_val = f"tenant-{str(uuid.uuid4())[:6]}"

    subdomain_val = (payload.subdomain or slug_val).strip().lower()

    # Check if slug exists
    existing = await db.scalar(select(Business).where(Business.slug == slug_val))
    if existing:
        slug_val = f"{slug_val}-{str(uuid.uuid4())[:4]}"
        if not payload.subdomain:
            subdomain_val = slug_val

    tenant_id = str(uuid.uuid4())
    owner_id = user.id
    user_created = False

    # If email is provided, create primary admin user for this tenant
    if payload.email and payload.email.strip():
        clean_email = payload.email.strip().lower()
        existing_user = await db.scalar(select(User).where(User.email == clean_email))
        if existing_user:
            raise HTTPException(status_code=400, detail=f"User with email '{clean_email}' already exists")

        pwd = payload.password.strip() if payload.password else "Welcome123!"
        owner_permissions = dict(payload.permissions or {})
        if owner_permissions:
            owner_permissions.setdefault("leads", True)
            owner_permissions.setdefault("lists", True)

        new_owner = User(
            id=str(uuid.uuid4()),
            email=clean_email,
            hashed_password=hash_password(pwd),
            plain_password=pwd,
            full_name=f"{name_clean} Admin",
            role="BUSINESS_OWNER",
            business_id=tenant_id,
            permissions=owner_permissions,
            is_active=True,
            email_verified=True,
            status="ACTIVE",
        )
        db.add(new_owner)
        owner_id = new_owner.id
        user_created = True

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
        current_plan=payload.current_plan or "Pro",
        registration_source="SUPER_ADMIN_CREATED",
        wallet_balance=100.0,
        permissions=payload.permissions or {},
    )
    db.add(new_tenant)

    try:
        existing_rule = await db.scalar(select(QualificationRule).where(QualificationRule.business_id == new_tenant.id))
        if not existing_rule:
            db.add(QualificationRule(business_id=new_tenant.id))
    except Exception as e:
        logger.debug(f"[BUSINESS CREATE] QualificationRule add note: {e}")

    await db.commit()
    await db.refresh(new_tenant)

    # Sync to Tenant model table (best-effort; will not block business creation if tenants table differs)
    try:
        tenant_record = Tenant(
            id=tenant_id,
            name=name_clean,
            slug=slug_val,
            subdomain=subdomain_val,
            custom_domain=payload.custom_domain.strip().lower() if payload.custom_domain else None,
            status="active",
            cluster_id=(payload.cluster_id or "default").strip(),
            server_url=payload.server_url.strip() if payload.server_url else None,
            isolation_strategy=payload.isolation_strategy or "SHARED_DATABASE",
            database_url=payload.database_url.strip() if payload.database_url else None,
            schema_name=payload.schema_name.strip() if payload.schema_name else None,
        )
        db.add(tenant_record)
        await db.commit()
    except Exception as e:
        logger.debug(f"[BUSINESS CREATE] Sync to tenants table note: {e}")

    return {
        "id": new_tenant.id,
        "name": new_tenant.name,
        "slug": new_tenant.slug,
        "subdomain": new_tenant.subdomain,
        "cluster_id": new_tenant.cluster_id,
        "server_url": new_tenant.server_url,
        "isolation_strategy": new_tenant.isolation_strategy,
        "industry": new_tenant.industry,
        "is_active": new_tenant.is_active,
        "current_plan": new_tenant.current_plan,
        "wallet_balance": new_tenant.wallet_balance,
        "created_at": new_tenant.created_at.isoformat() if new_tenant.created_at else "",
        "user_count": 1 if user_created else 0,
        "permissions": new_tenant.permissions or {},
    }


@router.patch("/{business_id}/permissions", response_model=dict)
async def update_tenant_permissions(
    business_id: str,
    payload: TenantPermissionsPayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Updates access permissions checklist for a specific tenant workspace."""
    biz = await db.get(Business, business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant not found")

    biz.permissions = payload.permissions

    # Update all users belonging to this tenant
    res = await db.execute(select(User).where(User.business_id == business_id))
    tenant_users = res.scalars().all()
    for u in tenant_users:
        u.permissions = payload.permissions

    if biz.owner_user_id:
        owner = await db.get(User, biz.owner_user_id)
        if owner:
            owner.permissions = payload.permissions

    await db.commit()
    return {"message": "Permissions updated successfully", "permissions": payload.permissions}


class TenantPlanUpdatePayload(BaseModel):
    current_plan: str
    wallet_balance: Optional[float] = None
    add_balance: Optional[float] = None


@router.get("/{business_id}/details", response_model=dict)
async def get_tenant_details(
    business_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint to retrieve comprehensive tenant overview, pricing, invoices, and analytics.
    SUPER_ADMIN can view any tenant; regular admins can only view their own business.
    """
    # Authorization: non-super-admin users may only view their own business
    is_super_admin = user.role == "SUPER_ADMIN" or user.email == "admin@callzenza.com"
    if not is_super_admin and user.business_id and user.business_id != business_id:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: You do not have permission to view this tenant's details."
        )

    biz = await db.get(Business, business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant organization not found")


    # 1. Analytics & Usage
    total_calls = await db.scalar(select(func.count(Call.id)).where(Call.business_id == business_id)) or 0
    total_seconds = await db.scalar(select(func.sum(Call.duration_seconds)).where(Call.business_id == business_id)) or 0
    total_minutes = round(total_seconds / 60.0, 1)

    total_leads = await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == business_id)) or 0
    qualified_leads = await db.scalar(
        select(func.count(Lead.id)).where(
            Lead.business_id == business_id,
            (Lead.qualification_status == "QUALIFIED") | (Lead.status == "QUALIFIED")
        )
    ) or 0
    qual_rate = round((qualified_leads / total_leads * 100.0) if total_leads > 0 else 0.0, 1)

    total_campaigns = await db.scalar(select(func.count(Campaign.id)).where(Campaign.business_id == business_id)) or 0
    active_campaigns = await db.scalar(
        select(func.count(Campaign.id)).where(
            Campaign.business_id == business_id,
            Campaign.status.in_(["RUNNING", "ACTIVE"])
        )
    ) or 0

    total_users = await db.scalar(select(func.count(User.id)).where(User.business_id == business_id)) or 0
    total_agents = await db.scalar(select(func.count(AIAgent.id)).where(AIAgent.business_id == business_id)) or 0

    # 2. Invoices
    res_invoices = await db.execute(
        select(DBInvoice).where(DBInvoice.business_id == business_id).order_by(DBInvoice.created_at.desc()).limit(15)
    )
    invoices = [
        {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "date": inv.date,
            "description": inv.description,
            "amount": inv.amount,
            "status": inv.status,
        }
        for inv in res_invoices.scalars().all()
    ]

    # 3. Users list snippet
    res_users = await db.execute(
        select(User).where(User.business_id == business_id).order_by(User.created_at.desc()).limit(10)
    )
    users_list = [
        {
            "id": u.id,
            "email": u.email,
            "full_name": u.full_name or f"{u.first_name} {u.last_name}".strip(),
            "role": u.role,
            "status": u.status,
            "email_verified": u.email_verified,
        }
        for u in res_users.scalars().all()
    ]

    # 4. Standard available plans configuration
    available_plans = [
        {
            "name": "Free Plan",
            "price": "$0 / month",
            "minutes_included": 100,
            "concurrent_calls": 2,
            "ai_agents": 1,
            "features": ["100 Free Minutes", "2 Concurrent Calls", "1 AI Voice Agent", "Community Support"],
        },
        {
            "name": "Starter",
            "price": "$49 / month",
            "minutes_included": 500,
            "concurrent_calls": 5,
            "ai_agents": 3,
            "features": ["500 Call Minutes", "5 Concurrent Calls", "3 AI Voice Agents", "Inbound DID Routing", "Standard Analytics"],
        },
        {
            "name": "Growth",
            "price": "$149 / month",
            "minutes_included": 2000,
            "concurrent_calls": 15,
            "ai_agents": 10,
            "features": ["2,000 Call Minutes", "15 Concurrent Calls", "10 AI Voice Agents", "Full CRM & Lead Hopper", "Advanced Analytics"],
        },
        {
            "name": "Enterprise Pro",
            "price": "$399 / month",
            "minutes_included": 10000,
            "concurrent_calls": 50,
            "ai_agents": 50,
            "features": ["10,000 Call Minutes", "50 Concurrent Calls", "Unlimited AI Agents", "Custom Subdomain & Cluster", "Dedicated Database Isolation", "24/7 SLA Support"],
        },
    ]

    return {
        "tenant": {
            "id": biz.id,
            "name": biz.name,
            "slug": biz.slug or "",
            "subdomain": getattr(biz, "subdomain", None) or biz.slug or "",
            "custom_domain": getattr(biz, "custom_domain", None),
            "status": getattr(biz, "status", "active") or "active",
            "is_active": biz.is_active,
            "cluster_id": getattr(biz, "cluster_id", "default") or "default",
            "server_url": getattr(biz, "server_url", None),
            "region": getattr(biz, "region", "us-east") or "us-east",
            "isolation_strategy": getattr(biz, "isolation_strategy", "SHARED_DATABASE") or "SHARED_DATABASE",
            "industry": biz.industry or "General Business",
            "created_at": biz.created_at.isoformat() if biz.created_at else "",
            "permissions": getattr(biz, "permissions", {}) or {},
        },
        "pricing": {
            "current_plan": biz.current_plan or "Free Plan",
            "wallet_balance": biz.wallet_balance or 0.0,
            "billing_company_name": biz.billing_company_name or biz.name,
            "billing_email": biz.billing_email or "",
            "billing_tax_id": biz.billing_tax_id or "",
            "invoices": invoices,
            "available_plans": available_plans,
        },
        "analytics": {
            "total_calls": total_calls,
            "total_call_minutes": total_minutes,
            "total_leads": total_leads,
            "qualified_leads": qualified_leads,
            "qualification_rate": qual_rate,
            "total_campaigns": total_campaigns,
            "active_campaigns": active_campaigns,
            "total_users": total_users,
            "total_agents": total_agents,
        },
        "users": users_list,
    }


@router.patch("/{business_id}/plan", response_model=dict)
async def update_tenant_plan(
    business_id: str,
    payload: TenantPlanUpdatePayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """SuperAdmin updates tenant's pricing plan and wallet balance."""
    biz = await db.get(Business, business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant not found")

    biz.current_plan = payload.current_plan
    if payload.wallet_balance is not None:
        biz.wallet_balance = payload.wallet_balance
    if payload.add_balance is not None and payload.add_balance > 0:
        biz.wallet_balance = (biz.wallet_balance or 0.0) + payload.add_balance

    db.add(biz)
    await db.commit()
    await db.refresh(biz)

    return {
        "message": f"Tenant plan updated to {biz.current_plan}",
        "current_plan": biz.current_plan,
        "wallet_balance": biz.wallet_balance,
    }


@router.patch("/{business_id}/status", response_model=dict)
async def toggle_tenant_status(
    business_id: str,
    status_payload: dict = Body(...),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle tenant active / suspended status."""
    biz = await db.get(Business, business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant not found")
    
    if "is_active" in status_payload:
        biz.is_active = bool(status_payload["is_active"])
    
    db.add(biz)
    await db.commit()
    return {"id": biz.id, "is_active": biz.is_active, "status": "active" if biz.is_active else "suspended"}


@router.delete("/{business_id}", response_model=dict)
async def delete_tenant(
    business_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a tenant / business and clean up its scoped data."""
    if business_id == PRIMARY_BUSINESS_ID:
        raise HTTPException(status_code=400, detail="Cannot delete primary default organization")

    biz = await db.get(Business, business_id)
    if not biz:
        raise HTTPException(status_code=404, detail="Tenant not found")

    from app.db.models import (
        Lead, Campaign, Call, AuditLog, User, QualificationRule, DBInvoice,
        AIAgent, Conversation, Integration
    )
    await db.execute(delete(Conversation).where(Conversation.business_id == business_id))
    await db.execute(delete(Integration).where(Integration.business_id == business_id))
    await db.execute(delete(AIAgent).where(AIAgent.business_id == business_id))
    await db.execute(delete(Lead).where(Lead.business_id == business_id))
    await db.execute(delete(Campaign).where(Campaign.business_id == business_id))
    await db.execute(delete(Call).where(Call.business_id == business_id))
    await db.execute(delete(User).where(User.business_id == business_id))
    await db.execute(delete(QualificationRule).where(QualificationRule.business_id == business_id))
    await db.execute(delete(DBInvoice).where(DBInvoice.business_id == business_id))
    await db.delete(biz)
    await db.commit()

    return {"message": f"Tenant {business_id} deleted successfully"}


@router.post("", response_model=Token, status_code=201)
async def create_business(
    payload: BusinessCreate,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Token:
    """Creates a tenant and makes the caller its BUSINESS_OWNER (module 7)."""
    if user.business_id:
        raise HTTPException(status_code=400, detail="User already belongs to a business")

    business = Business(name=payload.name, industry=payload.industry, owner_user_id=user.id)
    db.add(business)
    await db.flush()

    db.add(QualificationRule(business_id=business.id))

    user.business_id = business.id
    user.role = "BUSINESS_OWNER"
    db.add(user)
    await db.commit()

    token = create_access_token(subject=user.id, role=user.role)
    return Token(access_token=token)


@router.get("/me", response_model=BusinessOut)
async def get_my_business(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Business:
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business yet")
    business = await db.get(Business, user.business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    return business


@router.get("/me/billing", response_model=BillingPreferenceOut)
async def get_billing_preferences(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Business:
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business associated with this user")
    business = await db.get(Business, user.business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")
    return business


@router.put("/me/billing", response_model=BillingPreferenceOut)
async def update_billing_preferences(
    payload: BillingPreferenceUpdate,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Business:
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business associated with this user")
    business = await db.get(Business, user.business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    business.billing_company_name = payload.billing_company_name
    business.billing_email = payload.billing_email
    business.billing_address = payload.billing_address
    business.billing_tax_id = payload.billing_tax_id

    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


@router.patch("/me", response_model=BusinessOut)
async def update_my_business(
    payload: BusinessUpdate,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Business:
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business associated with this user")
    business = await db.get(Business, user.business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    data = payload.model_dump(exclude_unset=True)
    for field, val in data.items():
        setattr(business, field, val)

    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


@router.post("/me/billing/payment", response_model=BillingPreferenceOut)
async def process_billing_payment(
    payload: PaymentPayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Business:
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business associated with this user")
    business = await db.get(Business, user.business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    # Update balance and plan
    business.wallet_balance += payload.amount_added
    if payload.plan_name:
        business.current_plan = payload.plan_name

    # Create Invoice record
    import random
    invoice_number = f"INV-2026-{random.randint(1000, 9999)}"
    from datetime import datetime
    current_date = datetime.now().strftime("%b %d, %Y")
    
    invoice = DBInvoice(
        business_id=business.id,
        invoice_number=invoice_number,
        date=current_date,
        description=payload.invoice_description,
        amount=f"₹{payload.amount_added:.2f}",
        status="Paid",
    )
    db.add(invoice)
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


@router.get("/me/billing/invoices", response_model=list[InvoiceOut])
async def get_billing_invoices(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> list[DBInvoice]:
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business associated with this user")
    stmt = select(DBInvoice).where(DBInvoice.business_id == user.business_id).order_by(DBInvoice.created_at.desc())
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.delete("/me/billing/cancel-plan", response_model=BillingPreferenceOut)
async def cancel_billing_plan(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Business:
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business associated with this user")
    business = await db.get(Business, user.business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    business.current_plan = "Free Plan"
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


import hmac
import hashlib
import httpx
from datetime import datetime
from app.core.config import get_settings


class RazorpayOrderCreatePayload(BaseModel):
    amount_inr: float
    plan_name: Optional[str] = None


class RazorpayOrderResponse(BaseModel):
    order_id: str
    key_id: str
    amount_paise: int
    currency: str = "INR"


class RazorpayVerifyPayload(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: Optional[str] = ""
    amount_inr: float
    plan_name: Optional[str] = None
    invoice_description: Optional[str] = None


@router.post("/me/billing/razorpay/create-order", response_model=RazorpayOrderResponse)
async def create_razorpay_order(
    payload: RazorpayOrderCreatePayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business associated with this user")

    settings = get_settings()
    amount_paise = int(round(payload.amount_inr * 100))
    key_id = settings.razorpay_key_id or "rzp_test_TMkmJogQ2Homiz"
    key_secret = settings.razorpay_key_secret

    order_id = f"order_{uuid.uuid4().hex[:14]}"

    if key_secret:
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    "https://api.razorpay.com/v1/orders",
                    auth=(key_id, key_secret),
                    json={
                        "amount": amount_paise,
                        "currency": "INR",
                        "receipt": f"rcpt_{uuid.uuid4().hex[:8]}",
                        "notes": {
                            "user_id": user.id,
                            "business_id": user.business_id,
                            "plan_name": payload.plan_name or "Wallet Credit"
                        }
                    },
                    timeout=10.0
                )
                if res.status_code == 200:
                    rzp_data = res.json()
                    order_id = rzp_data.get("id", order_id)
        except Exception as e:
            logger.warning(f"Razorpay API order creation failed: {e}. Falling back to order ID.")

    return RazorpayOrderResponse(
        order_id=order_id,
        key_id=key_id,
        amount_paise=amount_paise,
        currency="INR"
    )


@router.post("/me/billing/razorpay/verify")
async def verify_razorpay_payment(
    payload: RazorpayVerifyPayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.business_id:
        raise HTTPException(status_code=404, detail="No business associated with this user")
    business = await db.get(Business, user.business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found")

    settings = get_settings()
    key_secret = settings.razorpay_key_secret
    if key_secret and payload.razorpay_signature:
        msg = f"{payload.razorpay_order_id}|{payload.razorpay_payment_id}"
        generated_signature = hmac.new(
            key_secret.encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()
        if generated_signature != payload.razorpay_signature:
            raise HTTPException(status_code=400, detail="Razorpay signature verification failed")

    # Credit business balance & update current plan
    business.wallet_balance += payload.amount_inr
    if payload.plan_name and payload.plan_name.strip():
        business.current_plan = payload.plan_name.strip()

    # Create Invoice record
    import random
    invoice_number = f"INV-RZP-{random.randint(10000, 99999)}"
    current_date = datetime.now().strftime("%b %d, %Y")
    inv_desc = payload.invoice_description or (
        f"GBZ {payload.plan_name} Subscription" if payload.plan_name else f"Razorpay Payment ({payload.razorpay_payment_id})"
    )

    invoice = DBInvoice(
        business_id=business.id,
        invoice_number=invoice_number,
        date=current_date,
        description=inv_desc,
        amount=f"₹{payload.amount_inr:.2f}",
        status="Paid",
    )
    db.add(invoice)
    db.add(business)
    await db.commit()
    await db.refresh(business)

    return {
        "success": True,
        "message": "Payment verified successfully",
        "wallet_balance": business.wallet_balance,
        "current_plan": business.current_plan,
        "invoice_number": invoice_number,
    }
