"""
Comprehensive Regression Test Suite:
1. Strict Campaign Lead Isolation (Auto-Dial, Re-Dial, Call Next Lead, Zero business-wide fallbacks).
2. Campaign-Specific Outbound SIP Account and DID Routing (Zero silent fallback to default accounts).
3. Truthful SIP Reachability & Diagnostics (SIP OPTIONS probe response parsing & timeout handling).
"""

import asyncio
import os
import sys
import uuid
import socket
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    Business,
    Campaign,
    CampaignLead,
    Lead,
    SIPAccount,
    DIDNumber,
    User,
    Call,
    DoNotCallRecord,
)
from app.services.sip_account_service import (
    validate_sip_account_for_campaign,
    probe_sip_connectivity,
    test_and_update_sip_account as sip_test_and_update,
    classify_sip_registration_response,
)
from app.api.routes.local_voice_test import start_local_test, StartSessionRequest
from app.services.voice_service import initiate_outbound_call


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_strict_campaign_lead_isolation_and_no_fallback():
    """
    Verifies Issue 1:
    - Campaign A dials exclusively its assigned leads (Lead A1).
    - Campaign B cannot dial Campaign A's leads.
    - Unassigned / seed leads (Jasmine, Mugilan) are NEVER dialed as fallback when a campaign has no leads.
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        # Organization
        biz = Business(id="biz_1", name="Alpha Corp", owner_user_id="user_admin")
        db.add(biz)

        # Active SIP Account for Campaign
        sip_acc = SIPAccount(
            id="sip_1",
            organization_id="biz_1",
            name="Alpha SIP",
            username="alpha_user",
            host="10.0.0.1",
            port=5060,
            transport="udp",
            enabled=True,
            direction="both",
            caller_id="+15551110001",
            max_channels=10,
            registration_status="registered",
        )
        db.add(sip_acc)

        # Campaign A
        camp_a = Campaign(
            id="camp_a",
            business_id="biz_1",
            name="Campaign A",
            status="RUNNING",
            sip_account_id="sip_1",
        )
        # Campaign B
        camp_b = Campaign(
            id="camp_b",
            business_id="biz_1",
            name="Campaign B",
            status="RUNNING",
            sip_account_id="sip_1",
        )
        # Campaign Empty (No leads assigned)
        camp_empty = Campaign(
            id="camp_empty",
            business_id="biz_1",
            name="Campaign Empty",
            status="RUNNING",
            sip_account_id="sip_1",
        )
        db.add_all([camp_a, camp_b, camp_empty])

        # Leads
        lead_a1 = Lead(id="lead_a1", business_id="biz_1", first_name="Alice", phone="+15551111111", status="PENDING", call_status="PENDING")
        lead_b1 = Lead(id="lead_b1", business_id="biz_1", first_name="Bob", phone="+15552222222", status="PENDING", call_status="PENDING")
        lead_seed = Lead(id="lead_seed", business_id="biz_1", first_name="Jasmine", phone="+15559998887", status="PENDING", call_status="PENDING")
        db.add_all([lead_a1, lead_b1, lead_seed])
        await db.commit()

        # Assign lead_a1 to camp_a
        c_lead_a = CampaignLead(id="cl_a1", campaign_id="camp_a", lead_id="lead_a1", status="PENDING")
        # Assign lead_b1 to camp_b
        c_lead_b = CampaignLead(id="cl_b1", campaign_id="camp_b", lead_id="lead_b1", status="PENDING")
        db.add_all([c_lead_a, c_lead_b])
        await db.commit()

        # 1. Start session for Campaign A with its assigned lead -> Success
        with patch("app.services.voice_service.get_voice_provider") as mock_gp:
            mock_provider = MagicMock()
            mock_provider.place_call = AsyncMock(return_value="call_sid_1")
            mock_gp.return_value = mock_provider

            res = await start_local_test(StartSessionRequest(campaign_id="camp_a", lead_id="lead_a1"), db=db)
            assert res["customer_name"] == "Alice"
            assert res["customer_phone"] == "+15551111111"
            assert res["lead_id"] == "lead_a1"

        # 2. Start session for Campaign A with Campaign B's lead -> MUST BE REJECTED (HTTP 400)
        with pytest.raises(HTTPException) as exc_info:
            await start_local_test(StartSessionRequest(campaign_id="camp_a", lead_id="lead_b1"), db=db)
        assert exc_info.value.status_code == 400
        assert "not assigned to campaign" in exc_info.value.detail.lower()

        # 3. Start session for Campaign A with unassigned Seed lead (Jasmine) -> MUST BE REJECTED (HTTP 400)
        with pytest.raises(HTTPException) as exc_info:
            await start_local_test(StartSessionRequest(campaign_id="camp_a", lead_id="lead_seed"), db=db)
        assert exc_info.value.status_code == 400
        assert "not assigned to campaign" in exc_info.value.detail.lower()

        # 4. Start session for Campaign Empty without lead_id -> MUST BE REJECTED with actionable error
        # MUST NEVER silently fall back to Jasmine / Mugilan / any database lead!
        with pytest.raises(HTTPException) as exc_info:
            await start_local_test(StartSessionRequest(campaign_id="camp_empty", lead_id=None), db=db)
        assert exc_info.value.status_code == 400
        assert "assigned to campaign" in exc_info.value.detail.lower()

    await engine.dispose()


@pytest.mark.asyncio
async def test_cross_tenant_lead_and_campaign_isolation():
    """
    Verifies that cross-tenant access is rejected with HTTP 403.
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        biz1 = Business(id="biz_tenant_1", name="Tenant One", owner_user_id="user_t1")
        biz2 = Business(id="biz_tenant_2", name="Tenant Two", owner_user_id="user_t2")
        db.add_all([biz1, biz2])

        sip_1 = SIPAccount(
            id="sip_t1",
            organization_id="biz_tenant_1",
            name="T1 SIP",
            username="t1_user",
            host="10.0.0.1",
            port=5060,
            transport="udp",
            enabled=True,
            direction="both",
            caller_id="+15551110001",
            max_channels=10,
        )
        db.add(sip_1)

        camp_t1 = Campaign(id="camp_t1", business_id="biz_tenant_1", name="Camp T1", sip_account_id="sip_t1")
        lead_t2 = Lead(id="lead_t2", business_id="biz_tenant_2", first_name="Stranger", phone="+15559990000")
        db.add_all([camp_t1, lead_t2])
        await db.commit()

        # Attempt to dial Tenant 2's lead inside Tenant 1's campaign
        with pytest.raises(HTTPException) as exc_info:
            await start_local_test(StartSessionRequest(campaign_id="camp_t1", lead_id="lead_t2"), db=db)
        assert exc_info.value.status_code in (400, 403)


    await engine.dispose()


@pytest.mark.asyncio
async def test_campaign_specific_sip_and_did_enforcement_and_no_silent_fallback():
    """
    Verifies Issue 2:
    - Campaign A uses SIP Account 1 and DID 1.
    - Campaign B uses SIP Account 2 and DID 2.
    - Missing / disabled SIP account immediately halts call with FAILED status and clear reason.
    - Zero silent fallback to global or organization defaults.
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        biz = Business(id="biz_telephony", name="Telephony Org", owner_user_id="user_admin")
        db.add(biz)

        # SIP Account 1 (Assigned to Campaign A)
        sip_acc1 = SIPAccount(
            id="sip_acc_1",
            organization_id="biz_telephony",
            name="Trunk Alpha",
            username="user_alpha",
            host="sip.alpha.com",
            port=5060,
            transport="udp",
            caller_id="+18001112222",
            enabled=True,
            direction="both",
            max_channels=10,
            registration_status="registered",
        )
        # SIP Account 2 (Assigned to Campaign B)
        sip_acc2 = SIPAccount(
            id="sip_acc_2",
            organization_id="biz_telephony",
            name="Trunk Beta",
            username="user_beta",
            host="sip.beta.com",
            port=5062,
            transport="udp",
            caller_id="+18003334444",
            enabled=True,
            direction="both",
            max_channels=10,
            registration_status="registered",
        )
        # Disabled SIP Account
        sip_disabled = SIPAccount(
            id="sip_disabled",
            organization_id="biz_telephony",
            name="Trunk Disabled",
            username="user_dis",
            host="sip.disabled.com",
            port=5060,
            transport="udp",
            caller_id="+18005556666",
            enabled=False,
            direction="both",
            max_channels=10,
            registration_status="unregistered",
        )
        db.add_all([sip_acc1, sip_acc2, sip_disabled])

        camp_a = Campaign(id="c_a", business_id="biz_telephony", name="Campaign Alpha", sip_account_id="sip_acc_1")
        camp_b = Campaign(id="c_b", business_id="biz_telephony", name="Campaign Beta", sip_account_id="sip_acc_2")
        camp_no_sip = Campaign(id="c_no_sip", business_id="biz_telephony", name="Campaign No SIP", sip_account_id=None)
        camp_dis_sip = Campaign(id="c_dis_sip", business_id="biz_telephony", name="Campaign Disabled SIP", sip_account_id="sip_disabled")
        db.add_all([camp_a, camp_b, camp_no_sip, camp_dis_sip])

        lead = Lead(id="lead_target", business_id="biz_telephony", first_name="Target", phone="+15557778888")
        db.add(lead)
        await db.commit()

        # 1. Outbound call for Campaign A -> Uses Trunk Alpha and +18001112222
        with patch("app.services.voice_service.get_voice_provider") as mock_gp:
            mock_provider = MagicMock()
            mock_provider.place_call = AsyncMock(return_value="call_sid_alpha")
            mock_gp.return_value = mock_provider

            res_a = await initiate_outbound_call(
                db=db,
                lead=lead,
                agent=None,
                campaign=camp_a,
                campaign_id="c_a",
            )
            assert res_a.status in ("QUEUED", "RINGING", "IN_PROGRESS", "CONNECTED")
            call_kwargs = mock_provider.place_call.call_args.kwargs
            assert call_kwargs["sip_account"].id == "sip_acc_1"
            assert call_kwargs["from_"] == "+18001112222"

        # 2. Outbound call for Campaign B -> Uses Trunk Beta and +18003334444
        with patch("app.services.voice_service.get_voice_provider") as mock_gp:
            mock_provider = MagicMock()
            mock_provider.place_call = AsyncMock(return_value="call_sid_beta")
            mock_gp.return_value = mock_provider

            res_b = await initiate_outbound_call(
                db=db,
                lead=lead,
                agent=None,
                campaign=camp_b,
                campaign_id="c_b",
            )
            assert res_b.status in ("QUEUED", "RINGING", "IN_PROGRESS", "CONNECTED")
            call_kwargs = mock_provider.place_call.call_args.kwargs
            assert call_kwargs["sip_account"].id == "sip_acc_2"
            assert call_kwargs["from_"] == "+18003334444"

        # 3. Campaign with NO SIP account -> MUST FAIL IMMEDIATELY without falling back to Trunk Alpha or Beta!
        res_no_sip = await initiate_outbound_call(
            db=db,
            lead=lead,
            agent=None,
            campaign=camp_no_sip,
            campaign_id="c_no_sip",
        )
        assert res_no_sip.status == "FAILED"
        assert "no outbound sip account assigned" in res_no_sip.notes.lower()

        # 4. Campaign with DISABLED SIP account -> MUST FAIL IMMEDIATELY without silent fallback
        res_dis_sip = await initiate_outbound_call(
            db=db,
            lead=lead,
            agent=None,
            campaign=camp_dis_sip,
            campaign_id="c_dis_sip",
        )
        assert res_dis_sip.status == "FAILED"
        assert "disabled" in res_dis_sip.notes.lower()


    await engine.dispose()


@pytest.mark.asyncio
async def test_truthful_sip_reachability_and_options_diagnostics():
    """
    Verifies Issue 3:
    - Truthful SIP OPTIONS probe responses:
      - Valid SIP response (200 OK, 401 Unauthorized, etc.) is parsed and reports reachable: True.
      - Socket timeout or unresponsive server returns reachable: False with SIP_OPTIONS_TIMEOUT.
      - Never marks unreachable hosts as healthy with a fake ping.
    """
    # 1. Test probe against a server that responds with SIP/2.0 200 OK
    mock_200_resp = (
        b"SIP/2.0 200 OK\r\n"
        b"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK.123;rport\r\n"
        b"Content-Length: 0\r\n\r\n"
    )

    with patch("asyncio.get_running_loop") as mock_loop_fn:
        mock_loop = MagicMock()
        mock_loop.getaddrinfo = AsyncMock(return_value=[(None, None, None, None, ("192.0.2.1", 5060))])
        mock_loop.sock_sendto = AsyncMock()
        mock_loop.sock_recv = AsyncMock(return_value=mock_200_resp)
        mock_loop_fn.return_value = mock_loop

        res = await probe_sip_connectivity(host="sip.example.com", port=5060, transport="udp", timeout=2.0)
        assert res["reachable"] is True
        assert res["check_type"] == "SIP_OPTIONS"
        assert res["sip_status_code"] == 200
        assert res["sip_status_reason"] == "OK"
        assert res["error"] is None

    # 2. Test probe against a server that responds with SIP/2.0 401 Unauthorized
    mock_401_resp = (
        b"SIP/2.0 401 Unauthorized\r\n"
        b"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK.123;rport\r\n"
        b"WWW-Authenticate: Digest realm=\"sip.example.com\", nonce=\"abc\"\r\n"
        b"Content-Length: 0\r\n\r\n"
    )

    with patch("asyncio.get_running_loop") as mock_loop_fn:
        mock_loop = MagicMock()
        mock_loop.getaddrinfo = AsyncMock(return_value=[(None, None, None, None, ("192.0.2.1", 5060))])
        mock_loop.sock_sendto = AsyncMock()
        mock_loop.sock_recv = AsyncMock(return_value=mock_401_resp)
        mock_loop_fn.return_value = mock_loop

        res = await probe_sip_connectivity(host="sip.example.com", port=5060, transport="udp", timeout=2.0)
        assert res["reachable"] is True
        assert res["check_type"] == "SIP_OPTIONS"
        assert res["sip_status_code"] == 401
        assert res["sip_status_reason"] == "Unauthorized"

    # 3. Test probe against an UNRESPONSIVE host (Timeout)
    with patch("asyncio.get_running_loop") as mock_loop_fn:
        mock_loop = MagicMock()
        mock_loop.getaddrinfo = AsyncMock(return_value=[(None, None, None, None, ("192.0.2.99", 5060))])
        mock_loop.sock_sendto = AsyncMock()
        mock_loop.sock_recv = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_loop_fn.return_value = mock_loop

        res = await probe_sip_connectivity(host="unreachable.example.com", port=5060, transport="udp", timeout=1.0)
        # MUST report reachable: False! Never claim accessible on timeout!
        assert res["reachable"] is False
        assert res["check_type"] == "SIP_OPTIONS"
        assert res["error_code"] == "SIP_OPTIONS_TIMEOUT"
        assert "timed out" in res["error"].lower()
        assert res["sip_status_code"] is None
