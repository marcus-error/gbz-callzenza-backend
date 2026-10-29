import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Call, Campaign
from app.core.security import create_access_token, hash_password
from app.api.routes.websockets import global_ws_manager


@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_business_scoped_websocket_broadcast_isolation():
    """Verify Business A receives broadcasts meant for Business A, and Business B receives 0 events for Business A."""
    biz_a = f"biz_a_{uuid.uuid4().hex[:6]}"
    biz_b = f"biz_b_{uuid.uuid4().hex[:6]}"

    # Mock WebSockets
    class DummyWebSocket:
        def __init__(self):
            self.messages = []

        async def send_text(self, text: str):
            self.messages.append(text)

    ws_a = DummyWebSocket()
    ws_b = DummyWebSocket()

    global_ws_manager.active_connections.append({
        "websocket": ws_a,
        "business_id": biz_a,
        "user_id": "user_a",
    })
    global_ws_manager.active_connections.append({
        "websocket": ws_b,
        "business_id": biz_b,
        "user_id": "user_b",
    })

    try:
        # Broadcast event for Business A
        event_payload = {
            "event": "CALL_STATUS_CHANGED",
            "call_id": "call-123",
            "status": "CONFERENCE_3WAY_ACTIVE",
            "lead_name": "Test Lead",
        }
        await global_ws_manager.broadcast_to_business(biz_a, event_payload)

        # Assert Business A received the message
        assert len(ws_a.messages) == 1
        assert "CONFERENCE_3WAY_ACTIVE" in ws_a.messages[0]

        # Assert Business B received ZERO messages for Business A's call
        assert len(ws_b.messages) == 0

    finally:
        # Cleanup connections
        global_ws_manager.disconnect(ws_a)
        global_ws_manager.disconnect(ws_b)
