"""API routes for Multi-Tenant SIP Configuration and Operational Management."""
from datetime import datetime, timezone
from typing import Any, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user, get_current_tenant_id, require_admin
from app.db.database import get_db
from app.db.models import Business, Call, Campaign, SIPAccount, User
from app.schemas.sip_account import (
    SIPAccountCreate,
    SIPAccountDIDAssign,
    SIPAccountDIDOut,
    SIPAccountOut,
    SIPAccountRegisterResponse,
    SIPAccountTestResponse,
    SIPAccountToggleStatus,
    SIPAccountUpdate,
)
from app.services.sip_account_service import (
    encrypt_sip_secret,
    register_and_update_sip_account,
    test_and_update_sip_account,
)

def _to_sip_account_out(
    acc: SIPAccount,
    org_name: str = "Default Organization",
    assigned_campaigns_count: int = 0,
    dids: Optional[List[Any]] = None,
) -> SIPAccountOut:
    settings = acc.settings or {} if isinstance(acc.settings, dict) else {}
    reg_diag = settings.get("registration_diagnostics") or {} if isinstance(settings.get("registration_diagnostics"), dict) else {}
    reg_status = (acc.registration_status or "unregistered").lower()
    last_err = acc.last_error_message or reg_diag.get("message")
    err_code = reg_diag.get("error_code")
    sip_code = reg_diag.get("sip_status_code")
    sip_reason = reg_diag.get("sip_status_reason")
    suggested_act = reg_diag.get("suggested_action")
    retryable_val = reg_diag.get("retryable")

    try:
        from app.services.sip_inbound_daemon import get_daemon_registration_info
        daemon_info = get_daemon_registration_info(acc.id)
        is_daemon_target = bool(
            daemon_info.get("account_id") == acc.id
            and (daemon_info.get("attempt_id") is not None or daemon_info.get("last_checked_at") is not None)
        )
        if not acc.enabled:
            reg_status = "disabled"
        elif is_daemon_target:
            if daemon_info.get("is_registered"):
                reg_status = "registered"
                last_err = None
                sip_code = 200
            elif daemon_info.get("status"):
                reg_status = daemon_info["status"]
                if daemon_info.get("last_error"):
                    last_err = daemon_info["last_error"]
                if daemon_info.get("error_code"):
                    err_code = daemon_info["error_code"]
                if daemon_info.get("sip_status_code"):
                    sip_code = daemon_info["sip_status_code"]
                if daemon_info.get("suggested_action"):
                    suggested_act = daemon_info["suggested_action"]
        elif reg_status in ("auth_failed", "dns_error", "network_error", "not_required", "not_configured"):
            # Preserve granular diagnostic states
            pass
        elif acc.direction and acc.direction.lower() == "outbound":
            if acc.last_health_status == "HEALTHY":
                reg_status = "outbound_ready"
            elif reg_status not in ("registered", "outbound_ready"):
                reg_status = "unregistered"
    except Exception:
        pass

    dids_out: List[SIPAccountDIDOut] = []
    dids_source = dids if dids is not None else getattr(acc, "dids", None)
    if dids_source:
        for d in dids_source:
            try:
                dids_out.append(SIPAccountDIDOut(
                    id=str(d.id),
                    phone_number=d.phone_number,
                    friendly_name=getattr(d, "friendly_name", None),
                    status=getattr(d, "status", "ACTIVE"),
                    is_outbound_caller_id=getattr(d, "is_outbound_caller_id", False),
                    sip_account_id=getattr(d, "sip_account_id", None),
                    organization_id=getattr(d, "business_id", None),
                    created_at=getattr(d, "created_at", None),
                ))
            except Exception:
                pass

    return SIPAccountOut(
        id=acc.id,
        organization_id=acc.organization_id,
        organization_name=org_name,
        name=acc.name,
        username=acc.username,
        auth_username=acc.auth_username,
        domain=acc.domain,
        host=acc.host,
        port=acc.port,
        transport=acc.transport,
        outbound_proxy=acc.outbound_proxy,
        caller_id=acc.caller_id,
        direction=acc.direction,
        enabled=acc.enabled,
        registration_status=reg_status,
        last_health_status=acc.last_health_status,
        last_health_latency_ms=acc.last_health_latency_ms,
        last_health_checked_at=acc.last_health_checked_at,
        last_error_message=last_err,
        max_channels=acc.max_channels,
        has_password=bool(acc.password_hash),
        masked_password="••••••••" if acc.password_hash else "",
        assigned_campaigns_count=assigned_campaigns_count,
        dids=dids_out,
        settings=settings,
        sip_server=settings.get("sip_server") or acc.host,
        sip_login=settings.get("sip_login") or acc.auth_username or acc.username,
        sip_public_address=settings.get("sip_public_address") or "Auto",
        voicemail_number=settings.get("voicemail_number"),
        dial_plan=settings.get("dial_plan"),
        webhook_base_url=settings.get("webhook_base_url"),
        additional_dids=settings.get("additional_dids") or [d.phone_number for d in dids_out if d.phone_number and d.phone_number != acc.caller_id],
        hide_caller_id=bool(settings.get("hide_caller_id", False)),
        media_encryption=bool(settings.get("media_encryption", False)),
        publish_presence=bool(settings.get("publish_presence", False)),
        allow_ip_rewrite=bool(settings.get("allow_ip_rewrite", False)),
        ice=bool(settings.get("ice", False)),
        disable_session_timers=bool(settings.get("disable_session_timers", False)),
        use_auth_user_in_from=bool(settings.get("use_auth_user_in_from", False)),
        extension=acc.extension or settings.get("extension"),
        dialing_prefix=acc.dialing_prefix or settings.get("dialing_prefix"),
        display_name=acc.display_name or settings.get("display_name"),
        refresh_interval=acc.refresh_interval or settings.get("refresh_interval", 1800),
        keep_alive_interval=acc.keep_alive_interval or settings.get("keep_alive_interval", 20),
        local_ip=acc.local_ip or settings.get("local_ip"),
        rtp_ip=acc.rtp_ip or settings.get("rtp_ip"),
        rtp_port=acc.rtp_port or settings.get("rtp_port"),
        stun_server=acc.stun_server or settings.get("stun_server"),
        sip_ws_url=acc.sip_ws_url or settings.get("sip_ws_url"),
        is_default=bool(acc.is_default or settings.get("is_default", False)),
        error_code=err_code,
        sip_status_code=sip_code,
        sip_status_reason=sip_reason,
        check_type=reg_diag.get("check_type"),
        suggested_action=suggested_act,
        retryable=retryable_val if retryable_val is not None else (reg_status in ("network_error", "unregistered", "auth_failed")),
        registration_diagnostics=reg_diag if reg_diag else ({
            "status": reg_status,
            "error_code": err_code,
            "message": last_err,
            "sip_status_code": sip_code,
            "suggested_action": suggested_act,
        } if last_err or sip_code else None),
        created_at=acc.created_at,
        updated_at=acc.updated_at,
    )


async def _unset_other_defaults(db: AsyncSession, organization_id: str, current_account_id: str) -> None:
    stmt = select(SIPAccount).where(
        SIPAccount.organization_id == organization_id,
        SIPAccount.id != current_account_id,
    )
    res = await db.execute(stmt)
    other_accounts = res.scalars().all()
    for other in other_accounts:
        if isinstance(other.settings, dict) and other.settings.get("is_default"):
            new_settings = dict(other.settings)
            new_settings["is_default"] = False
            other.settings = new_settings
            db.add(other)


router = APIRouter(prefix="/api/sip-accounts", tags=["SIP Accounts"])
v1_router = APIRouter(prefix="/api/v1/sip-accounts", tags=["SIP Accounts"])


def is_super_admin_user(user: User) -> bool:
    role = (user.role or "").strip().upper()
    return role == "SUPER_ADMIN" or (user.email or "").strip().lower() == "admin@callzenza.com"


@router.get("", response_model=List[SIPAccountOut])
@router.get("/", response_model=List[SIPAccountOut])
@v1_router.get("", response_model=List[SIPAccountOut])
@v1_router.get("/", response_model=List[SIPAccountOut])
async def list_sip_accounts(
    enabled_only: bool = Query(False, description="Filter for only enabled accounts"),
    direction: Optional[str] = Query(None, description="Filter by direction: outbound, inbound, both"),
    organization_id: Optional[str] = Query(None, description="Optional organization filter (Super Admin only)"),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
):
    """Lists SIP accounts.
    For regular tenants: strictly returns accounts assigned to their authorized organization_id.
    For Super Admin: returns accounts across organizations or filtered by organization_id.
    Never reveals raw passwords.
    """
    is_super = is_super_admin_user(user)

    # Ensure any active carriers/trunks are synchronized into SIP Accounts
    try:
        from app.services.sip_account_service import sync_carrier_trunks_to_sip_accounts
        target_sync_tenant = organization_id if (is_super and organization_id) else tenant_id
        await sync_carrier_trunks_to_sip_accounts(db, target_sync_tenant)
    except Exception as sync_err:
        logger.warning(f"[SIP_ACCOUNTS] Auto-sync warning: {sync_err}")

    if is_super:
        if organization_id and organization_id.strip():
            stmt = select(SIPAccount).where(SIPAccount.organization_id == organization_id.strip())
        else:
            stmt = select(SIPAccount)
    else:
        # Strict tenant isolation
        stmt = select(SIPAccount).where(SIPAccount.organization_id == tenant_id)

    if enabled_only:
        stmt = stmt.where(SIPAccount.enabled == True)
    if direction:
        dir_clean = direction.strip().lower()
        if dir_clean in ("outbound", "inbound"):
            stmt = stmt.where(SIPAccount.direction.in_([dir_clean, "both"]))
        elif dir_clean == "both":
            stmt = stmt.where(SIPAccount.direction == "both")

    stmt = stmt.order_by(SIPAccount.created_at.desc())
    result = await db.execute(stmt)
    accounts = result.scalars().all()

    if not accounts:
        return []

    # Get campaign assignment counts per SIP account
    acc_ids = [acc.id for acc in accounts]
    camp_counts_stmt = (
        select(Campaign.sip_account_id, func.count(Campaign.id))
        .where(Campaign.sip_account_id.in_(acc_ids))
        .group_by(Campaign.sip_account_id)
    )
    camp_counts_res = await db.execute(camp_counts_stmt)
    camp_counts_map = dict(camp_counts_res.all())

    # Fetch organization names for all distinct organizations
    org_ids = list({acc.organization_id for acc in accounts if acc.organization_id})
    org_map = {}
    if org_ids:
        orgs_res = await db.execute(select(Business.id, Business.name).where(Business.id.in_(org_ids)))
        org_map = dict(orgs_res.all())

    # Fetch DIDs associated with these SIP accounts
    from app.db.models import DIDNumber
    dids_res = await db.execute(select(DIDNumber).where(DIDNumber.sip_account_id.in_(acc_ids)))
    dids_by_acc: dict = {}
    for d in dids_res.scalars().all():
        dids_by_acc.setdefault(d.sip_account_id, []).append(d)

    out_list = []
    for acc in accounts:
        out_list.append(
            _to_sip_account_out(
                acc,
                org_name=org_map.get(acc.organization_id, "Default Organization"),
                assigned_campaigns_count=camp_counts_map.get(acc.id, 0),
                dids=dids_by_acc.get(acc.id, []),
            )
        )
    return out_list


@router.post("", response_model=SIPAccountOut, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=SIPAccountOut, status_code=status.HTTP_201_CREATED)
@v1_router.post("", response_model=SIPAccountOut, status_code=status.HTTP_201_CREATED)
@v1_router.post("/", response_model=SIPAccountOut, status_code=status.HTTP_201_CREATED)
async def create_sip_account(
    data: SIPAccountCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Creates a new SIP account.
    Admin can assign the account to a specified organization/tenant.
    Non-superadmin creates accounts strictly for their own authorized tenant.
    Credentials are encrypted at rest with Fernet.
    """
    is_super = is_super_admin_user(user)
    target_org_id = tenant_id

    if is_super and data.organization_id and data.organization_id.strip():
        # Validate that the selected organization exists
        target_biz = await db.get(Business, data.organization_id.strip())
        if not target_biz:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Target organization '{data.organization_id}' not found.",
            )
        target_org_id = target_biz.id

    # Enforce unique account name per organization
    existing_stmt = select(SIPAccount).where(
        SIPAccount.organization_id == target_org_id,
        SIPAccount.name == data.name.strip(),
    )
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A SIP account named '{data.name.strip()}' already exists in this organization.",
        )

    # Secure credential storage
    encrypted_pw = encrypt_sip_secret(data.password) if data.password else None

    new_account = SIPAccount(
        id=str(uuid.uuid4()),
        organization_id=target_org_id,
        name=data.name.strip(),
        username=data.username.strip(),
        auth_username=data.auth_username.strip() if data.auth_username else None,
        password_hash=encrypted_pw,
        domain=data.domain.strip() if data.domain else None,
        host=data.host.strip(),
        port=data.port,
        transport=data.transport.strip().lower(),
        outbound_proxy=data.outbound_proxy.strip() if data.outbound_proxy else None,
        caller_id=data.caller_id.strip() if data.caller_id else None,
        direction=data.direction.strip().lower(),
        enabled=data.enabled,
        registration_status="pending",
        last_health_status="unknown",
        max_channels=data.max_channels,
        settings=data.settings or {},
    )

    # Populate extended properties if provided on model
    ext_settings = dict(new_account.settings or {})
    for k in (
        "sip_server", "sip_login", "sip_public_address", "voicemail_number",
        "dial_plan", "webhook_base_url", "hide_caller_id", "media_encryption",
        "publish_presence", "allow_ip_rewrite", "ice", "disable_session_timers",
        "use_auth_user_in_from"
    ):
        v = getattr(data, k, None)
        if v is not None:
            ext_settings[k] = v
    if data.additional_dids:
        ext_settings["additional_dids"] = data.additional_dids
    new_account.settings = ext_settings

    for prop in ("extension", "dialing_prefix", "display_name", "refresh_interval", "keep_alive_interval", "local_ip", "rtp_ip", "rtp_port", "stun_server", "sip_ws_url", "is_default"):
        val = getattr(data, prop, None)
        if val is not None:
            setattr(new_account, prop, val)

    if new_account.is_default:
        await _unset_other_defaults(db, tenant_id, new_account.id)

    db.add(new_account)
    await db.commit()
    await db.refresh(new_account)

    # Automatically synchronize caller_id and additional_dids into did_inventory
    from app.db.models import DIDNumber
    dids_to_register = []
    if new_account.caller_id and new_account.caller_id.strip():
        dids_to_register.append((new_account.caller_id.strip(), True))
    if data.additional_dids:
        for ad in data.additional_dids:
            if ad and ad.strip() and ad.strip() != (new_account.caller_id or "").strip():
                dids_to_register.append((ad.strip(), False))

    for num, is_primary in dids_to_register:
        existing_did = (await db.execute(
            select(DIDNumber).where(DIDNumber.phone_number == num)
        )).scalars().first()
        if existing_did:
            existing_did.sip_account_id = new_account.id
            if not existing_did.business_id:
                existing_did.business_id = target_org_id
            if is_primary:
                existing_did.is_outbound_caller_id = True
            db.add(existing_did)
        else:
            new_did = DIDNumber(
                id=str(uuid.uuid4()),
                business_id=target_org_id,
                phone_number=num,
                sip_account_id=new_account.id,
                status="ACTIVE",
                is_outbound_caller_id=is_primary,
            )
            db.add(new_did)
    await db.commit()

    logger.info(
        f"[SIP_ACCOUNTS] Created SIP account '{new_account.name}' (id={new_account.id}) for org={target_org_id}"
    )

    # Sync to carrier and trunk tables to maintain backwards-compatibility
    try:
        from app.services.sip_account_service import sync_sip_account_to_carrier_trunk
        await sync_sip_account_to_carrier_trunk(db, new_account, raw_password=data.password)
    except Exception as e:
        logger.warning(f"[SIP_ACCOUNTS] Error syncing to carrier/trunk: {e}")

    # Trigger quick initial reachability check
    try:
        await test_and_update_sip_account(db, new_account, timeout=2.5)
    except Exception as e:
        logger.warning(f"[SIP_ACCOUNTS] Initial probe warning for {new_account.id}: {e}")

    try:
        from app.services.sip_inbound_daemon import notify_sip_account_changed
        notify_sip_account_changed()
    except Exception:
        pass

    org = await db.get(Business, target_org_id)
    org_name = org.name if org else "Default Organization"

    return _to_sip_account_out(new_account, org_name=org_name, assigned_campaigns_count=0)


# ---------------------------------------------------------------------------
# Super Admin Cross-Tenant Monitoring Endpoint (Must precede /{sip_account_id})
# ---------------------------------------------------------------------------

@router.get("/super-admin/overview", response_model=List[SIPAccountOut])
@router.get("/super-admin/overview/", response_model=List[SIPAccountOut])
@v1_router.get("/super-admin/overview", response_model=List[SIPAccountOut])
@v1_router.get("/super-admin/overview/", response_model=List[SIPAccountOut])
async def super_admin_sip_overview(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
):
    """Super Admin view for monitoring tenant SIP accounts across the system.
    Strictly verifies SUPER_ADMIN role. Secrets remain encrypted and masked.
    """
    if not is_super_admin_user(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: Super Admin privileges required.",
        )

    # Join with Business to get organization names
    stmt = (
        select(SIPAccount, Business.name.label("org_name"))
        .outerjoin(Business, SIPAccount.organization_id == Business.id)
        .order_by(SIPAccount.created_at.desc())
    )
    results = (await db.execute(stmt)).all()

    out_list = []
    for row in results:
        acc: SIPAccount = row[0]
        org_name: Optional[str] = row[1] or "Unknown Organization"

        camp_count_stmt = select(func.count(Campaign.id)).where(Campaign.sip_account_id == acc.id)
        camp_count = (await db.execute(camp_count_stmt)).scalar() or 0

        out_list.append(_to_sip_account_out(acc, org_name=org_name, assigned_campaigns_count=camp_count))
    return out_list


@router.get("/{sip_account_id}", response_model=SIPAccountOut)
@router.get("/{sip_account_id}/", response_model=SIPAccountOut)
@v1_router.get("/{sip_account_id}", response_model=SIPAccountOut)
@v1_router.get("/{sip_account_id}/", response_model=SIPAccountOut)
async def get_sip_account(
    sip_account_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
):
    """Retrieves a single SIP account with tenant ownership verification."""
    is_super = is_super_admin_user(user)
    if is_super:
        stmt = select(SIPAccount).where(SIPAccount.id == sip_account_id)
    else:
        stmt = select(SIPAccount).where(
            SIPAccount.id == sip_account_id,
            SIPAccount.organization_id == tenant_id,
        )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    camp_count_stmt = select(func.count(Campaign.id)).where(Campaign.sip_account_id == sip_account_id)
    camp_count = (await db.execute(camp_count_stmt)).scalar() or 0

    org = await db.get(Business, account.organization_id)
    org_name = org.name if org else "Default Organization"

    from app.db.models import DIDNumber
    dids_stmt = select(DIDNumber).where(DIDNumber.sip_account_id == sip_account_id).order_by(DIDNumber.created_at.desc())
    dids_list = (await db.execute(dids_stmt)).scalars().all()

    return _to_sip_account_out(account, org_name=org_name, assigned_campaigns_count=camp_count, dids=dids_list)


@router.get("/{sip_account_id}/dids", response_model=List[SIPAccountDIDOut])
@router.get("/{sip_account_id}/dids/", response_model=List[SIPAccountDIDOut])
@v1_router.get("/{sip_account_id}/dids", response_model=List[SIPAccountDIDOut])
@v1_router.get("/{sip_account_id}/dids/", response_model=List[SIPAccountDIDOut])
async def get_sip_account_dids(
    sip_account_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
):
    """Retrieves all DIDs explicitly assigned to a SIP account."""
    is_super = is_super_admin_user(user)
    if is_super:
        stmt = select(SIPAccount).where(SIPAccount.id == sip_account_id)
    else:
        stmt = select(SIPAccount).where(
            SIPAccount.id == sip_account_id,
            SIPAccount.organization_id == tenant_id,
        )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    from app.db.models import DIDNumber
    dids_stmt = select(DIDNumber).where(DIDNumber.sip_account_id == sip_account_id).order_by(DIDNumber.created_at.desc())
    dids = (await db.execute(dids_stmt)).scalars().all()
    return [
        SIPAccountDIDOut(
            id=str(d.id),
            phone_number=d.phone_number,
            friendly_name=getattr(d, "friendly_name", None),
            status=getattr(d, "status", "ACTIVE"),
            is_outbound_caller_id=getattr(d, "is_outbound_caller_id", False),
            sip_account_id=d.sip_account_id,
            organization_id=d.business_id,
            created_at=getattr(d, "created_at", None),
        )
        for d in dids
    ]


@router.post("/{sip_account_id}/dids", response_model=List[SIPAccountDIDOut])
@router.post("/{sip_account_id}/dids/", response_model=List[SIPAccountDIDOut])
@v1_router.post("/{sip_account_id}/dids", response_model=List[SIPAccountDIDOut])
@v1_router.post("/{sip_account_id}/dids/", response_model=List[SIPAccountDIDOut])
async def assign_dids_to_sip_account(
    sip_account_id: str,
    payload: SIPAccountDIDAssign,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Assigns one or more DIDs to a SIP account with strict tenant isolation."""
    is_super = is_super_admin_user(user)
    if is_super:
        stmt = select(SIPAccount).where(SIPAccount.id == sip_account_id)
    else:
        stmt = select(SIPAccount).where(
            SIPAccount.id == sip_account_id,
            SIPAccount.organization_id == tenant_id,
        )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    from app.db.models import DIDNumber
    for did_id in payload.did_ids:
        did = await db.get(DIDNumber, did_id)
        if not did:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"DID '{did_id}' not found.",
            )
        if not is_super and did.business_id and did.business_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"DID '{did.phone_number}' belongs to another organization.",
            )
        did.sip_account_id = account.id
        db.add(did)

    await db.commit()

    dids_stmt = select(DIDNumber).where(DIDNumber.sip_account_id == sip_account_id).order_by(DIDNumber.created_at.desc())
    dids = (await db.execute(dids_stmt)).scalars().all()
    return [
        SIPAccountDIDOut(
            id=str(d.id),
            phone_number=d.phone_number,
            friendly_name=getattr(d, "friendly_name", None),
            status=getattr(d, "status", "ACTIVE"),
            is_outbound_caller_id=getattr(d, "is_outbound_caller_id", False),
            sip_account_id=d.sip_account_id,
            organization_id=d.business_id,
            created_at=getattr(d, "created_at", None),
        )
        for d in dids
    ]


@router.delete("/{sip_account_id}/dids/{did_id}")
@router.delete("/{sip_account_id}/dids/{did_id}/")
@v1_router.delete("/{sip_account_id}/dids/{did_id}")
@v1_router.delete("/{sip_account_id}/dids/{did_id}/")
async def unassign_did_from_sip_account(
    sip_account_id: str,
    did_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Unlinks a DID from a SIP account."""
    is_super = is_super_admin_user(user)
    if is_super:
        stmt = select(SIPAccount).where(SIPAccount.id == sip_account_id)
    else:
        stmt = select(SIPAccount).where(
            SIPAccount.id == sip_account_id,
            SIPAccount.organization_id == tenant_id,
        )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    from app.db.models import DIDNumber
    did = await db.get(DIDNumber, did_id)
    if not did or did.sip_account_id != sip_account_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="DID not found or not assigned to this SIP account.",
        )

    did.sip_account_id = None
    db.add(did)
    await db.commit()
    return {"status": "unassigned", "did_id": did_id, "sip_account_id": sip_account_id}



@router.put("/{sip_account_id}", response_model=SIPAccountOut)
@router.put("/{sip_account_id}/", response_model=SIPAccountOut)
@v1_router.put("/{sip_account_id}", response_model=SIPAccountOut)
@v1_router.put("/{sip_account_id}/", response_model=SIPAccountOut)
async def update_sip_account(
    sip_account_id: str,
    data: SIPAccountUpdate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Updates a SIP account. Only authorized organization admins can update."""
    is_super = is_super_admin_user(user)
    if is_super:
        stmt = select(SIPAccount).where(SIPAccount.id == sip_account_id)
    else:
        stmt = select(SIPAccount).where(
            SIPAccount.id == sip_account_id,
            SIPAccount.organization_id == tenant_id,
        )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    # Super Admin can reassign organization
    if is_super and data.organization_id and data.organization_id.strip():
        target_biz = await db.get(Business, data.organization_id.strip())
        if not target_biz:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Target organization '{data.organization_id}' not found.",
            )
        account.organization_id = target_biz.id

    # Validate name uniqueness if changing name
    if data.name and data.name.strip() != account.name:
        existing_stmt = select(SIPAccount).where(
            SIPAccount.organization_id == account.organization_id,
            SIPAccount.name == data.name.strip(),
            SIPAccount.id != sip_account_id,
        )
        if (await db.execute(existing_stmt)).scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"A SIP account named '{data.name.strip()}' already exists.",
            )
        account.name = data.name.strip()

    if data.username is not None:
        account.username = data.username.strip()
    if data.auth_username is not None:
        account.auth_username = data.auth_username.strip() or None
    if data.domain is not None:
        account.domain = data.domain.strip() or None
    if data.host is not None:
        account.host = data.host.strip()
    if data.port is not None:
        account.port = data.port
    if data.transport is not None:
        account.transport = data.transport.strip().lower()
    if data.outbound_proxy is not None:
        account.outbound_proxy = data.outbound_proxy.strip() or None
    if data.caller_id is not None:
        account.caller_id = data.caller_id.strip() or None
    if data.direction is not None:
        account.direction = data.direction.strip().lower()
    if data.enabled is not None:
        account.enabled = data.enabled
    if data.max_channels is not None:
        account.max_channels = data.max_channels
    for prop in ("extension", "dialing_prefix", "display_name", "refresh_interval", "keep_alive_interval", "local_ip", "rtp_ip", "rtp_port", "stun_server", "sip_ws_url", "is_default"):
        val = getattr(data, prop, None)
        if val is not None:
            setattr(account, prop, val)

    merged_settings = dict(account.settings or {})
    if data.settings is not None:
        merged_settings.update(data.settings)
    for k in (
        "sip_server", "sip_login", "sip_public_address", "voicemail_number",
        "dial_plan", "webhook_base_url", "hide_caller_id", "media_encryption",
        "publish_presence", "allow_ip_rewrite", "ice", "disable_session_timers",
        "use_auth_user_in_from"
    ):
        v = getattr(data, k, None)
        if v is not None:
            merged_settings[k] = v
    if data.additional_dids is not None:
        merged_settings["additional_dids"] = data.additional_dids
    account.settings = merged_settings

    if data.is_default is True:
        await _unset_other_defaults(db, tenant_id, account.id)
        account.is_default = True

    # Password update: only if non-empty string provided
    if data.password:
        account.password_hash = encrypt_sip_secret(data.password)

    account.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    db.add(account)
    await db.commit()
    await db.refresh(account)

    # Synchronize primary caller_id and additional_dids into did_inventory
    from app.db.models import DIDNumber
    dids_to_register = []
    if account.caller_id and account.caller_id.strip():
        dids_to_register.append((account.caller_id.strip(), True))
    if data.additional_dids is not None:
        for ad in data.additional_dids:
            if ad and ad.strip() and ad.strip() != (account.caller_id or "").strip():
                dids_to_register.append((ad.strip(), False))

    for num, is_primary in dids_to_register:
        existing_did = (await db.execute(
            select(DIDNumber).where(DIDNumber.phone_number == num)
        )).scalars().first()
        if existing_did:
            existing_did.sip_account_id = account.id
            if not existing_did.business_id:
                existing_did.business_id = account.organization_id
            if is_primary:
                existing_did.is_outbound_caller_id = True
            db.add(existing_did)
        else:
            new_did = DIDNumber(
                id=str(uuid.uuid4()),
                business_id=account.organization_id,
                phone_number=num,
                sip_account_id=account.id,
                status="ACTIVE",
                is_outbound_caller_id=is_primary,
            )
            db.add(new_did)
    await db.commit()

    # Sync to carrier and trunk tables to maintain backwards-compatibility
    try:
        from app.services.sip_account_service import sync_sip_account_to_carrier_trunk
        await sync_sip_account_to_carrier_trunk(db, account, raw_password=data.password)
    except Exception as e:
        logger.warning(f"[SIP_ACCOUNTS] Error syncing to carrier/trunk on update: {e}")

    logger.info(f"[SIP_ACCOUNTS] Updated SIP account '{account.name}' (id={account.id})")

    try:
        from app.services.sip_inbound_daemon import notify_sip_account_changed
        notify_sip_account_changed()
    except Exception:
        pass

    camp_count_stmt = select(func.count(Campaign.id)).where(Campaign.sip_account_id == sip_account_id)
    camp_count = (await db.execute(camp_count_stmt)).scalar() or 0

    org = await db.get(Business, account.organization_id)
    org_name = org.name if org else "Default Organization"

    return _to_sip_account_out(account, org_name=org_name, assigned_campaigns_count=camp_count)


@router.post("/{sip_account_id}/set-default", response_model=SIPAccountOut)
@router.post("/{sip_account_id}/set-default/", response_model=SIPAccountOut)
@v1_router.post("/{sip_account_id}/set-default", response_model=SIPAccountOut)
@v1_router.post("/{sip_account_id}/set-default/", response_model=SIPAccountOut)
async def set_default_sip_account(
    sip_account_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Sets the designated SIP account as the organization default."""
    stmt = select(SIPAccount).where(
        SIPAccount.id == sip_account_id,
        SIPAccount.organization_id == tenant_id,
    )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    await _unset_other_defaults(db, tenant_id, account.id)
    account.is_default = True
    account.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(account)
    await db.commit()
    await db.refresh(account)

    try:
        from app.services.sip_inbound_daemon import notify_sip_account_changed
        notify_sip_account_changed()
    except Exception:
        pass

    camp_count_stmt = select(func.count(Campaign.id)).where(Campaign.sip_account_id == sip_account_id)
    camp_count = (await db.execute(camp_count_stmt)).scalar() or 0

    org = await db.get(Business, tenant_id)
    org_name = org.name if org else "Default Organization"
    return _to_sip_account_out(account, org_name=org_name, assigned_campaigns_count=camp_count)


@router.delete("/{sip_account_id}")
@router.delete("/{sip_account_id}/")
@v1_router.delete("/{sip_account_id}")
@v1_router.delete("/{sip_account_id}/")
async def delete_sip_account(
    sip_account_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Deletes a SIP account after enforcing safety business rules."""
    is_super = is_super_admin_user(user)
    if is_super:
        stmt = select(SIPAccount).where(SIPAccount.id == sip_account_id)
    else:
        stmt = select(SIPAccount).where(
            SIPAccount.id == sip_account_id,
            SIPAccount.organization_id == tenant_id,
        )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    # Business Rule 1: Check assigned campaigns
    camp_stmt = select(Campaign.id, Campaign.name).where(
        Campaign.sip_account_id == sip_account_id,
        Campaign.business_id == account.organization_id,
    )
    assigned_campaigns = (await db.execute(camp_stmt)).all()
    if assigned_campaigns:
        camp_names = ", ".join([f"'{c[1]}'" for c in assigned_campaigns[:3]])
        count = len(assigned_campaigns)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete SIP account '{account.name}': it is assigned to {count} campaign(s) "
                f"({camp_names}{'...' if count > 3 else ''}). Please reassign or delete these campaigns first."
            ),
        )

    # Business Rule 2: Check active in-flight calls
    active_calls_stmt = select(Call.id).where(
        Call.sip_account_id == sip_account_id,
        Call.status.in_(["RINGING", "IN_PROGRESS", "ringing", "in-progress", "dialing", "connected"]),
    )
    active_calls = (await db.execute(active_calls_stmt)).scalars().all()
    if active_calls:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete SIP account '{account.name}': {len(active_calls)} active calls "
                f"are currently in progress using this account."
            ),
        )

    # Business Rule 3: Cascade delete linked legacy carrier and trunk so auto-sync does not recreate it
    settings_dict = account.settings or {}
    carrier_id = settings_dict.get("carrier_id")
    trunk_id = settings_dict.get("trunk_id")
    
    from app.db.models import SIPCarrier, SIPTrunk
    if trunk_id:
        t = await db.get(SIPTrunk, trunk_id)
        if t:
            await db.delete(t)
    if carrier_id:
        c = await db.get(SIPCarrier, carrier_id)
        if c:
            rem_trunks = (await db.scalars(select(SIPTrunk).where(SIPTrunk.carrier_id == carrier_id))).all()
            for tr in rem_trunks:
                await db.delete(tr)
            await db.delete(c)
    else:
        # Also check for any carrier matching this account's host and username
        matched_carriers = (await db.scalars(
            select(SIPCarrier).where(
                SIPCarrier.sip_host.like(f"%{account.host}%"),
                SIPCarrier.username == account.username
            )
        )).all()
        for mc in matched_carriers:
            mc_trunks = (await db.scalars(select(SIPTrunk).where(SIPTrunk.carrier_id == mc.id))).all()
            for mt in mc_trunks:
                await db.delete(mt)
            await db.delete(mc)

    await db.delete(account)
    await db.commit()

    try:
        from app.services.sip_inbound_daemon import notify_sip_account_changed
        notify_sip_account_changed()
    except Exception:
        pass

    logger.info(f"[SIP_ACCOUNTS] Deleted SIP account '{account.name}' (id={sip_account_id})")
    return {"message": f"SIP account '{account.name}' deleted successfully", "id": sip_account_id}


@router.post("/{sip_account_id}/test-connection", response_model=SIPAccountTestResponse)
@router.post("/{sip_account_id}/test-connection/", response_model=SIPAccountTestResponse)
@v1_router.post("/{sip_account_id}/test-connection", response_model=SIPAccountTestResponse)
@v1_router.post("/{sip_account_id}/test-connection/", response_model=SIPAccountTestResponse)
async def test_sip_connection(
    sip_account_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
):
    """Executes live connectivity and socket probes against the SIP account."""
    is_super = is_super_admin_user(user)
    if is_super:
        stmt = select(SIPAccount).where(SIPAccount.id == sip_account_id)
    else:
        stmt = select(SIPAccount).where(
            SIPAccount.id == sip_account_id,
            SIPAccount.organization_id == tenant_id,
        )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    probe_res = await test_and_update_sip_account(db, account, timeout=4.0)

    return SIPAccountTestResponse(
        account_id=account.id,
        reachable=probe_res["reachable"],
        latency_ms=probe_res["latency_ms"],
        transport=probe_res["transport"],
        check_type=probe_res.get("check_type") or "SIP_OPTIONS",
        sip_status_code=probe_res.get("sip_status_code"),
        sip_status_reason=probe_res.get("sip_status_reason"),
        dns_resolved_ip=probe_res.get("dns_resolved_ip"),
        error=probe_res.get("error"),
        error_code=probe_res.get("error_code") or (None if probe_res["reachable"] else "SIP_OPTIONS_TIMEOUT"),
        suggested_action=probe_res.get("suggested_action"),
        retryable=probe_res.get("retryable"),
        timestamp=datetime.now(timezone.utc),
    )


@router.post("/{sip_account_id}/register", response_model=SIPAccountRegisterResponse)
@router.post("/{sip_account_id}/register/", response_model=SIPAccountRegisterResponse)
@v1_router.post("/{sip_account_id}/register", response_model=SIPAccountRegisterResponse)
@v1_router.post("/{sip_account_id}/register/", response_model=SIPAccountRegisterResponse)
async def register_sip_account(
    sip_account_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Initiates an active SIP REGISTER challenge and updates account registration status."""
    stmt = select(SIPAccount).where(
        SIPAccount.id == sip_account_id,
        SIPAccount.organization_id == tenant_id,
    )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    res = await register_and_update_sip_account(db, account, timeout=4.0)

    return SIPAccountRegisterResponse(
        account_id=account.id,
        status=res["status"],
        message=res["message"],
        is_registered=res["is_registered"],
        last_error=res.get("last_error"),
        error_code=res.get("error_code"),
        sip_status_code=res.get("sip_status_code"),
        retryable=bool(res.get("retryable", False)),
        suggested_action=res.get("suggested_action"),
        last_checked_at=res.get("last_checked_at"),
        timestamp=res["timestamp"],
    )


@router.post("/{sip_account_id}/toggle-status")
@router.post("/{sip_account_id}/toggle-status/")
@v1_router.post("/{sip_account_id}/toggle-status")
@v1_router.post("/{sip_account_id}/toggle-status/")
async def toggle_sip_account_status(
    sip_account_id: str,
    payload: Optional[SIPAccountToggleStatus] = None,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Enables or disables a SIP account."""
    is_super = is_super_admin_user(user)
    if is_super:
        stmt = select(SIPAccount).where(SIPAccount.id == sip_account_id)
    else:
        stmt = select(SIPAccount).where(
            SIPAccount.id == sip_account_id,
            SIPAccount.organization_id == tenant_id,
        )
    account = (await db.execute(stmt)).scalar_one_or_none()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SIP account not found or access denied.",
        )

    if payload is not None:
        account.enabled = payload.enabled
    else:
        account.enabled = not account.enabled

    account.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(account)
    await db.commit()
    await db.refresh(account)

    try:
        from app.services.sip_inbound_daemon import notify_sip_account_changed
        notify_sip_account_changed()
    except Exception:
        pass

    logger.info(
        f"[SIP_ACCOUNTS] Account '{account.name}' (id={account.id}) enabled={account.enabled}"
    )
    return {
        "id": account.id,
        "name": account.name,
        "enabled": account.enabled,
        "registration_status": account.registration_status,
    }
