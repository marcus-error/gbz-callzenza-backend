import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import User, Business, Expert, LeadGroup, Lead, Campaign, CampaignLead, Client, Call
from app.core.security import hash_password

@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture
async def setup_data_chain_fixture():
    async with AsyncSessionLocal() as session:
        import uuid
        uid = uuid.uuid4().hex[:6]
        admin_email = f"clientadmin_{uid}@callzenza.com"
        agent_email = f"chainagent_{uid}@gmail.com"

        # Create Business
        biz = Business(name=f"Client Chain Test Biz {uid}", owner_user_id="owner-1")
        session.add(biz)
        await session.flush()

        # Create Admin
        admin = User(
            email=admin_email,
            hashed_password=hash_password("admin123"),
            full_name="Admin User",
            role="ADMIN",
            business_id=biz.id
        )
        session.add(admin)

        # Create Voice Agent User
        agent_user = User(
            email=agent_email,
            hashed_password=hash_password("agent123"),
            full_name="Chain Agent",
            role="VOICE_AGENT",
            business_id=biz.id
        )
        session.add(agent_user)

        # Create Client
        client = Client(
            business_id=biz.id,
            name="Acme Corporation",
            company_name="Acme Corp Global",
            contact_number="+15559876543",
            email="contact@acme.com"
        )
        session.add(client)
        await session.flush()

        # Create Expert (Voice Agent)
        expert = Expert(
            business_id=biz.id,
            user_id=agent_user.id,
            name="Chain Agent Expert",
            email=agent_user.email
        )
        session.add(expert)
        await session.flush()

        # Create Lead Group & Lead
        group = LeadGroup(business_id=biz.id, filename="AcmeLeads.xlsx", lead_count=1)
        session.add(group)
        await session.flush()

        lead = Lead(
            business_id=biz.id,
            group_id=group.id,
            first_name="Arun",
            last_name="Kumar",
            phone="+919876543210",
            email="arun@gmail.com",
            company="Arun Enterprises",
            status="NEW"
        )
        session.add(lead)
        await session.flush()

        # Create Campaign linked to Client, LeadGroup, Expert
        campaign = Campaign(
            business_id=biz.id,
            name="Acme Q3 Sales Campaign",
            status="RUNNING",
            agent_type="HUMAN",
            voice_agent_id=expert.id,
            lead_group_id=group.id,
            client_id=client.id,
            client_name=client.name,
            client_phone=client.contact_number
        )
        session.add(campaign)
        await session.flush()

        c_lead = CampaignLead(
            campaign_id=campaign.id,
            lead_id=lead.id,
            assigned_expert_id=expert.id,
            status="PENDING"
        )
        session.add(c_lead)
        await session.commit()

        yield {
            "biz_id": biz.id,
            "admin_email": admin.email,
            "agent_email": agent_user.email,
            "client_id": client.id,
            "client_name": client.name,
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "lead_id": lead.id,
            "lead_name": "Arun Kumar",
            "expert_id": expert.id
        }

@pytest.mark.asyncio
async def test_client_campaign_data_chain(async_client, setup_data_chain_fixture):
    data = setup_data_chain_fixture

    # 1. Login as Admin
    login_res = await async_client.post("/api/auth/login", json={
        "email": data["admin_email"],
        "password": "admin123"
    })
    assert login_res.status_code == 200
    admin_token = login_res.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Verify Campaign returns exact client_id and client_name
    camp_res = await async_client.get(f"/api/campaigns/{data['campaign_id']}", headers=admin_headers)
    assert camp_res.status_code == 200
    camp_json = camp_res.json()
    assert camp_json["client_id"] == data["client_id"]
    assert camp_json["client_name"] == data["client_name"]

    # 2. Login as Voice Agent
    agent_login = await async_client.post("/api/auth/login", json={
        "email": data["agent_email"],
        "password": "agent123"
    })
    assert agent_login.status_code == 200
    agent_token = agent_login.json()["access_token"]
    agent_headers = {"Authorization": f"Bearer {agent_token}"}

    # 3. Call Dial Next endpoint /api/voice/leads/next
    next_res = await async_client.get("/api/voice/leads/next", headers=agent_headers)
    assert next_res.status_code == 200
    next_json = next_res.json()

    assert "lead" in next_json
    assert next_json["lead"]["id"] == data["lead_id"]
    assert next_json["lead"]["lead_name"] == data["lead_name"]

    assert "campaign" in next_json
    assert next_json["campaign"]["id"] == data["campaign_id"]
    assert next_json["campaign"]["client_id"] == data["client_id"]
    assert next_json["campaign"]["client_name"] == data["client_name"]
