import io
import uuid
import pytest
import pandas as pd
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import User, Business, Lead, LeadGroup, Campaign, CampaignLead, Expert
from app.core.security import create_access_token
from app.services.import_service import parse_leads_file


@pytest.fixture
async def excel_test_env():
    async with AsyncSessionLocal() as db:
        unique_id = uuid.uuid4().hex[:6]
        user = User(
            email=f"exceladmin_{unique_id}@example.com",
            hashed_password="hash",
            role="ADMIN",
            status="ACTIVE",
            full_name="Excel Admin",
        )
        db.add(user)
        await db.flush()

        biz = Business(name="Excel Test Biz", owner_user_id=user.id)
        db.add(biz)
        await db.flush()

        user.business_id = biz.id
        db.add(user)

        expert = Expert(
            business_id=biz.id,
            user_id=user.id,
            name="Excel Expert",
            email="exceladmin@example.com",
        )
        db.add(expert)
        await db.commit()

        token = create_access_token(user.id, user.role)
        headers = {"Authorization": f"Bearer {token}"}

        return {
            "biz_id": biz.id,
            "user": user,
            "expert": expert,
            "headers": headers,
        }


@pytest.mark.asyncio
async def test_parse_leads_file_preserves_phone_numbers():
    """Verify parse_leads_file parses various phone column headers and full names."""
    df = pd.DataFrame([
        {"Full Name": "Alice Smith", "Mobile": "555-0101", "City": "New York"},
        {"Name": "Bob Jones", "Contact Number": "+1 555-0102", "Email": "bob@example.com"},
        {"First Name": "Charlie", "Last Name": "Brown", "Phone": "5550103", "Address": "Austin"},
        {"Customer Name": "Diana Prince", "Cell": "555-0104", "Notes": "High priority"},
    ])
    buffer = io.BytesIO()
    df.to_excel(buffer, index=False)
    content = buffer.getvalue()

    rows = parse_leads_file(content, "test.xlsx")

    assert len(rows) == 4
    assert rows[0]["first_name"] == "Alice"
    assert rows[0]["last_name"] == "Smith"
    assert rows[0]["phone"] == "5550101"

    assert rows[1]["first_name"] == "Bob"
    assert rows[1]["last_name"] == "Jones"
    assert rows[1]["phone"] == "+15550102"

    assert rows[2]["first_name"] == "Charlie"
    assert rows[2]["last_name"] == "Brown"
    assert rows[2]["phone"] == "5550103"

    assert rows[3]["first_name"] == "Diana"
    assert rows[3]["last_name"] == "Prince"
    assert rows[3]["phone"] == "5550104"


@pytest.mark.asyncio
async def test_excel_import_endpoint_and_dial_next_flow(excel_test_env):
    """Test uploading an Excel file with 10 leads, assigning to campaign, and fetching all 10 via Dial Next."""
    headers = excel_test_env["headers"]
    biz_id = excel_test_env["biz_id"]
    expert = excel_test_env["expert"]

    # Create an Excel file in-memory with 10 leads
    records = []
    for i in range(1, 11):
        records.append({
            "Full Name": f"Customer{i} Test",
            "Phone Number": f"+155590000{i:02d}",
            "Email": f"customer{i}@example.com",
            "Company": f"Company {i}",
            "City": "San Francisco",
        })

    df = pd.DataFrame(records)
    buffer = io.BytesIO()
    df.to_excel(buffer, index=False)
    content = buffer.getvalue()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Import file via POST /api/leads/import
        files = {"file": ("callzenzatest.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        res_import = await client.post("/api/leads/import", files=files, data={"auto_call": "false"}, headers=headers)
        assert res_import.status_code == 201
        data_import = res_import.json()
        assert data_import["total_rows"] == 10
        assert data_import["leads_created"] == 10

        # Verify leads in database
        async with AsyncSessionLocal() as db:
            leads = (await db.scalars(select(Lead).where(Lead.business_id == biz_id).order_by(Lead.created_at.asc()))).all()
            assert len(leads) == 10

            # Create a Campaign with all 10 leads assigned to expert
            camp = Campaign(
                business_id=biz_id,
                name="CallZenza Test Campaign",
                agent_type="HUMAN",
                voice_agent_id=expert.id,
                status="RUNNING",
            )
            db.add(camp)
            await db.flush()

            for l in leads:
                cl = CampaignLead(
                    campaign_id=camp.id,
                    lead_id=l.id,
                    assigned_expert_id=expert.id,
                    status="PENDING",
                )
                db.add(cl)

            await db.commit()
            camp_id = camp.id

        # Dial Next 10 times and verify each lead is fetched sequentially
        fetched_lead_ids = []
        for step in range(1, 11):
            res_next = await client.get(f"/api/voice/leads/next?campaign_id={camp_id}", headers=headers)
            assert res_next.status_code == 200
            data_next = res_next.json()
            assert data_next["lead"] is not None
            lead_id = data_next["lead"]["id"]
            assert lead_id not in fetched_lead_ids
            fetched_lead_ids.append(lead_id)

            # Simulate call end to mark lead completed
            res_end = await client.post(
                f"/api/voice/calls/{lead_id}/end",
                json={"duration_seconds": 60, "outcome": "COMPLETED"},
                headers=headers,
            )
            assert res_end.status_code == 200

        assert len(fetched_lead_ids) == 10

        # 11th fetch should return no more leads
        res_empty = await client.get(f"/api/voice/leads/next?campaign_id={camp_id}", headers=headers)
        assert res_empty.status_code == 200
        assert res_empty.json()["lead"] is None
