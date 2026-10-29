import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Campaign, CampaignLead, Expert, AIAgent, Call
from app.core.security import create_access_token, hash_password
from app.workers.tasks import _run_campaign_calls_async


@pytest_asyncio.fixture
async def campaign_test_env():
    async with AsyncSessionLocal() as db:
        biz = Business(name="Campaign Fetching Test Biz", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        # Create Voice Agent 1 (User + Expert)
        user_va1 = User(
            email=f"va1_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Voice Agent One",
            role="VOICE_AGENT",
            business_id=biz.id,
            status="AVAILABLE",
        )
        db.add(user_va1)
        await db.flush()

        expert_va1 = Expert(
            business_id=biz.id,
            user_id=user_va1.id,
            name="Voice Agent One",
            email=user_va1.email,
            status="AVAILABLE",
        )
        db.add(expert_va1)
        await db.flush()

        # Create Voice Agent 2 (User + Expert)
        user_va2 = User(
            email=f"va2_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Voice Agent Two",
            role="VOICE_AGENT",
            business_id=biz.id,
            status="AVAILABLE",
        )
        db.add(user_va2)
        await db.flush()

        expert_va2 = Expert(
            business_id=biz.id,
            user_id=user_va2.id,
            name="Voice Agent Two",
            email=user_va2.email,
            status="AVAILABLE",
        )
        db.add(expert_va2)
        await db.flush()

        # Create AI Agent 1
        ai_agent1 = AIAgent(
            business_id=biz.id,
            name="AI Sales Agent One",
            type="SALES",
            status="ACTIVE",
        )
        db.add(ai_agent1)
        await db.flush()

        await db.commit()

        token_va1 = create_access_token(user_va1.id, user_va1.role)
        token_va2 = create_access_token(user_va2.id, user_va2.role)

        yield {
            "biz_id": biz.id,
            "user_va1": user_va1,
            "expert_va1": expert_va1,
            "headers_va1": {"Authorization": f"Bearer {token_va1}"},
            "user_va2": user_va2,
            "expert_va2": expert_va2,
            "headers_va2": {"Authorization": f"Bearer {token_va2}"},
            "ai_agent1": ai_agent1,
        }


@pytest.mark.asyncio
async def test_voice_agent_campaign_lead_fetching(campaign_test_env):
    """Test 1: Voice Agent fetches A1 -> A2 -> A3 in order from assigned Campaign A."""
    biz_id = campaign_test_env["biz_id"]
    expert1 = campaign_test_env["expert_va1"]
    headers1 = campaign_test_env["headers_va1"]

    async with AsyncSessionLocal() as db:
        # Create Campaign A assigned to Voice Agent 1
        camp_a = Campaign(
            business_id=biz_id,
            name="Campaign A - Voice Agent 1",
            agent_type="HUMAN",
            voice_agent_id=expert1.id,
            schedule={"script": "Hello {{name}}, welcome to Campaign A!"},
            status="RUNNING",
        )
        db.add(camp_a)
        await db.flush()

        # Create leads A1, A2, A3
        leads_a = []
        for i in range(1, 4):
            l = Lead(
                business_id=biz_id,
                first_name=f"LeadA{i}",
                last_name="Test",
                phone=f"+1555010000{i}",
                email=f"leada{i}@example.com",
            )
            db.add(l)
            await db.flush()
            leads_a.append(l)

            cl = CampaignLead(
                campaign_id=camp_a.id,
                lead_id=l.id,
                assigned_expert_id=expert1.id,
                status="PENDING",
            )
            db.add(cl)

        # Create an unrelated Lead B in another campaign to test isolation
        camp_other = Campaign(business_id=biz_id, name="Other Campaign", status="RUNNING")
        db.add(camp_other)
        await db.flush()
        lead_b = Lead(business_id=biz_id, first_name="OtherLead", phone="+15559999999")
        db.add(lead_b)
        await db.flush()
        db.add(CampaignLead(campaign_id=camp_other.id, lead_id=lead_b.id, status="PENDING"))

        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Fetch 1: Should return Lead A1
        res1 = await client.get(f"/api/voice/leads/next?campaign_id={camp_a.id}", headers=headers1)
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["lead"]["id"] == leads_a[0].id
        assert "LeadA1" in data1["lead"]["first_name"]
        assert "Campaign A" in data1["campaign"]["name"]

        # Fetch 2: Should return Lead A2
        res2 = await client.get(f"/api/voice/leads/next?campaign_id={camp_a.id}", headers=headers1)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["lead"]["id"] == leads_a[1].id
        assert "LeadA2" in data2["lead"]["first_name"]

        # Fetch 3: Should return Lead A3
        res3 = await client.get(f"/api/voice/leads/next?campaign_id={camp_a.id}", headers=headers1)
        assert res3.status_code == 200
        data3 = res3.json()
        assert data3["lead"]["id"] == leads_a[2].id
        assert "LeadA3" in data3["lead"]["first_name"]

        # Fetch 4: Should return no more leads in Campaign A
        res4 = await client.get(f"/api/voice/leads/next?campaign_id={camp_a.id}", headers=headers1)
        assert res4.status_code == 200
        data4 = res4.json()
        assert data4["lead"] is None


@pytest.mark.asyncio
async def test_ai_agent_campaign_lead_fetching(campaign_test_env):
    """Test 2: AI Agent processes B1 -> B2 -> B3 in assigned Campaign B."""
    biz_id = campaign_test_env["biz_id"]
    ai_agent = campaign_test_env["ai_agent1"]

    async with AsyncSessionLocal() as db:
        camp_b = Campaign(
            business_id=biz_id,
            name="Campaign B - AI Agent 1",
            agent_type="AI_VOICE",
            agent_id=ai_agent.id,
            auto_dial_enabled=True,
            schedule={"calling_mode": "local"},
            status="RUNNING",
        )
        db.add(camp_b)
        await db.flush()

        leads_b = []
        for i in range(1, 4):
            l = Lead(
                business_id=biz_id,
                first_name=f"LeadB{i}",
                last_name="Test",
                phone=f"+1555020000{i}",
                email=f"leadb{i}@example.com",
            )
            db.add(l)
            await db.flush()
            leads_b.append(l)

            cl = CampaignLead(
                campaign_id=camp_b.id,
                lead_id=l.id,
                status="PENDING",
            )
            db.add(cl)

        await db.commit()
        camp_b_id = camp_b.id

    # Execute AI campaign runner task
    await _run_campaign_calls_async(camp_b_id)

    async with AsyncSessionLocal() as db:
        camp = await db.get(Campaign, camp_b_id)
        assert camp.status == "COMPLETED"

        cls = (await db.scalars(select(CampaignLead).where(CampaignLead.campaign_id == camp_b_id))).all()
        assert len(cls) == 3
        for cl in cls:
            assert cl.status in ("COMPLETED", "FAILED")

        for l_orig in leads_b:
            l = await db.get(Lead, l_orig.id)
            assert l.call_status in ("COMPLETED", "FAILED", "CONNECTED")


@pytest.mark.asyncio
async def test_multiple_campaigns_isolation(campaign_test_env):
    """Test 3: Voice Agent 1 receives Campaign A leads only; Voice Agent 2 receives Campaign B leads only."""
    biz_id = campaign_test_env["biz_id"]
    expert1 = campaign_test_env["expert_va1"]
    headers1 = campaign_test_env["headers_va1"]
    expert2 = campaign_test_env["expert_va2"]
    headers2 = campaign_test_env["headers_va2"]

    async with AsyncSessionLocal() as db:
        camp_a = Campaign(business_id=biz_id, name="Campaign A", voice_agent_id=expert1.id, status="RUNNING")
        camp_b = Campaign(business_id=biz_id, name="Campaign B", voice_agent_id=expert2.id, status="RUNNING")
        db.add_all([camp_a, camp_b])
        await db.flush()

        lead_a = Lead(business_id=biz_id, first_name="LeadA_Isolated", phone="+15551111111")
        lead_b = Lead(business_id=biz_id, first_name="LeadB_Isolated", phone="+15552222222")
        db.add_all([lead_a, lead_b])
        await db.flush()

        db.add(CampaignLead(campaign_id=camp_a.id, lead_id=lead_a.id, assigned_expert_id=expert1.id, status="PENDING"))
        db.add(CampaignLead(campaign_id=camp_b.id, lead_id=lead_b.id, assigned_expert_id=expert2.id, status="PENDING"))
        await db.commit()

        camp_a_id = camp_a.id
        camp_b_id = camp_b.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Agent 1 fetches from Campaign A
        res1 = await client.get(f"/api/voice/leads/next?campaign_id={camp_a_id}", headers=headers1)
        assert res1.status_code == 200
        assert res1.json()["lead"]["id"] == lead_a.id

        # Agent 2 fetches from Campaign B
        res2 = await client.get(f"/api/voice/leads/next?campaign_id={camp_b_id}", headers=headers2)
        assert res2.status_code == 200
        assert res2.json()["lead"]["id"] == lead_b.id


@pytest.mark.asyncio
async def test_call_completion_flow(campaign_test_env):
    """Test 5: Completing a call updates Call, Lead, and CampaignLead statuses, and completed leads are not re-fetched."""
    biz_id = campaign_test_env["biz_id"]
    expert1 = campaign_test_env["expert_va1"]
    headers1 = campaign_test_env["headers_va1"]

    async with AsyncSessionLocal() as db:
        camp = Campaign(business_id=biz_id, name="Completion Test Campaign", voice_agent_id=expert1.id, status="RUNNING")
        db.add(camp)
        await db.flush()

        l1 = Lead(business_id=biz_id, first_name="LeadFinish", phone="+15557778888")
        db.add(l1)
        await db.flush()

        cl1 = CampaignLead(campaign_id=camp.id, lead_id=l1.id, assigned_expert_id=expert1.id, status="PENDING")
        db.add(cl1)
        await db.commit()

        camp_id = camp.id
        l1_id = l1.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Fetch lead
        res_next = await client.get(f"/api/voice/leads/next?campaign_id={camp_id}", headers=headers1)
        assert res_next.status_code == 200
        assert res_next.json()["lead"]["id"] == l1_id

        # End call
        res_end = await client.post(
            f"/api/voice/calls/{l1_id}/end",
            json={"duration_seconds": 120, "outcome": "QUALIFIED"},
            headers=headers1,
        )
        assert res_end.status_code == 200

        # Verify DB updates
        async with AsyncSessionLocal() as db:
            lead = await db.get(Lead, l1_id)
            assert lead.status == "COMPLETED"
            assert lead.call_status == "COMPLETED"
            assert lead.call_result == "QUALIFIED"

            cl = (await db.scalars(select(CampaignLead).where(CampaignLead.lead_id == l1_id))).first()
            assert cl.status == "QUALIFIED"

        # Attempt to fetch next lead — should be empty
        res_empty = await client.get(f"/api/voice/leads/next?campaign_id={camp_id}", headers=headers1)
        assert res_empty.status_code == 200
        assert res_empty.json()["lead"] is None
