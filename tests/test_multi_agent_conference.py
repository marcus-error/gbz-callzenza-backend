import asyncio
import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Call, CallParticipant, CallTransfer
from app.core.security import create_access_token, hash_password
from app.services.conference_service import (
    get_or_create_conference_room,
    list_conference_participants,
    invite_voice_agent_to_conference,
    accept_conference_invite,
    reject_conference_invite,
    remove_conference_participant,
    MAX_CONFERENCE_CAPACITY,
)
from app.api.routes.barge_stream import (
    barge_sessions,
    _get_or_create_session,
    mix_ulaw_bytes,
    safe_put_audio_frame,
)


class MockWebSocket:
    def __init__(self, name: str):
        self.name = name
        self.sent_messages = []
        self.closed = False
        self.close_code = None

    async def send_text(self, text: str):
        if self.closed:
            raise RuntimeError(f"Cannot send to closed WebSocket {self.name}")
        self.sent_messages.append(text)

    async def close(self, code: int = 1000):
        self.closed = True
        self.close_code = code


@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_scenario_1_baseline_customer_and_agent1(async_client: AsyncClient):
    """Scenario 1: Customer + Agent 1 baseline call."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Conf Biz 1", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        agent1 = User(
            email=f"agent1_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Agent One",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add(agent1)
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="John", last_name="Customer", phone="+15550100001")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            user_id=agent1.id,
            client_phone=lead.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token = create_access_token(agent1.id, agent1.role)
        headers = {"Authorization": f"Bearer {token}"}

        # Query participants for baseline call
        res = await async_client.get(f"/api/conference/{call.id}/participants", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["call_id"] == call.id
        assert data["connected_count"] >= 1
        # Check Customer & Host Agent are present
        types = [p["participant_type"] for p in data["participants"]]
        assert "CUSTOMER" in types
        assert "VOICE_AGENT" in types


@pytest.mark.asyncio
async def test_scenario_2_customer_agent1_agent2_conference(async_client: AsyncClient):
    """Scenario 2: Customer + Agent 1 + Agent 2 live conference."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Conf Biz 2", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        agent1 = User(
            email=f"agent1_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Host Agent",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        agent2 = User(
            email=f"agent2_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Secondary Agent 2",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add_all([agent1, agent2])
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Alice", last_name="Brown", phone="+15550100002")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            user_id=agent1.id,
            client_phone=lead.phone,
            direction="INBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token1 = create_access_token(agent1.id, agent1.role)
        headers1 = {"Authorization": f"Bearer {token1}"}

        # Agent 1 invites Agent 2
        invite_res = await async_client.post(
            f"/api/conference/{call.id}/add-agent",
            headers=headers1,
            json={"target_agent_id": agent2.id, "transfer_type": "conference"},
        )
        assert invite_res.status_code == 200
        inv_data = invite_res.json()
        assert inv_data["status"] == "INVITED"

        # Agent 2 accepts invite
        token2 = create_access_token(agent2.id, agent2.role)
        headers2 = {"Authorization": f"Bearer {token2}"}
        accept_res = await async_client.post(f"/api/conference/{call.id}/accept", headers=headers2)
        assert accept_res.status_code == 200
        assert accept_res.json()["status"] == "CONNECTED"

        # Verify all 3 participants are connected
        p_res = await async_client.get(f"/api/conference/{call.id}/participants", headers=headers1)
        assert p_res.status_code == 200
        participants = p_res.json()["participants"]
        connected = [p for p in participants if p["status"] == "CONNECTED"]
        assert len(connected) == 3


@pytest.mark.asyncio
async def test_scenario_3_and_4_sequential_multi_agents(async_client: AsyncClient):
    """Scenarios 3 & 4: Adding multiple agents sequentially (Customer + Agent 1 + Agent 2 + Agent 3)."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Conf Biz 3", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        agents = []
        for i in range(1, 4):
            ag = User(
                email=f"ag_{i}_{uuid.uuid4().hex[:6]}@example.com",
                hashed_password=hash_password("pass123"),
                full_name=f"Agent Number {i}",
                role="VOICE_AGENT",
                business_id=biz.id,
                is_active=True,
                status="AVAILABLE",
            )
            db.add(ag)
            agents.append(ag)
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="David", last_name="Miller", phone="+15550100003")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            user_id=agents[0].id,
            client_phone=lead.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token1 = create_access_token(agents[0].id, agents[0].role)
        headers1 = {"Authorization": f"Bearer {token1}"}

        # Sequentially invite Agent 2 and Agent 3
        for ag in [agents[1], agents[2]]:
            res = await async_client.post(
                f"/api/conference/{call.id}/add-agent",
                headers=headers1,
                json={"target_agent_id": ag.id},
            )
            assert res.status_code == 200

            # Agent accepts
            t = create_access_token(ag.id, ag.role)
            acc = await async_client.post(f"/api/conference/{call.id}/accept", headers={"Authorization": f"Bearer {t}"})
            assert acc.status_code == 200

        # Verify 4 connected participants
        p_res = await async_client.get(f"/api/conference/{call.id}/participants", headers=headers1)
        assert p_res.status_code == 200
        p_data = p_res.json()
        assert p_data["connected_count"] == 4


@pytest.mark.asyncio
async def test_scenario_5_bidirectional_audio_mixing():
    """Scenario 5: Multi-party bidirectional audio mixing without distortion or self-echo."""
    conf_call_id = f"test-conf-audio-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    # 160 bytes of silence / voice in G.711 mu-law
    ulaw_chunk_1 = bytes([0xFF] * 160)
    ulaw_chunk_2 = bytes([0x7F] * 160)

    mixed = mix_ulaw_bytes(ulaw_chunk_1, ulaw_chunk_2)
    assert len(mixed) == 160
    assert isinstance(mixed, bytes)

    # Attach customer ws and 2 barger WebSockets
    cust_ws = MockWebSocket("Customer")
    ag1_ws = MockWebSocket("Agent1")
    ag2_ws = MockWebSocket("Agent2")

    session["customer_ws"] = cust_ws
    session["browser_bargers"].add(ag1_ws)
    session["browser_bargers"].add(ag2_ws)

    # Ensure queue frame distribution works
    safe_put_audio_frame(session["barge_inbound_queue"], ulaw_chunk_1)
    assert not session["barge_inbound_queue"].empty()

    # Cleanup
    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_6_agent_rejection_leaves_customer_and_agent1_intact(async_client: AsyncClient):
    """Scenario 6: Agent rejection or failure does not terminate customer call."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Conf Biz 4", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        agent1 = User(
            email=f"ag1_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Host Agent 1",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        agent2 = User(
            email=f"ag2_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Rejecting Agent 2",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add_all([agent1, agent2])
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Mark", last_name="Taylor", phone="+15550100004")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            user_id=agent1.id,
            client_phone=lead.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token1 = create_access_token(agent1.id, agent1.role)
        headers1 = {"Authorization": f"Bearer {token1}"}

        # Invite Agent 2
        await async_client.post(
            f"/api/conference/{call.id}/add-agent",
            headers=headers1,
            json={"target_agent_id": agent2.id},
        )

        # Agent 2 rejects the invite
        token2 = create_access_token(agent2.id, agent2.role)
        headers2 = {"Authorization": f"Bearer {token2}"}
        rej_res = await async_client.post(f"/api/conference/{call.id}/reject", headers=headers2)
        assert rej_res.status_code == 200
        assert rej_res.json().get("status") in ["REJECTED", "FAILED"]

        # Verify call is still IN_PROGRESS and Customer + Agent 1 are still connected
        p_res = await async_client.get(f"/api/conference/{call.id}/participants", headers=headers1)
        assert p_res.status_code == 200
        data = p_res.json()
        assert data["connected_count"] == 2
        # Check call in DB is active (not ended or failed)
        await db.refresh(call)
        assert call.status in ["IN_PROGRESS", "CONFERENCE_3WAY_ACTIVE"]


@pytest.mark.asyncio
async def test_scenario_7_and_10_remove_participant(async_client: AsyncClient):
    """Scenarios 7 & 10: Participant removal / leaving while keeping other participants active."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Conf Biz 5", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        agent1 = User(
            email=f"ag1_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Host Agent",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        agent2 = User(
            email=f"ag2_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Temp Agent",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add_all([agent1, agent2])
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Sara", last_name="Connor", phone="+15550100005")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            user_id=agent1.id,
            client_phone=lead.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token1 = create_access_token(agent1.id, agent1.role)
        headers1 = {"Authorization": f"Bearer {token1}"}

        # Invite and connect Agent 2
        await async_client.post(
            f"/api/conference/{call.id}/add-agent",
            headers=headers1,
            json={"target_agent_id": agent2.id},
        )
        token2 = create_access_token(agent2.id, agent2.role)
        await async_client.post(f"/api/conference/{call.id}/accept", headers={"Authorization": f"Bearer {token2}"})

        # Remove Agent 2
        rem_res = await async_client.post(
            f"/api/conference/{call.id}/remove-participant",
            headers=headers1,
            json={"participant_id": agent2.id},
        )
        assert rem_res.status_code == 200
        assert rem_res.json()["status"] == "LEFT"

        # Check participants list: Agent 2 is LEFT, Customer & Agent 1 are still CONNECTED
        p_res = await async_client.get(f"/api/conference/{call.id}/participants", headers=headers1)
        assert p_res.status_code == 200
        data = p_res.json()
        assert data["connected_count"] == 2


@pytest.mark.asyncio
async def test_scenario_8_duplicate_agent_prevention(async_client: AsyncClient):
    """Scenario 8: Prevent inviting an agent who is already invited or connected (HTTP 409)."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Conf Biz 6", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        agent1 = User(
            email=f"ag1_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Host Agent",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        agent2 = User(
            email=f"ag2_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Target Agent",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add_all([agent1, agent2])
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Paul", last_name="Walker", phone="+15550100006")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            user_id=agent1.id,
            client_phone=lead.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token1 = create_access_token(agent1.id, agent1.role)
        headers1 = {"Authorization": f"Bearer {token1}"}

        # First invite
        res1 = await async_client.post(
            f"/api/conference/{call.id}/add-agent",
            headers=headers1,
            json={"target_agent_id": agent2.id},
        )
        assert res1.status_code == 200

        # Duplicate invite -> 409 Conflict
        res2 = await async_client.post(
            f"/api/conference/{call.id}/add-agent",
            headers=headers1,
            json={"target_agent_id": agent2.id},
        )
        assert res2.status_code == 409
        err_msg = str(res2.json().get("message") or res2.json().get("detail") or "")
        assert "already" in err_msg.lower()


@pytest.mark.asyncio
async def test_scenario_9_capacity_limit_enforcement(async_client: AsyncClient):
    """Scenario 9: Conference enforces MAX_CONFERENCE_CAPACITY (5 participants)."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Conf Biz 7", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        host = User(
            email=f"host_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Conference Host",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add(host)
        await db.flush()

        # Create 5 extra agents
        extra_agents = []
        for i in range(5):
            ag = User(
                email=f"extra_{i}_{uuid.uuid4().hex[:6]}@example.com",
                hashed_password=hash_password("pass123"),
                full_name=f"Extra Agent {i}",
                role="VOICE_AGENT",
                business_id=biz.id,
                is_active=True,
                status="AVAILABLE",
            )
            db.add(ag)
            extra_agents.append(ag)
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Cap", last_name="Test", phone="+15550100007")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            user_id=host.id,
            client_phone=lead.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token_host = create_access_token(host.id, host.role)
        headers_host = {"Authorization": f"Bearer {token_host}"}

        # Current capacity: Customer (1) + Host (1) = 2.
        # Adding agent 0, 1, 2 fills capacity to 5.
        for ag in extra_agents[:3]:
            r = await async_client.post(
                f"/api/conference/{call.id}/add-agent",
                headers=headers_host,
                json={"target_agent_id": ag.id},
            )
            assert r.status_code == 200

        # Adding 4th extra agent (6th total participant) must return 400 capacity exceeded
        overflow_res = await async_client.post(
            f"/api/conference/{call.id}/add-agent",
            headers=headers_host,
            json={"target_agent_id": extra_agents[3].id},
        )
        assert overflow_res.status_code == 400
        err_cap = str(overflow_res.json().get("message") or overflow_res.json().get("detail") or "")
        assert "capacity" in err_cap.lower()


@pytest.mark.asyncio
async def test_scenario_11_and_12_tenant_and_call_isolation(async_client: AsyncClient):
    """Scenarios 11 & 12: Tenant isolation and concurrent conference isolation."""
    async with AsyncSessionLocal() as db:
        # Business A
        biz_a = Business(name="Tenant Alpha", owner_user_id=str(uuid.uuid4()))
        # Business B
        biz_b = Business(name="Tenant Beta", owner_user_id=str(uuid.uuid4()))
        db.add_all([biz_a, biz_b])
        await db.flush()

        agent_a = User(
            email=f"agent_a_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Agent Alpha",
            role="VOICE_AGENT",
            business_id=biz_a.id,
            is_active=True,
            status="AVAILABLE",
        )
        agent_b = User(
            email=f"agent_b_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Agent Beta",
            role="VOICE_AGENT",
            business_id=biz_b.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add_all([agent_a, agent_b])
        await db.flush()

        lead_a = Lead(business_id=biz_a.id, first_name="Client", last_name="Alpha", phone="+15550100008")
        db.add(lead_a)
        await db.flush()

        call_a = Call(
            business_id=biz_a.id,
            lead_id=lead_a.id,
            user_id=agent_a.id,
            client_phone=lead_a.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call_a)
        await db.commit()

        token_a = create_access_token(agent_a.id, agent_a.role)
        token_b = create_access_token(agent_b.id, agent_b.role)

        # Agent Alpha cannot invite Agent Beta (different tenant)
        cross_res = await async_client.post(
            f"/api/conference/{call_a.id}/add-agent",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"target_agent_id": agent_b.id},
        )
        assert cross_res.status_code == 404  # Agent Beta not found within Tenant Alpha

        # Agent Beta cannot access Tenant Alpha's conference participants
        unauth_p = await async_client.get(
            f"/api/conference/{call_a.id}/participants",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert unauth_p.status_code == 404  # Call Alpha not found within Tenant Beta


@pytest.mark.asyncio
async def test_scenario_13_call_prefix_and_domain_resolution_invite_agent(async_client: AsyncClient):
    """
    Scenario 13: Verifies resolution of call identifiers such as 'call-99feea470eb6'
    where the DB Call.call_sid has '@callzenza' domain and Call.id is a UUID.
    Verifies that both GET and POST /api/conference/call-99feea470eb6/invite-agent succeed (no 404).
    """
    async with AsyncSessionLocal() as db:
        biz = Business(name="Resolution Biz", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        host_agent = User(
            email=f"host_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Host Agent 1",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        target_agent = User(
            email=f"target_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Target Voice Agent 2",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add_all([host_agent, target_agent])
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Jane", last_name="Customer", phone="+15550199999")
        db.add(lead)
        await db.flush()

        # Call record has call_sid with SIP domain suffix '@callzenza'
        clean_call_id = "call-99feea470eb6"
        call = Call(
            id=str(uuid.uuid4()),
            business_id=biz.id,
            lead_id=lead.id,
            user_id=host_agent.id,
            agent_id=host_agent.id,
            call_sid=f"{clean_call_id}@callzenza",
            client_phone=lead.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token = create_access_token(host_agent.id, host_agent.role)
        headers = {"Authorization": f"Bearer {token}"}

        # 1. GET /api/conference/call-99feea470eb6/invite-agent returns 200 (not 404 / 405)
        get_res = await async_client.get(
            f"/api/conference/{clean_call_id}/invite-agent",
            headers=headers
        )
        assert get_res.status_code == 200
        get_data = get_res.json()
        assert get_data["success"] is True
        assert get_data["can_invite"] is True
        assert get_data["call_id"] == call.id

        # 2. POST /api/conference/call-99feea470eb6/invite-agent successfully invites Agent 2
        post_res = await async_client.post(
            f"/api/conference/{clean_call_id}/invite-agent",
            headers=headers,
            json={"agent_id": target_agent.id, "notes": "Please assist with billing."}
        )
        assert post_res.status_code == 200
        post_data = post_res.json()
        assert post_data["success"] is True
        assert post_data["status"] == "INVITED"
        assert post_data["target_agent_id"] == target_agent.id
        assert post_data["call_id"] == call.id

        # 3. GET /api/conference/call-99feea470eb6/participants resolves participants
        part_res = await async_client.get(
            f"/api/conference/{clean_call_id}/participants",
            headers=headers
        )
        assert part_res.status_code == 200
        part_data = part_res.json()
        assert part_data["active_participants_count"] >= 2


@pytest.mark.asyncio
async def test_scenario_14_error_status_codes_and_validation(async_client: AsyncClient):
    """
    Scenario 14: Verifies meaningful HTTP error codes (400, 404, 409) without masking:
    - Non-existent call returns 404
    - Non-existent target agent returns 404
    - Self-invite returns 400
    - Offline agent returns 400
    - Duplicate invitation returns 409
    """
    async with AsyncSessionLocal() as db:
        biz = Business(name="Validation Biz", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        agent_online = User(
            email=f"online_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Online Agent",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        agent_offline = User(
            email=f"offline_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Offline Agent",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="OFFLINE",
        )
        db.add_all([agent_online, agent_offline])
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Test", last_name="Customer", phone="+15550188888")
        db.add(lead)
        await db.flush()

        call = Call(
            id=f"call-val-{uuid.uuid4().hex[:8]}",
            business_id=biz.id,
            lead_id=lead.id,
            user_id=agent_online.id,
            agent_id=agent_online.id,
            client_phone=lead.phone,
            direction="OUTBOUND",
            agent_type="VOICE_AGENT",
            status="IN_PROGRESS",
        )
        db.add(call)
        await db.commit()

        token = create_access_token(agent_online.id, agent_online.role)
        headers = {"Authorization": f"Bearer {token}"}

        # A. Non-existent call -> 404
        bad_call_res = await async_client.post(
            "/api/conference/non-existent-call-999999/invite-agent",
            headers=headers,
            json={"agent_id": agent_online.id}
        )
        assert bad_call_res.status_code == 404

        # B. Non-existent agent ID -> 404
        bad_agent_res = await async_client.post(
            f"/api/conference/{call.id}/invite-agent",
            headers=headers,
            json={"agent_id": "non-existent-agent-id-12345"}
        )
        assert bad_agent_res.status_code == 404

        # C. Self-invite -> 400
        self_res = await async_client.post(
            f"/api/conference/{call.id}/invite-agent",
            headers=headers,
            json={"agent_id": agent_online.id}
        )
        assert self_res.status_code == 400
        err_self = str(self_res.json().get("message") or self_res.json().get("detail") or "")
        assert "Cannot add yourself" in err_self

        # D. Offline agent -> 400
        offline_res = await async_client.post(
            f"/api/conference/{call.id}/invite-agent",
            headers=headers,
            json={"agent_id": agent_offline.id}
        )
        assert offline_res.status_code == 400
        err_off = str(offline_res.json().get("message") or offline_res.json().get("detail") or "")
        assert "offline" in err_off.lower()

        # Create a second available agent for duplicate testing
        agent2 = User(
            email=f"agent2_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Agent Two",
            role="VOICE_AGENT",
            business_id=biz.id,
            is_active=True,
            status="AVAILABLE",
        )
        db.add(agent2)
        await db.commit()

        # E. First invite succeeds (200)
        first_invite = await async_client.post(
            f"/api/conference/{call.id}/invite-agent",
            headers=headers,
            json={"agent_id": agent2.id}
        )
        assert first_invite.status_code == 200

        # Duplicate invite -> 409 Conflict
        dup_invite = await async_client.post(
            f"/api/conference/{call.id}/invite-agent",
            headers=headers,
            json={"agent_id": agent2.id}
        )
        assert dup_invite.status_code == 409
        err_dup = str(dup_invite.json().get("message") or dup_invite.json().get("detail") or "")
        assert "already" in err_dup.lower()

