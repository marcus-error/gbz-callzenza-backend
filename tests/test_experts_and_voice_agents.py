import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_experts_me_unauthorized():
    # Calling /api/experts/me without auth token should return 401 Unauthorized
    resp = client.get("/api/experts/me")
    assert resp.status_code == 401

def test_voice_agents_unauthorized():
    # Calling /api/voice-agents without auth token should return 401 Unauthorized
    resp = client.get("/api/voice-agents")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_experts_auto_sync_voice_agents():
    import uuid
    from app.db.database import AsyncSessionLocal
    from app.db.models import Business, User, Expert
    from app.api.routes.experts import list_experts

    async with AsyncSessionLocal() as db:
        biz = Business(name=f"Sync Test Biz {uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        user = User(
            email=f"jon_{uuid.uuid4().hex[:6]}@gmail.com",
            hashed_password="hash",
            full_name="Ageon Targareyoun",
            role="VOICE_AGENT",
            business_id=biz.id
        )
        db.add(user)
        await db.commit()

        # Call list_experts for this business
        experts = await list_experts(user=user, db=db)

        # Verify Ageon Targareyoun is returned as an available Expert
        expert_emails = [e.email for e in experts]
        assert user.email in expert_emails
        target_exp = next(e for e in experts if e.email == user.email)
        assert target_exp.name == "Ageon Targareyoun"

