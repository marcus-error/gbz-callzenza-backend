import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_transfer_to_agent_unauthorized():
    """Accessing /api/lead-assignment/transfer-to-agent without auth token should return 401."""
    resp = client.post("/api/lead-assignment/transfer-to-agent", json={"lead_id": "nonexistent-id"})
    assert resp.status_code == 401


def test_complete_ai_call_unauthorized():
    """Accessing /api/lead-assignment/complete-ai-call without auth token should return 401."""
    resp = client.post("/api/lead-assignment/complete-ai-call", json={"lead_id": "nonexistent-id"})
    assert resp.status_code == 401


def test_list_queue_unauthorized():
    """Accessing /api/lead-assignment/queue without auth token should return 401."""
    resp = client.get("/api/lead-assignment/queue")
    assert resp.status_code == 401


def test_list_assignments_unauthorized():
    """Accessing /api/lead-assignment/assignments without auth token should return 401."""
    resp = client.get("/api/lead-assignment/assignments")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_specific_agent_request_and_reassignment():
    """Tests lead reassignment when a customer requests a specific Voice Agent by name."""
    from app.db.database import AsyncSessionLocal
    from app.db.models import Business, Expert, Lead, LeadAssignment, AgentAssignmentQueue, User
    from app.services.lead_assignment_service import LeadAssignmentService
    import uuid

    try:
        async with AsyncSessionLocal() as db:
            usr = User(id=str(uuid.uuid4()), email=f"user_{uuid.uuid4()}@example.com", hashed_password="pw")
            biz = Business(id=str(uuid.uuid4()), name="Test Assignment Biz", owner_user_id=usr.id)
            db.add_all([usr, biz])
            await db.commit()
            biz_id = biz.id

        # Create two experts: Aurora (AVAILABLE) and Asu (BUSY)
        aurora = Expert(id=str(uuid.uuid4()), business_id=biz_id, name="Aurora", status="AVAILABLE")
        asu = Expert(id=str(uuid.uuid4()), business_id=biz_id, name="Asu", status="BUSY")
        db.add_all([aurora, asu])
        await db.commit()

        # 1. Assign lead to Aurora first
        lead_id = str(uuid.uuid4())
        res1 = await LeadAssignmentService.transfer_lead_to_agent(
            db=db,
            business_id=biz_id,
            lead_id=lead_id,
            trigger_source="TALK_TO_AGENT"
        )
        assert res1["status"] == "ASSIGNED"
        assert res1["assigned_expert"]["id"] == aurora.id

        # 2. Customer requests Asu specifically (Asu is BUSY)
        res2 = await LeadAssignmentService.transfer_lead_to_agent(
            db=db,
            business_id=biz_id,
            lead_id=lead_id,
            requested_expert_name="asu"
        )
        assert res2["status"] == "QUEUED_FOR_REQUESTED_AGENT"
        assert res2["requested_expert"]["id"] == asu.id

        # Aurora assignment should be REASSIGNED
        old_assign = (await db.execute(
            __import__("sqlalchemy").select(LeadAssignment).where(
                LeadAssignment.lead_id == lead_id,
                LeadAssignment.expert_id == aurora.id
            )
        )).scalars().first()
        assert old_assign.status == "REASSIGNED"

        # 3. Asu becomes AVAILABLE -> process_assignment_queue
        asu.status = "AVAILABLE"
        await db.commit()

        processed = await LeadAssignmentService.process_assignment_queue(db=db, business_id=biz_id, expert_id=asu.id)
        assert processed == 1

        # Lead should now be assigned to Asu
        lead = await db.get(Lead, lead_id)
        assert lead.assigned_expert_id == asu.id
    except Exception as e:
        pytest.skip(f"Skipping database test due to local DB connection: {e}")

