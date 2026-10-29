import uuid
import pytest
import io
import pandas as pd

from app.services.import_service import parse_leads_file
from app.db.database import AsyncSessionLocal
from app.db.models import Lead, LeadGroup, Campaign, CampaignLead, CallTransfer, User, Expert, Business
from app.workers.tasks import _run_campaign_calls_async
from app.core.security import hash_password
from sqlalchemy import select, func


@pytest.mark.asyncio
async def test_excel_import_statistics():
    """Test importing an Excel file with 20 rows, duplicate handling, and statistics calculation."""
    async with AsyncSessionLocal() as db_session:
        biz = Business(name=f"Import Biz {uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db_session.add(biz)
        await db_session.flush()

        # Create 20 sample rows: 18 unique, 1 duplicate, 1 empty/invalid
        data = []
        for i in range(1, 19):
            data.append({
                "Name": f"Customer {i}",
                "Email": f"customer{i}@example.com",
                "Mobile": f"+155500000{i:02d}",
                "City": f"City {i}"
            })
        # Add 1 duplicate row
        data.append({
            "Name": "Customer 1 Duplicate",
            "Email": "customer1@example.com",
            "Mobile": "+15550000001",
            "City": "City 1"
        })
        # Add 1 invalid row (no name, email, phone)
        data.append({
            "Name": "",
            "Email": "",
            "Mobile": "",
            "City": "Empty City"
        })

        buffer = io.BytesIO()
        df = pd.DataFrame(data)
        df.to_excel(buffer, index=False)
        buffer.seek(0)

        rows = parse_leads_file(buffer.getvalue(), "test_leads.xlsx")
        assert len(rows) == 20

        # Save to lead group
        group = LeadGroup(
            business_id=biz.id,
            filename="test_leads.xlsx",
            lead_count=0,
            status="IDLE"
        )
        db_session.add(group)
        await db_session.flush()

        seen_contacts = set()
        created_leads = []
        duplicate_rows = 0
        invalid_rows = 0

        for i, r in enumerate(rows):
            name = r.get("first_name", "").strip()
            email = r.get("email", "").strip().lower()
            phone = r.get("phone", "").strip()
            city = r.get("location", "").strip()

            if not (name or email or phone):
                invalid_rows += 1
                continue

            key = (phone, email) if (phone or email) else (name,)
            if key in seen_contacts:
                duplicate_rows += 1
                continue
            seen_contacts.add(key)

            lead = Lead(
                business_id=biz.id,
                group_id=group.id,
                source="EXCEL_IMPORT",
                first_name=name,
                last_name="",
                email=email,
                phone=phone,
                location=city,
                status="Ready",
                call_status="NEW"
            )
            db_session.add(lead)
            created_leads.append(lead)

        await db_session.commit()

        assert len(created_leads) == 18
        assert duplicate_rows == 1
        assert invalid_rows == 1


@pytest.mark.asyncio
async def test_campaign_voice_agent_isolation():
    """Test that Voice Agent campaigns are not called by AI background worker."""
    async with AsyncSessionLocal() as db_session:
        biz = Business(name=f"Isolation Biz {uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        user = User(
            email=f"va_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass"),
            full_name="Jon Voice Agent",
            role="VOICE_AGENT",
            business_id=biz.id
        )
        db_session.add_all([biz, user])
        await db_session.flush()

        expert = Expert(
            user_id=user.id,
            business_id=biz.id,
            name="Jon Voice Agent",
            email=user.email,
            status="AVAILABLE"
        )
        db_session.add(expert)
        await db_session.flush()

        campaign = Campaign(
            business_id=biz.id,
            name="Voice Campaign",
            agent_type="HUMAN",
            voice_agent_id=expert.id,
            status="RUNNING"
        )
        db_session.add(campaign)
        await db_session.flush()

        for i in range(5):
            lead = Lead(
                business_id=biz.id,
                first_name=f"Lead {i}",
                phone=f"+1555111000{i}",
                email=f"lead{i}@test.com",
                status="Ready",
                call_status="NEW"
            )
            db_session.add(lead)
            await db_session.flush()

            clead = CampaignLead(
                campaign_id=campaign.id,
                lead_id=lead.id,
                assigned_expert_id=expert.id,
                status="PENDING"
            )
            db_session.add(clead)

        await db_session.commit()

        await _run_campaign_calls_async(campaign.id)

        stmt = select(CampaignLead).where(CampaignLead.campaign_id == campaign.id)
        cleads = (await db_session.scalars(stmt)).all()
        for cl in cleads:
            assert cl.status == "PENDING"


@pytest.mark.asyncio
async def test_call_transfer_isolation():
    """Test CallTransfer record creation and target agent isolation."""
    async with AsyncSessionLocal() as db_session:
        biz = Business(name=f"Transfer Biz {uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        u1 = User(email=f"u1_{uuid.uuid4().hex[:6]}@test.com", hashed_password="h", business_id=biz.id, role="AGENT", full_name="User 1")
        u2 = User(email=f"u2_{uuid.uuid4().hex[:6]}@test.com", hashed_password="h", business_id=biz.id, role="AGENT", full_name="User 2")
        db_session.add_all([biz, u1, u2])
        await db_session.flush()

        t1 = CallTransfer(
            business_id=biz.id,
            call_id="call-123",
            from_agent_id="ai-agent-id",
            to_agent_id=u1.id,
            transfer_type="Customer Requested Human",
            status="PENDING"
        )
        t2 = CallTransfer(
            business_id=biz.id,
            call_id="call-456",
            from_agent_id="ai-agent-id",
            to_agent_id=u2.id,
            transfer_type="Customer Requested Human",
            status="PENDING"
        )
        db_session.add_all([t1, t2])
        await db_session.commit()

        stmt = select(CallTransfer).where(CallTransfer.to_agent_id == u1.id)
        u1_transfers = (await db_session.scalars(stmt)).all()

        assert len(u1_transfers) == 1
        assert u1_transfers[0].id == t1.id


@pytest.mark.asyncio
async def test_csv_lead_flexible_parsing_and_import():
    """Test parsing CSV with single names (Subham, neeraj), missing locations, and alias columns."""
    async with AsyncSessionLocal() as db_session:
        biz = Business(name=f"Flex Biz {uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db_session.add(biz)
        await db_session.flush()

        csv_content = (
            "Name,Mobile,Email\n"
            "Subham,,skulshrestha52@gmail.com\n"
            "DILLIP KUMAR MENDULI,+919876543210,dillipmenduli_75@yahoo.com\n"
            "neeraj,,neeraj123agrawal@yahoo.com\n"
            "shreeram,+919876543211,shreeram.puneeta@gmail.com\n"
            "AMIT KUMAR SINGH,+919876543212,amitsinghdscl@gmail.com\n"
        ).encode("utf-8")

        rows = parse_leads_file(csv_content, "customer_leads.csv")
        assert len(rows) == 5

        # Check single name parsing
        assert rows[0]["first_name"] == "Subham"
        assert rows[0]["email"] == "skulshrestha52@gmail.com"

        # Check full name splitting
        assert rows[1]["first_name"] == "DILLIP"
        assert rows[1]["last_name"] == "KUMAR MENDULI"

        group = LeadGroup(business_id=biz.id, filename="customer_leads.csv", lead_count=len(rows), status="COMPLETED")
        db_session.add(group)
        await db_session.flush()

        for r in rows:
            lead = Lead(
                business_id=biz.id,
                group_id=group.id,
                source="EXCEL_IMPORT",
                first_name=r.get("first_name", ""),
                last_name=r.get("last_name", ""),
                email=r.get("email", ""),
                phone=r.get("phone", ""),
                location=r.get("location", "")
            )
            db_session.add(lead)

        await db_session.commit()

        stmt = select(func.count(Lead.id)).where(Lead.group_id == group.id)
        count = (await db_session.execute(stmt)).scalar()
        assert count == 5

