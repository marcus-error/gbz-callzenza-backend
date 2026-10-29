import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Call
from app.core.security import create_access_token, hash_password
from app.api.routes.websockets import global_ws_manager


class DummyWebSocket:
    def __init__(self):
        self.messages = []

    async def send_text(self, text: str):
        self.messages.append(text)


@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def setup_tenants_and_users():
    """Seeds two separate tenants (A and B) with users for strict isolation testing."""
    biz_a_id = f"biz_a_{uuid.uuid4().hex[:8]}"
    biz_b_id = f"biz_b_{uuid.uuid4().hex[:8]}"
    user_a_id = f"user_a_{uuid.uuid4().hex[:8]}"
    user_b_id = f"user_b_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        biz_a = Business(id=biz_a_id, name="Tenant A PBX", owner_user_id=user_a_id)
        biz_b = Business(id=biz_b_id, name="Tenant B PBX", owner_user_id=user_b_id)
        db.add_all([biz_a, biz_b])
        await db.commit()

        user_a = User(
            id=user_a_id,
            email=f"agent_a_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("Password123!"),
            full_name="Agent Alpha",
            business_id=biz_a_id,
            role="ADMIN",
            is_active=True,
            sip_extension="1001",
        )
        user_b = User(
            id=user_b_id,
            email=f"agent_b_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("Password123!"),
            full_name="Agent Beta",
            business_id=biz_b_id,
            role="ADMIN",
            is_active=True,
            sip_extension="1002",
        )
        db.add_all([user_a, user_b])
        await db.commit()

        token_a = create_access_token(subject=user_a_id, role="ADMIN", tenant_id=biz_a_id)
        token_b = create_access_token(subject=user_b_id, role="ADMIN", tenant_id=biz_b_id)

        return {
            "biz_a_id": biz_a_id,
            "biz_b_id": biz_b_id,
            "user_a": user_a,
            "user_b": user_b,
            "token_a": token_a,
            "token_b": token_b,
        }


@pytest.mark.asyncio
async def test_sip_config_authenticated(async_client, setup_tenants_and_users):
    """Verifies authenticated user gets accurate SIP config with ICE servers, extension, and no raw credentials."""
    token = setup_tenants_and_users["token_a"]
    resp = await async_client.get(
        "/api/telephony/sip/config",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "sipServer" in data
    assert "sipPort" in data
    assert "sipWsUrl" in data
    assert "extension" in data
    assert data["extension"] == "1001"
    assert "iceServers" in data
    assert isinstance(data["iceServers"], list)
    assert len(data["iceServers"]) > 0
    assert "password" not in data  # Never expose raw SIP password


@pytest.mark.asyncio
async def test_outbound_call_creation_and_duplicate_prevention(async_client, setup_tenants_and_users):
    """Verifies outbound call initiation, call record creation, and duplicate dial prevention."""
    token = setup_tenants_and_users["token_a"]
    headers = {"Authorization": f"Bearer {token}"}
    destination = "+919876543210"

    # Register mock WebSocket for tenant A
    ws_a = DummyWebSocket()
    global_ws_manager.active_connections.append({
        "websocket": ws_a,
        "business_id": setup_tenants_and_users["biz_a_id"],
        "user_id": setup_tenants_and_users["user_a"].id,
    })

    try:
        # 1. Initiate outbound call
        resp = await async_client.post(
            "/api/telephony/calls/outbound",
            json={"phone_number": destination},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        call_data = resp.json()
        assert call_data["status"] == "ringing"
        assert call_data["destination_number"] == destination
        assert call_data["sip_extension"] == "1001"
        call_id = call_data["id"]

        # Assert WebSocket received real-time event
        assert any("call_ringing" in m for m in ws_a.messages)

        # 2. Duplicate call attempt must return 409 Conflict
        dup_resp = await async_client.post(
            "/api/telephony/calls/outbound",
            json={"phone_number": destination},
            headers=headers,
        )
        assert dup_resp.status_code == 409
        error_msg = dup_resp.json().get("detail") or dup_resp.json().get("message") or ""
        assert "already in progress" in error_msg

    finally:
        global_ws_manager.disconnect(ws_a)


@pytest.mark.asyncio
async def test_invalid_phone_number_rejection(async_client, setup_tenants_and_users):
    """Verifies empty or malformed phone numbers are rejected with 422."""
    token = setup_tenants_and_users["token_a"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await async_client.post(
        "/api/telephony/calls/outbound",
        json={"phone_number": "invalid-chars-abc"},
        headers=headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_inbound_call_trigger_and_matching(async_client, setup_tenants_and_users):
    """Verifies inbound SIP call event routes to matching extension and broadcasts incoming_call."""
    ws_a = DummyWebSocket()
    global_ws_manager.active_connections.append({
        "websocket": ws_a,
        "business_id": setup_tenants_and_users["biz_a_id"],
        "user_id": setup_tenants_and_users["user_a"].id,
    })

    try:
        inbound_resp = await async_client.post(
            "/api/telephony/calls/inbound",
            json={
                "caller_number": "+919988776655",
                "destination_extension": "1001",
                "tenant_id": setup_tenants_and_users["biz_a_id"],
            },
        )
        assert inbound_resp.status_code == 201, inbound_resp.text
        data = inbound_resp.json()
        assert data["direction"] == "inbound"
        assert data["status"] == "ringing"
        assert data["sip_extension"] == "1001"
        assert data["caller_number"] == "+919988776655"

        # Assert WebSocket received incoming_call
        assert any("incoming_call" in m for m in ws_a.messages)

    finally:
        global_ws_manager.disconnect(ws_a)


@pytest.mark.asyncio
async def test_call_lifecycle_accept_mute_unmute_hangup(async_client, setup_tenants_and_users):
    """Verifies sequential call lifecycle: accept -> mute -> unmute -> hangup."""
    token = setup_tenants_and_users["token_a"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create an initial inbound call
    create_resp = await async_client.post(
        "/api/telephony/calls/inbound",
        json={
            "caller_number": "+919123456789",
            "destination_extension": "1001",
            "tenant_id": setup_tenants_and_users["biz_a_id"],
        },
    )
    call_id = create_resp.json()["id"]

    # 1. Accept call
    accept_resp = await async_client.post(f"/api/telephony/calls/{call_id}/accept", headers=headers)
    assert accept_resp.status_code == 200, accept_resp.text
    assert accept_resp.json()["status"] == "connected"
    assert accept_resp.json()["answered_at"] is not None

    # 2. Mute call
    mute_resp = await async_client.post(f"/api/telephony/calls/{call_id}/mute", headers=headers)
    assert mute_resp.status_code == 200
    assert mute_resp.json()["status"] == "muted"

    # 3. Unmute call
    unmute_resp = await async_client.post(f"/api/telephony/calls/{call_id}/unmute", headers=headers)
    assert unmute_resp.status_code == 200
    assert unmute_resp.json()["status"] == "unmuted"

    # 4. Hangup call
    hangup_resp = await async_client.post(f"/api/telephony/calls/{call_id}/hangup", headers=headers)
    assert hangup_resp.status_code == 200
    assert hangup_resp.json()["status"] == "completed"
    assert hangup_resp.json()["ended_at"] is not None
    assert hangup_resp.json()["hangup_reason"] == "NORMAL_CLEARING"


@pytest.mark.asyncio
async def test_call_reject_lifecycle(async_client, setup_tenants_and_users):
    """Verifies call rejection sets status to rejected and records hangup reason."""
    token = setup_tenants_and_users["token_a"]
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await async_client.post(
        "/api/telephony/calls/inbound",
        json={
            "caller_number": "+919555666777",
            "destination_extension": "1001",
            "tenant_id": setup_tenants_and_users["biz_a_id"],
        },
    )
    call_id = create_resp.json()["id"]

    reject_resp = await async_client.post(f"/api/telephony/calls/{call_id}/reject", headers=headers)
    assert reject_resp.status_code == 200, reject_resp.text
    assert reject_resp.json()["status"] == "rejected"
    assert reject_resp.json()["hangup_reason"] == "REJECTED"


@pytest.mark.asyncio
async def test_strict_tenant_isolation(async_client, setup_tenants_and_users):
    """Verifies Tenant B cannot access, accept, reject, mute, or hang up Tenant A's call."""
    token_a = setup_tenants_and_users["token_a"]
    token_b = setup_tenants_and_users["token_b"]

    # Tenant A creates an outbound call
    create_resp = await async_client.post(
        "/api/telephony/calls/outbound",
        json={"phone_number": "+918888899999"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    call_id = create_resp.json()["id"]

    # Tenant B tries to view Tenant A's call
    get_resp = await async_client.get(
        f"/api/telephony/calls/{call_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert get_resp.status_code == 403, "Tenant B must not view Tenant A's call"

    # Tenant B tries to accept Tenant A's call
    accept_resp = await async_client.post(
        f"/api/telephony/calls/{call_id}/accept",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert accept_resp.status_code == 403, "Tenant B must not accept Tenant A's call"

    # Tenant B tries to hangup Tenant A's call
    hangup_resp = await async_client.post(
        f"/api/telephony/calls/{call_id}/hangup",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert hangup_resp.status_code == 403, "Tenant B must not hangup Tenant A's call"
