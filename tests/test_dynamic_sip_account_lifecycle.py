import asyncio
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import (
    Business,
    SIPAccount,
    Campaign,
    Call,
)
from app.services.sip_account_service import (
    get_organization_sip_account,
    validate_sip_account_for_campaign,
)
from app.services.voice_service import (
    ACTIVE_SIP_CALLS,
    allocate_unique_rtp_socket,
)
from app.api.routes.sip_accounts import _to_sip_account_out, _unset_other_defaults


@pytest_asyncio.fixture
async def setup_test_orgs():
    async with AsyncSessionLocal() as db:
        biz1 = Business(name=f"Org1_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        biz2 = Business(name=f"Org2_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add_all([biz1, biz2])
        await db.commit()
        await db.refresh(biz1)
        await db.refresh(biz2)

        yield biz1, biz2


@pytest.mark.asyncio
async def test_sip_account_encryption_masking_and_properties(setup_test_orgs):
    """
    Test 1: Secret encryption, masking, and property getters/setters.
    """
    biz1, _ = setup_test_orgs
    async with AsyncSessionLocal() as db:
        plain_secret = "super_secret_sip_password_123!"
        acc = SIPAccount(
            business_id=biz1.id,
            name="Airtel Enterprise SIP",
            username="airtel_user_01",
            password=plain_secret,
            host="sip.airtel.in",
            port=5060,
            transport="udp",
            caller_id="+919876543210",
            direction="both",
            enabled=True,
            extension="1001",
            dialing_prefix="0",
            display_name="CallZenza Sales",
            refresh_interval=1800,
            keep_alive_interval=20,
            local_ip="192.168.1.50",
            rtp_ip="203.0.113.5",
            rtp_port=12000,
            stun_server="stun.l.google.com:19302",
            sip_ws_url="wss://sip.airtel.in/ws",
            is_default=True,
        )
        db.add(acc)
        await db.commit()
        await db.refresh(acc)

        # 1. Stored encrypted_password must NOT equal plaintext
        assert acc.encrypted_password is not None
        assert acc.encrypted_password != plain_secret
        assert plain_secret not in acc.encrypted_password

        # 2. Decrypted password property must return original plaintext
        assert acc.password == plain_secret

        # 3. Settings JSON contains extended fields
        assert acc.extension == "1001"
        assert acc.dialing_prefix == "0"
        assert acc.display_name == "CallZenza Sales"
        assert acc.refresh_interval == 1800
        assert acc.keep_alive_interval == 20
        assert acc.local_ip == "192.168.1.50"
        assert acc.rtp_ip == "203.0.113.5"
        assert acc.rtp_port == 12000
        assert acc.stun_server == "stun.l.google.com:19302"
        assert acc.sip_ws_url == "wss://sip.airtel.in/ws"
        assert acc.is_default is True

        # 4. Outbound schema masking (_to_sip_account_out)
        out = _to_sip_account_out(acc, org_name="Org 1", assigned_campaigns_count=2)
        assert out.name == "Airtel Enterprise SIP"
        assert out.has_password is True
        assert out.masked_password == "••••••••"
        # Ensure raw password is NEVER leaked in out dict/schema
        out_dict = out.model_dump()
        assert "password" not in out_dict or out_dict["password"] is None
        assert out.extension == "1001"
        assert out.dialing_prefix == "0"
        assert out.display_name == "CallZenza Sales"
        assert out.is_default is True
        assert out.assigned_campaigns_count == 2


@pytest.mark.asyncio
async def test_default_account_switching(setup_test_orgs):
    """
    Test 2: Setting a SIP account as default automatically unsets sibling defaults in the same organization.
    """
    biz1, _ = setup_test_orgs
    async with AsyncSessionLocal() as db:
        acc1 = SIPAccount(
            business_id=biz1.id,
            name="Primary SIP 1",
            username="user_01",
            password="secret1",
            host="sip1.example.com",
            port=5060,
            is_default=True,
        )
        acc2 = SIPAccount(
            business_id=biz1.id,
            name="Backup SIP 2",
            username="user_02",
            password="secret2",
            host="sip2.example.com",
            port=5060,
            is_default=False,
        )
        db.add_all([acc1, acc2])
        await db.commit()
        await db.refresh(acc1)
        await db.refresh(acc2)

        assert acc1.is_default is True
        assert acc2.is_default is False

        # Switch default to acc2
        await _unset_other_defaults(db, biz1.id, acc2.id)
        acc2.is_default = True
        await db.commit()
        await db.refresh(acc1)
        await db.refresh(acc2)

        assert acc1.is_default is False
        assert acc2.is_default is True


@pytest.mark.asyncio
async def test_tenant_isolation(setup_test_orgs):
    """
    Test 3: Cross-tenant isolation. Org 2 cannot resolve or access Org 1's SIP account.
    """
    biz1, biz2 = setup_test_orgs
    async with AsyncSessionLocal() as db:
        acc_org1 = SIPAccount(
            business_id=biz1.id,
            name="Org1 Private Trunk",
            username="trunk_org1",
            password="secret_org1",
            host="sip.org1.com",
            port=5060,
            is_default=True,
            enabled=True,
        )
        db.add(acc_org1)
        await db.commit()
        await db.refresh(acc_org1)

        # 1. Org 1 successfully resolves its own account
        resolved = await get_organization_sip_account(db, biz1.id, acc_org1.id)
        assert resolved is not None
        assert resolved.id == acc_org1.id

        # 2. Org 2 trying to access Org 1's SIP account must return None (strictly rejected)
        resolved_cross = await get_organization_sip_account(db, biz2.id, acc_org1.id)
        assert resolved_cross is None

        # 3. With raise_on_explicit_failure=True, it raises ValueError
        with pytest.raises(ValueError) as exc_info:
            await get_organization_sip_account(db, biz2.id, acc_org1.id, raise_on_explicit_failure=True)
        assert "Tenant isolation violation" in str(exc_info.value)


@pytest.mark.asyncio
async def test_campaign_sip_assignment_and_validation(setup_test_orgs):
    """
    Test 4: Campaign creation with selected SIP account and strict failure policy.
    Never silently fallback if an explicitly assigned SIP account is missing or disabled.
    """
    biz1, _ = setup_test_orgs
    async with AsyncSessionLocal() as db:
        acc = SIPAccount(
            business_id=biz1.id,
            name="Campaign Dedicated Trunk",
            username="camp_user",
            password="camp_secret",
            host="sip.camp.com",
            port=5060,
            enabled=True,
        )
        db.add(acc)
        await db.commit()
        await db.refresh(acc)

        # 1. Valid enabled account resolves for campaign
        is_val, err_msg, validated = await validate_sip_account_for_campaign(db, acc.id, biz1.id)
        assert is_val is True
        assert err_msg is None
        assert validated is not None
        assert validated.id == acc.id

        # 2. Disabled account must fail validation
        acc.enabled = False
        await db.commit()
        is_val, err_msg, disabled_acc = await validate_sip_account_for_campaign(db, acc.id, biz1.id)
        assert is_val is False
        assert "disabled" in err_msg.lower()

        # 3. Non-existent account ID must fail validation
        fake_id = str(uuid.uuid4())
        is_val, err_msg, fake_acc = await validate_sip_account_for_campaign(db, fake_id, biz1.id)
        assert is_val is False
        assert "not belong" in err_msg.lower() or "not exist" in err_msg.lower()


@pytest.mark.asyncio
async def test_runtime_resolution_unassigned_fallback_to_default(setup_test_orgs):
    """
    Test 5: When campaign has no explicit sip_account_id (None), runtime safely falls back
    to the organization's designated default SIP account.
    """
    biz1, _ = setup_test_orgs
    async with AsyncSessionLocal() as db:
        default_acc = SIPAccount(
            business_id=biz1.id,
            name="Org Default Gateway",
            username="gw_user",
            password="gw_password",
            host="sip.gateway.com",
            port=5060,
            is_default=True,
            enabled=True,
        )
        db.add(default_acc)
        await db.commit()
        await db.refresh(default_acc)

        # Pass None as sip_account_id
        resolved = await get_organization_sip_account(db, biz1.id, None)
        assert resolved is not None
        assert resolved.id == default_acc.id


@pytest.mark.asyncio
async def test_custom_rtp_port_allocation():
    """
    Test 6: allocate_unique_rtp_socket accepts custom preferred_port configured on the SIP account.
    """
    # Test preferred port allocation
    preferred_port = 14500
    sock, port = allocate_unique_rtp_socket(preferred_port=preferred_port)
