import uuid
import pytest
import pytest_asyncio
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Campaign, CampaignLead, Expert, Call, SIPAccount
from app.core.security import create_access_token, hash_password


@pytest_asyncio.fixture
async def dial_test_env():
    async with AsyncSessionLocal() as db:
        biz = Business(name="Dial Test Biz", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        # Create active SIP Account for this business
        sip_acc = SIPAccount(
            name="Test PBX Outbound Trunk",
            organization_id=biz.id,
            host="49.249.142.147",
            port=5060,
            username="09410087540",
            caller_id="09410087540",
            transport="UDP",
            direction="both",
            enabled=True,
            registration_status="registered",
        )
        db.add(sip_acc)
        await db.flush()

        # Create human Voice Agent (User + Expert)
        user_va = User(
            email=f"va_dial_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Authorized Voice Agent",
            role="VOICE_AGENT",
            business_id=biz.id,
            status="AVAILABLE",
        )
        db.add(user_va)
        await db.flush()

        expert_va = Expert(
            business_id=biz.id,
            user_id=user_va.id,
            name="Authorized Voice Agent",
            email=user_va.email,
            status="AVAILABLE",
        )
        db.add(expert_va)
        await db.flush()

        # Create Campaign assigned to this Voice Agent
        campaign = Campaign(
            name="Outbound Lead Campaign",
            business_id=biz.id,
            voice_agent_id=expert_va.id,
            agent_type="HUMAN",
            sip_account_id=sip_acc.id,
            status="ACTIVE",
            schedule={"script": "Hello {{name}}, this is Voice Agent calling."},
        )
        db.add(campaign)
        await db.flush()

        # Create 2 assigned leads for this campaign
        lead1 = Lead(
            business_id=biz.id,
            first_name="Customer",
            last_name="Alpha",
            phone="0119788270678370",
            status="NEW",
            call_status="NEW",
            assigned_expert_id=expert_va.id,
        )
        lead2 = Lead(
            business_id=biz.id,
            first_name="Customer",
            last_name="Beta",
            phone="0119788608949822",
            status="NEW",
            call_status="NEW",
            assigned_expert_id=expert_va.id,
        )
        db.add_all([lead1, lead2])
        await db.flush()

        cl1 = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead1.id,
            status="PENDING",
            assigned_expert_id=expert_va.id,
            attempt_count=0,
        )
        cl2 = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead2.id,
            status="PENDING",
            assigned_expert_id=expert_va.id,
            attempt_count=0,
        )
        db.add_all([cl1, cl2])
        await db.commit()

        token = create_access_token(user_va.id, user_va.role)

        yield {
            "biz_id": biz.id,
            "user": user_va,
            "token": token,
            "campaign": campaign,
            "sip_acc": sip_acc,
            "lead1": lead1,
            "lead2": lead2,
        }


@pytest.mark.asyncio
async def test_campaign_dial_next_customer_flow(dial_test_env):
    """Test A: Verify Campaign Lead Dialing ('Dial Next Customer' flow).
    
    1. Retrieve next eligible lead via /api/voice/leads/next.
    2. Initiate call via /api/voice/calls/initiate.
    3. Verify real call record, SIP account assignment, and phone normalization.
    """
    token = dial_test_env["token"]
    headers = {"Authorization": f"Bearer {token}"}
    campaign_id = dial_test_env["campaign"].id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Step 1: Click "Dial Next Customer" -> Fetch next lead
        res_next = await client.get(
            f"/api/voice/leads/next?campaign_id={campaign_id}",
            headers=headers
        )
        assert res_next.status_code == 200
        data_next = res_next.json()
        assert data_next["lead"] is not None
        lead_id = data_next["lead"]["id"]
        assert lead_id == dial_test_env["lead1"].id
        raw_phone = data_next["lead"]["phone"]
        assert raw_phone == "0119788270678370"

        # Step 2: Frontend initiates call for retrieved lead
        initiate_payload = {
            "lead_id": lead_id,
            "phone_number": raw_phone,
            "campaign_id": campaign_id,
            "agent_type": "HUMAN",
        }
        with patch("app.services.voice_service.SIPVoiceProvider.place_call", new_callable=AsyncMock, return_value="call-test-cid-12345"):
            res_call = await client.post(
                "/api/voice/calls/initiate",
                json=initiate_payload,
                headers=headers
            )
            assert res_call.status_code == 200
            call_data = res_call.json()
            assert call_data["status"] == "RINGING"
            call_id = call_data["call_id"]

        # Step 3: Verify DB state
        async with AsyncSessionLocal() as db:
            call_row = await db.get(Call, call_id)
            assert call_row is not None
            assert call_row.status == "RINGING"
            assert call_row.direction == "OUTBOUND"
            assert call_row.agent_type == "HUMAN"
            assert call_row.lead_id == lead_id
            assert call_row.campaign_id == campaign_id
            # Normalized E.164 phone number
            assert call_row.to_number == "+918270678370"
            assert call_row.sip_account_id == dial_test_env["sip_acc"].id


@pytest.mark.asyncio
async def test_manual_dial_pad_flow_independent_of_campaign(dial_test_env):
    """Test B: Verify Manual Dial Pad Call Flow.
    
    1. Agent enters manual number into dial pad and clicks Call.
    2. Works independently of selected campaign lead without associating old lead.
    3. Resolves active outbound SIP account automatically.
    """
    token = dial_test_env["token"]
    headers = {"Authorization": f"Bearer {token}"}
    manual_test_number = "9876543210"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Agent types number in dial pad and clicks Call
        # lead_id is NOT provided (or decoupled)
        manual_payload = {
            "phone_number": manual_test_number,
            "agent_type": "HUMAN",
        }
        with patch("app.services.voice_service.SIPVoiceProvider.place_call", new_callable=AsyncMock, return_value="call-test-cid-12345"):
            res_call = await client.post(
                "/api/voice/calls/initiate",
                json=manual_payload,
                headers=headers
            )
            assert res_call.status_code == 200
            call_data = res_call.json()
            assert call_data["status"] == "RINGING"
            call_id = call_data["call_id"]

        # Verify DB Call record
        async with AsyncSessionLocal() as db:
            call_row = await db.get(Call, call_id)
            assert call_row is not None
            assert call_row.to_number == "+919876543210"
            assert call_row.direction == "OUTBOUND"
            assert call_row.agent_type == "HUMAN"
            # SIP account is automatically resolved from organization active outbound account
            assert call_row.sip_account_id == dial_test_env["sip_acc"].id
            # Resolved or created lead matches the dialed number
            lead_row = await db.get(Lead, call_row.lead_id)
            assert lead_row is not None
            assert manual_test_number in lead_row.phone or lead_row.phone == "+919876543210"


@pytest.mark.asyncio
async def test_manual_dial_pad_decouples_mismatched_active_lead(dial_test_env):
    """Verify that if an active lead was open in UI, but user typed a different number into dial pad,
    backend decouples from the old lead and does not misattribute the call.
    """
    token = dial_test_env["token"]
    headers = {"Authorization": f"Bearer {token}"}
    old_lead_id = dial_test_env["lead1"].id
    different_number = "9655152448"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Payload carries previous lead_id, but the number dialed is different
        payload = {
            "lead_id": old_lead_id,
            "phone_number": different_number,
            "agent_type": "HUMAN",
        }
        with patch("app.services.voice_service.SIPVoiceProvider.place_call", new_callable=AsyncMock, return_value="call-test-cid-12345"):
            res = await client.post(
                "/api/voice/calls/initiate",
                json=payload,
                headers=headers
            )
            assert res.status_code == 200
            call_id = res.json()["call_id"]

        async with AsyncSessionLocal() as db:
            call_row = await db.get(Call, call_id)
            assert call_row is not None
            # Must NOT be attributed to the old lead
            assert call_row.lead_id != old_lead_id
            assert call_row.to_number == "+919655152448"


@pytest.mark.asyncio
async def test_dial_duplicate_click_prevention(dial_test_env):
    """Verify repeated rapid clicks within 3 seconds return the existing call without spawning duplicates."""
    token = dial_test_env["token"]
    headers = {"Authorization": f"Bearer {token}"}
    test_number = "9410087540"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "phone_number": test_number,
            "agent_type": "HUMAN",
        }
        with patch("app.services.voice_service.SIPVoiceProvider.place_call", new_callable=AsyncMock, return_value="call-test-cid-12345"):
            # First click
            res1 = await client.post("/api/voice/calls/initiate", json=payload, headers=headers)
            assert res1.status_code == 200
            call_id_1 = res1.json()["call_id"]

            # Immediate rapid second click
            res2 = await client.post("/api/voice/calls/initiate", json=payload, headers=headers)
            assert res2.status_code == 200
            call_id_2 = res2.json()["call_id"]

            # Should return the same call ID without initiating a duplicate call
            assert call_id_1 == call_id_2


@pytest.mark.asyncio
async def test_telephony_provider_timeout_handled_gracefully(dial_test_env):
    """Verify that if the telephony provider encounters a timeout, the backend logs a notice
    and allows the agent call session to proceed without crashing the Agent Dashboard."""
    token = dial_test_env["token"]
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    with patch("app.services.voice_service.SIPVoiceProvider.place_call", new_callable=AsyncMock, side_effect=RuntimeError("SIP Trunk Route Timeout")):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {
                "phone_number": "8270678370",
                "agent_type": "HUMAN",
            }
            res = await client.post("/api/voice/calls/initiate", json=payload, headers=headers)
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "RINGING"
            assert data["call_id"] is not None
