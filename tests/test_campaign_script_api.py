import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.security import create_access_token


import uuid
import pytest_asyncio
from app.db.database import AsyncSessionLocal
from app.models.tenant import Tenant
from app.db.models import User
from app.core.security import hash_password


@pytest_asyncio.fixture
async def auth_headers():
    uid = uuid.uuid4().hex[:8]
    async with AsyncSessionLocal() as session:
        tenant = Tenant(name=f"Test Tenant {uid}", slug=f"tenant-{uid}", status="active")
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

        user = User(
            email=f"admin_{uid}@callzenza.com",
            hashed_password=hash_password("password123"),
            full_name=f"Script Admin {uid}",
            role="SUPER_ADMIN",
            business_id=tenant.id,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    token = create_access_token(
        subject=user.id,
        role="SUPER_ADMIN",
        tenant_id=tenant.id,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_api_parse_script_happy_and_pause(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/campaigns/script/parse",
            headers=auth_headers,
            json={
                "script": '<emotion type="happy" />\nGood morning, sir.\n<break time="1.0s" />\nHow are you today?',
                "lead_name": "John Doe",
                "strict": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["segments"]) == 3
        assert data["segments"][0]["emotion"] == "happy"
        assert data["segments"][1]["type"] == "pause"
        assert data["segments"][1]["duration"] == 1.0
        assert "happy" in data["detected_emotions"]
        assert data["total_pause_duration"] == 1.0


@pytest.mark.asyncio
async def test_api_parse_script_multiple_emotions(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/campaigns/script/parse",
            headers=auth_headers,
            json={
                "script": (
                    '<emotion type="happy" />\nGood morning!\n'
                    '<break time="0.5s" />\n'
                    '<emotion type="neutral" />\nLet me explain.\n'
                    '<emotion type="surprised" />\nThat is amazing!'
                ),
                "strict": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["detected_emotions"]) == {"happy", "neutral", "surprised"}
        assert data["total_pause_duration"] == 0.5


@pytest.mark.asyncio
async def test_api_parse_script_invalid_emotion_422(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/campaigns/script/parse",
            headers=auth_headers,
            json={
                "script": '<emotion type="angry" />\nWhy did this happen?',
                "strict": True,
            },
        )
        assert resp.status_code == 422
        data = resp.json()
        err_msg = data.get("message") or data.get("detail", "")
        assert "Unsupported emotion" in err_msg


@pytest.mark.asyncio
async def test_api_parse_script_invalid_pause_greater_than_10s_422(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/campaigns/script/parse",
            headers=auth_headers,
            json={
                "script": '<break time="15s" />\nToo long pause.',
                "strict": True,
            },
        )
        assert resp.status_code == 422
        data = resp.json()
        err_msg = data.get("message") or data.get("detail", "")
        assert "Invalid pause duration" in err_msg


@pytest.mark.asyncio
async def test_api_speech_preview_general(auth_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/campaigns/speech-preview",
            headers=auth_headers,
            json={
                "script": '<emotion type="happy" />\nHello [Lead Name]!\n<break time="0.5s" />\nHow are you?',
                "lead_name": "Alice",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "audio_url" in data
        assert len(data["segments"]) == 3
        assert data["total_pause_duration"] == 0.5
