"""
Regression Test Suite:
1. Complete Assigned Leads Persistence & AI Agent API Retrieval:
   - Campaign assigned 10 leads returns all 10 in All Leads list with accurate counts.
   - Campaign assigned 100 leads paginates correctly with exact totals and zero dropped leads.
   - Concurrency 1/1 does NOT restrict the All Leads list to 1 lead.
   - Leads with prior CRM assigned_expert_id are NOT excluded from AI Agent campaigns.
   - Status filtering (PENDING, ACTIVE, COMPLETED, FAILED, DND) preserves aggregate counts.
   - Strict tenant and campaign isolation: zero leak across campaigns or businesses.
   - Empty campaign returns clean empty state.
   - Auto-Dial status progression preserves all assigned leads without dropping entries.
"""

import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    Business,
    Campaign,
    CampaignLead,
    Lead,
    User,
    Call,
    AIAgent,
)
from app.api.routes.campaign_ai_agent import get_campaign_ai_agent_leads


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def setup_test_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory


@pytest.mark.asyncio
async def test_campaign_assigned_10_leads_displays_all_and_accurate_counts():
    """
    Verifies:
    - A campaign assigned 10 leads returns all 10 in the All Leads list.
    - Counts accurately reflect: pending, active, completed, failed, and dnd.
    - Concurrency 1/1 does NOT restrict the All Leads list to 1 lead.
    """
    engine, session_factory = await setup_test_db()

    async with session_factory() as db:
        biz = Business(id="biz_1", name="Alpha Corp", owner_user_id="user_admin")
        db.add(biz)

        user = User(
            id="user_admin",
            business_id="biz_1",
            email="admin@alpha.com",
            role="ADMIN",
            hashed_password="hashed_pw",
        )
        db.add(user)

        ai_agent = AIAgent(
            id="agent_1",
            business_id="biz_1",
            name="Alpha AI",
            status="ACTIVE",
        )
        db.add(ai_agent)

        camp = Campaign(
            id="camp_10",
            business_id="biz_1",
            name="10 Leads Campaign",
            status="RUNNING",
            agent_id="agent_1",
            agent_type="AI_VOICE",
            auto_dial_enabled=True,
            auto_dial_ratio=1,  # Concurrency 1/1
        )
        db.add(camp)

        # Create 10 leads with diverse statuses
        leads_data = [
            ("l_1", "Lead One", "PENDING", "PENDING"),
            ("l_2", "Lead Two", "PENDING", "PENDING"),
            ("l_3", "Lead Three", "PENDING", "PENDING"),
            ("l_4", "Lead Four", "PENDING", "PENDING"),
            ("l_5", "Lead Five", "ACTIVE", "RINGING"),     # 1 Active call
            ("l_6", "Lead Six", "ACTIVE", "CONNECTED"),    # 1 Active call
            ("l_7", "Lead Seven", "COMPLETED", "COMPLETED"),
            ("l_8", "Lead Eight", "COMPLETED", "CALLED"),
            ("l_9", "Lead Nine", "FAILED", "NO_ANSWER"),
            ("l_10", "Lead Ten", "DNC", "DNC"),            # 1 DND
        ]

        for lid, name, l_status, cl_status in leads_data:
            ld = Lead(
                id=lid,
                business_id="biz_1",
                first_name=name,
                phone=f"+1555000{lid}",
                status=l_status,
                call_status=l_status,
            )
            cl = CampaignLead(
                id=f"cl_{lid}",
                campaign_id="camp_10",
                lead_id=lid,
                status=cl_status,
            )
            db.add_all([ld, cl])

        # Add 1 active call for lead 5 (proving concurrency 1 does not restrict roster)
        call_5 = Call(
            id="call_active_5",
            business_id="biz_1",
            campaign_id="camp_10",
            lead_id="l_5",
            status="RINGING",
            from_number="+15551110000",
            to_number="+1555000l_5",
        )
        db.add(call_5)
        await db.commit()

        # Invoke API endpoint
        res = await get_campaign_ai_agent_leads(
            campaign_id="camp_10",
            user=user,
            db=db,
            status_filter=None,
            limit=100,
            offset=0,
        )

        assert res["campaign_id"] == "camp_10"
        assert res["total_assigned"] == 10, f"Expected 10 total assigned leads, got {res['total_assigned']}"
        assert res["total_returned"] == 10, f"Expected 10 returned leads, got {res['total_returned']}"
        assert len(res["leads"]) == 10

        counts = res["counts"]
        assert counts["total"] == 10
        assert counts["pending"] == 4, f"Expected 4 pending, got {counts['pending']}"
        assert counts["active"] == 2, f"Expected 2 active, got {counts['active']}"
        assert counts["completed"] == 2, f"Expected 2 completed, got {counts['completed']}"
        assert counts["failed"] == 1, f"Expected 1 failed, got {counts['failed']}"
        assert counts["dnd"] == 1, f"Expected 1 dnd, got {counts['dnd']}"

        # Ensure all 10 lead IDs are present
        returned_ids = {l["id"] for l in res["leads"]}
        expected_ids = {f"l_{i}" for i in range(1, 11)}
        assert returned_ids == expected_ids


@pytest.mark.asyncio
async def test_campaign_assigned_100_leads_pagination():
    """
    Verifies:
    - A campaign assigned 100 leads paginates correctly.
    - Page 1 (limit=50, offset=0) returns 50 leads with has_more=True.
    - Page 2 (limit=50, offset=50) returns 50 leads with has_more=False.
    - No duplicate leads across pages, exact total is preserved.
    """
    engine, session_factory = await setup_test_db()

    async with session_factory() as db:
        biz = Business(id="biz_100", name="Century Corp", owner_user_id="user_century")
        db.add(biz)

        user = User(id="user_century", business_id="biz_100", email="century@corp.com", role="ADMIN", hashed_password="hashed_pw")
        db.add(user)

        ai_agent = AIAgent(id="agent_century", business_id="biz_100", name="Century AI", status="ACTIVE")
        db.add(ai_agent)

        camp = Campaign(
            id="camp_100",
            business_id="biz_100",
            name="100 Leads Campaign",
            status="RUNNING",
            agent_id="agent_century",
            agent_type="AI_VOICE",
        )
        db.add(camp)

        # Insert 100 leads
        for i in range(1, 101):
            lid = f"lead_{i:03d}"
            ld = Lead(
                id=lid,
                business_id="biz_100",
                first_name=f"Customer {i}",
                phone=f"+1555100{i:03d}",
                status="PENDING",
            )
            cl = CampaignLead(
                id=f"cl_{i:03d}",
                campaign_id="camp_100",
                lead_id=lid,
                status="PENDING",
            )
            db.add_all([ld, cl])
        await db.commit()

        # Page 1: limit 50, offset 0
        page1 = await get_campaign_ai_agent_leads(
            campaign_id="camp_100",
            user=user,
            db=db,
            limit=50,
            offset=0,
        )
        assert page1["total_assigned"] == 100
        assert page1["total_returned"] == 50
        assert len(page1["leads"]) == 50
        assert page1["pagination"]["total"] == 100
        assert page1["pagination"]["has_more"] is True
        assert page1["counts"]["total"] == 100

        # Page 2: limit 50, offset 50
        page2 = await get_campaign_ai_agent_leads(
            campaign_id="camp_100",
            user=user,
            db=db,
            limit=50,
            offset=50,
        )
        assert page2["total_assigned"] == 100
        assert page2["total_returned"] == 50
        assert len(page2["leads"]) == 50
        assert page2["pagination"]["total"] == 100
        assert page2["pagination"]["has_more"] is False

        # Verify no overlap between Page 1 and Page 2
        p1_ids = {l["id"] for l in page1["leads"]}
        p2_ids = {l["id"] for l in page2["leads"]}
        assert len(p1_ids.intersection(p2_ids)) == 0
        assert len(p1_ids.union(p2_ids)) == 100


@pytest.mark.asyncio
async def test_leads_with_crm_assigned_expert_not_excluded():
    """
    Verifies:
    - Leads with prior CRM assigned_expert_id on the Lead table are NOT dropped.
    - Both leads with assigned_expert_id and leads without appear in the AI campaign leads list.
    """
    engine, session_factory = await setup_test_db()

    async with session_factory() as db:
        biz = Business(id="biz_crm", name="CRM Corp", owner_user_id="user_crm")
        db.add(biz)

        user = User(id="user_crm", business_id="biz_crm", email="crm@corp.com", role="ADMIN", hashed_password="hashed_pw")
        db.add(user)

        ai_agent = AIAgent(id="agent_crm", business_id="biz_crm", name="CRM AI", status="ACTIVE")
        db.add(ai_agent)

        camp = Campaign(
            id="camp_crm",
            business_id="biz_crm",
            name="CRM Leads Campaign",
            status="RUNNING",
            agent_id="agent_crm",
            agent_type="AI_VOICE",
        )
        db.add(camp)

        # Lead A: Has prior CRM expert assignment
        lead_a = Lead(
            id="lead_crm_assigned",
            business_id="biz_crm",
            first_name="Assigned Expert Lead",
            phone="+15559990001",
            status="PENDING",
            assigned_expert_id="prior_expert_123",  # Existed previously on CRM
        )
        cl_a = CampaignLead(
            id="cl_crm_a",
            campaign_id="camp_crm",
            lead_id="lead_crm_assigned",
            status="PENDING",
        )

        # Lead B: Has no expert assignment
        lead_b = Lead(
            id="lead_crm_clean",
            business_id="biz_crm",
            first_name="Unassigned Lead",
            phone="+15559990002",
            status="PENDING",
            assigned_expert_id=None,
        )
        cl_b = CampaignLead(
            id="cl_crm_b",
            campaign_id="camp_crm",
            lead_id="lead_crm_clean",
            status="PENDING",
        )
        db.add_all([lead_a, cl_a, lead_b, cl_b])
        await db.commit()

        res = await get_campaign_ai_agent_leads(
            campaign_id="camp_crm",
            user=user,
            db=db,
            limit=100,
            offset=0,
        )

        assert res["total_assigned"] == 2
        assert len(res["leads"]) == 2
        lead_ids = [l["id"] for l in res["leads"]]
        assert "lead_crm_assigned" in lead_ids
        assert "lead_crm_clean" in lead_ids


@pytest.mark.asyncio
async def test_status_filter_preserves_total_counts():
    """
    Verifies:
    - Filtering by status_filter='COMPLETED' returns only completed leads.
    - Total assigned and aggregate counts remain global to the campaign.
    - Pagination total matches the filtered count.
    """
    engine, session_factory = await setup_test_db()

    async with session_factory() as db:
        biz = Business(id="biz_filt", name="Filt Corp", owner_user_id="user_filt")
        db.add(biz)

        user = User(id="user_filt", business_id="biz_filt", email="filt@corp.com", role="ADMIN", hashed_password="hashed_pw")
        db.add(user)

        ai_agent = AIAgent(id="agent_filt", business_id="biz_filt", name="Filt AI", status="ACTIVE")
        db.add(ai_agent)

        camp = Campaign(
            id="camp_filt",
            business_id="biz_filt",
            name="Filter Campaign",
            status="RUNNING",
            agent_id="agent_filt",
            agent_type="AI_VOICE",
        )
        db.add(camp)

        # Add 3 PENDING, 2 COMPLETED
        for i in range(1, 4):
            ld = Lead(id=f"p_{i}", business_id="biz_filt", first_name=f"Pending {i}", phone=f"+1555222{i}")
            cl = CampaignLead(id=f"cl_p_{i}", campaign_id="camp_filt", lead_id=f"p_{i}", status="PENDING")
            db.add_all([ld, cl])

        for i in range(1, 3):
            ld = Lead(id=f"c_{i}", business_id="biz_filt", first_name=f"Completed {i}", phone=f"+1555333{i}")
            cl = CampaignLead(id=f"cl_c_{i}", campaign_id="camp_filt", lead_id=f"c_{i}", status="COMPLETED")
            db.add_all([ld, cl])

        await db.commit()

        # Query filtered by COMPLETED
        res = await get_campaign_ai_agent_leads(
            campaign_id="camp_filt",
            user=user,
            db=db,
            status_filter="COMPLETED",
            limit=100,
            offset=0,
        )

        assert res["total_assigned"] == 5  # Global total preserved
        assert res["counts"]["total"] == 5
        assert res["counts"]["pending"] == 3
        assert res["counts"]["completed"] == 2
        assert res["pagination"]["total"] == 2  # Filtered matching total
        assert res["total_returned"] == 2
        for l in res["leads"]:
            assert l["call_status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_strict_campaign_and_tenant_isolation():
    """
    Verifies:
    - Campaign A in Business 1 never returns leads from Campaign B in Business 1.
    - Business 1 never returns leads from Business 2.
    - Empty campaign returns empty leads list and zero counts.
    """
    engine, session_factory = await setup_test_db()

    async with session_factory() as db:
        biz1 = Business(id="biz_iso1", name="Iso Corp 1", owner_user_id="user_iso1")
        biz2 = Business(id="biz_iso2", name="Iso Corp 2", owner_user_id="user_iso2")
        db.add_all([biz1, biz2])

        user1 = User(id="user_iso1", business_id="biz_iso1", email="u1@iso.com", role="ADMIN", hashed_password="hashed_pw")
        db.add(user1)

        ai_agent = AIAgent(id="agent_iso", business_id="biz_iso1", name="Iso AI", status="ACTIVE")
        db.add(ai_agent)

        camp_a = Campaign(id="camp_a", business_id="biz_iso1", name="Camp A", status="RUNNING", agent_id="agent_iso")
        camp_b = Campaign(id="camp_b", business_id="biz_iso1", name="Camp B", status="RUNNING", agent_id="agent_iso")
        camp_empty = Campaign(id="camp_empty", business_id="biz_iso1", name="Camp Empty", status="RUNNING", agent_id="agent_iso")
        db.add_all([camp_a, camp_b, camp_empty])

        # Lead A in Biz 1 for Camp A
        lead_a = Lead(id="lead_a", business_id="biz_iso1", first_name="Lead A", phone="+15551110000")
        cl_a = CampaignLead(id="cl_a", campaign_id="camp_a", lead_id="lead_a", status="PENDING")

        # Lead B in Biz 1 for Camp B
        lead_b = Lead(id="lead_b", business_id="biz_iso1", first_name="Lead B", phone="+15552220000")
        cl_b = CampaignLead(id="cl_b", campaign_id="camp_b", lead_id="lead_b", status="PENDING")

        # Lead X in Biz 2
        lead_x = Lead(id="lead_x", business_id="biz_iso2", first_name="Lead X", phone="+15553330000")
        db.add_all([lead_a, cl_a, lead_b, cl_b, lead_x])
        await db.commit()

        # Query Camp A
        res_a = await get_campaign_ai_agent_leads(
            campaign_id="camp_a",
            user=user1,
            db=db,
        )
        assert res_a["total_assigned"] == 1
        assert len(res_a["leads"]) == 1
        assert res_a["leads"][0]["id"] == "lead_a"

        # Query Empty Campaign
        res_empty = await get_campaign_ai_agent_leads(
            campaign_id="camp_empty",
            user=user1,
            db=db,
        )
        assert res_empty["total_assigned"] == 0
        assert len(res_empty["leads"]) == 0
        assert res_empty["counts"]["total"] == 0
        assert res_empty["pagination"]["total"] == 0


@pytest.mark.asyncio
async def test_lead_status_progression_preserves_entire_lead_list():
    """
    Verifies:
    - When Auto-Dial advances Lead 1 from PENDING -> RINGING -> CONNECTED -> COMPLETED:
    - Other assigned leads (Lead 2, Lead 3) remain intact in the campaign lead list.
    - Counts transition cleanly without losing any lead records.
    """
    engine, session_factory = await setup_test_db()

    async with session_factory() as db:
        biz = Business(id="biz_prog", name="Prog Corp", owner_user_id="user_prog")
        db.add(biz)

        user = User(id="user_prog", business_id="biz_prog", email="prog@corp.com", role="ADMIN", hashed_password="hashed_pw")
        db.add(user)

        ai_agent = AIAgent(id="agent_prog", business_id="biz_prog", name="Prog AI", status="ACTIVE")
        db.add(ai_agent)

        camp = Campaign(id="camp_prog", business_id="biz_prog", name="Progression Campaign", status="RUNNING", agent_id="agent_prog")
        db.add(camp)

        # 3 Leads
        for i in range(1, 4):
            ld = Lead(id=f"prog_l{i}", business_id="biz_prog", first_name=f"Prog Lead {i}", phone=f"+1555444{i}")
            cl = CampaignLead(id=f"prog_cl{i}", campaign_id="camp_prog", lead_id=f"prog_l{i}", status="PENDING")
            db.add_all([ld, cl])
        await db.commit()

        # Initial state: 3 PENDING
        r1 = await get_campaign_ai_agent_leads(campaign_id="camp_prog", user=user, db=db)
        assert r1["counts"]["total"] == 3
        assert r1["counts"]["pending"] == 3
        assert r1["counts"]["active"] == 0

        # Step 1: Lead 1 is dialed (RINGING)
        cl1 = await db.get(CampaignLead, "prog_cl1")
        cl1.status = "RINGING"
        db.add(cl1)
        await db.commit()

        r2 = await get_campaign_ai_agent_leads(campaign_id="camp_prog", user=user, db=db)
        assert r2["counts"]["total"] == 3
        assert r2["counts"]["pending"] == 2
        assert r2["counts"]["active"] == 1
        assert len(r2["leads"]) == 3

        # Step 2: Lead 1 completes
        cl1.status = "COMPLETED"
        db.add(cl1)
        await db.commit()

        r3 = await get_campaign_ai_agent_leads(campaign_id="camp_prog", user=user, db=db)
        assert r3["counts"]["total"] == 3
        assert r3["counts"]["pending"] == 2
        assert r3["counts"]["active"] == 0
        assert r3["counts"]["completed"] == 1
        assert len(r3["leads"]) == 3

        # Ensure all 3 leads are still returned
        lead_ids = {l["id"] for l in r3["leads"]}
        assert lead_ids == {"prog_l1", "prog_l2", "prog_l3"}
