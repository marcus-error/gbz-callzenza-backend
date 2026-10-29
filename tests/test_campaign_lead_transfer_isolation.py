import asyncio
import uuid
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import Lead, Campaign, CampaignLead, Expert, User
from app.services.lead_assignment_service import LeadAssignmentService
from app.api.routes.leads import list_leads

async def test_lead_transfer_isolation():
    async with AsyncSessionLocal() as db:
        test_biz_id = "c3dce06c-8330-4724-ba33-b5282b3c3596"
        
        # 1. Get or create a voice agent user and expert
        va_user = await db.scalar(select(User).where(User.role == "VOICE_AGENT", User.business_id == test_biz_id))
        if not va_user:
            print("No voice agent user found!")
            return
        
        expert = await db.scalar(select(Expert).where(Expert.user_id == va_user.id))
        if not expert:
            expert = await db.scalar(select(Expert).where(Expert.email == va_user.email))
        if not expert:
            print("No expert matching voice agent found!")
            return
        
        # Set expert to AVAILABLE so immediate transfer succeeds
        expert.status = "AVAILABLE"
        db.add(expert)
        await db.commit()
        await db.refresh(expert)
        
        print(f"Using Voice Agent User: {va_user.email} (id: {va_user.id})")
        print(f"Using Expert: {expert.name} (id: {expert.id}, status: {expert.status})")

        # 2. Create 3 test leads for a campaign
        lead1 = Lead(
            id=str(uuid.uuid4()),
            business_id=test_biz_id,
            first_name="Customer1",
            last_name="SingleTransfer",
            phone="+1111111111",
            email="cust1@example.com",
            status="NEW"
        )
        lead2 = Lead(
            id=str(uuid.uuid4()),
            business_id=test_biz_id,
            first_name="Customer2",
            last_name="ShouldNotTransfer",
            phone="+2222222222",
            email="cust2@example.com",
            status="NEW"
        )
        lead3 = Lead(
            id=str(uuid.uuid4()),
            business_id=test_biz_id,
            first_name="Customer3",
            last_name="ShouldNotTransfer",
            phone="+3333333333",
            email="cust3@example.com",
            status="NEW"
        )
        db.add_all([lead1, lead2, lead3])
        await db.commit()

        # 3. Create an AI campaign with these 3 leads
        campaign_id = str(uuid.uuid4())
        campaign = Campaign(
            id=campaign_id,
            business_id=test_biz_id,
            name="AI Isolation Test Campaign",
            agent_type="AI",
            voice_agent_id=expert.id,
            status="RUNNING"
        )
        db.add(campaign)
        
        cl1 = CampaignLead(id=str(uuid.uuid4()), campaign_id=campaign_id, lead_id=lead1.id, status="PENDING")
        cl2 = CampaignLead(id=str(uuid.uuid4()), campaign_id=campaign_id, lead_id=lead2.id, status="PENDING")
        cl3 = CampaignLead(id=str(uuid.uuid4()), campaign_id=campaign_id, lead_id=lead3.id, status="PENDING")
        db.add_all([cl1, cl2, cl3])
        await db.commit()
        
        print("\nCreated AI Campaign with 3 customer leads.")
        
        # 4. Check voice agent leads BEFORE any transfer
        va_leads_before = await list_leads(user=va_user, db=db)
        va_lead_ids_before = [l["id"] for l in va_leads_before]
        
        assert lead1.id not in va_lead_ids_before, "Lead 1 should not be in VA leads yet!"
        assert lead2.id not in va_lead_ids_before, "Lead 2 should not be in VA leads!"
        assert lead3.id not in va_lead_ids_before, "Lead 3 should not be in VA leads!"
        print("PASS: Before transfer, voice agent sees NONE of the campaign leads.")

        # 5. Simulate AI Agent speaking to Customer 1 and transferring to Voice Agent
        print(f"\nSimulating call transfer for Customer 1 ({lead1.id}) to Voice Agent ({expert.name})...")
        res = await LeadAssignmentService.transfer_lead_to_agent(
            db=db,
            lead_id=lead1.id,
            business_id=test_biz_id,
            campaign_id=campaign_id,
            ai_agent_name="AI Agent Zara",
            qualification_stage="QUALIFIED",
            trigger_source="AI_VOICE_TRANSFER",
            notes="Customer requested to speak with human voice agent",
            requested_expert_name=expert.name
        )
        print(f"Transfer Result: success={res.get('success')}, status={res.get('status')}")

        # 6. Verify Lead 1 and CampaignLead 1 in DB
        await db.refresh(lead1)
        await db.refresh(lead2)
        await db.refresh(lead3)
        await db.refresh(cl1)
        await db.refresh(cl2)
        await db.refresh(cl3)

        assert lead1.assigned_expert_id == expert.id, f"Lead 1 must be assigned to {expert.id}"
        assert cl1.assigned_expert_id == expert.id, f"CampaignLead 1 must be assigned to {expert.id}"

        assert lead2.assigned_expert_id is None, "Lead 2 must NOT be assigned to expert!"
        assert cl2.assigned_expert_id is None, "CampaignLead 2 must NOT be assigned to expert!"

        assert lead3.assigned_expert_id is None, "Lead 3 must NOT be assigned to expert!"
        assert cl3.assigned_expert_id is None, "CampaignLead 3 must NOT be assigned to expert!"

        print("PASS: In database, ONLY Customer 1 has assigned_expert_id set to Voice Agent.")

        # 7. Check voice agent leads AFTER transfer
        va_leads_after = await list_leads(user=va_user, db=db)
        va_lead_ids_after = [l["id"] for l in va_leads_after]

        assert lead1.id in va_lead_ids_after, "Lead 1 MUST be in Voice Agent's leads after transfer!"
        assert lead2.id not in va_lead_ids_after, "Lead 2 MUST NOT be in Voice Agent's leads!"
        assert lead3.id not in va_lead_ids_after, "Lead 3 MUST NOT be in Voice Agent's leads!"

        print(f"PASS: Voice Agent's leads list contains ONLY Customer 1 ({lead1.first_name}) and NOT Customer 2 or 3.")

        # Cleanup
        await db.delete(cl1)
        await db.delete(cl2)
        await db.delete(cl3)
        await db.delete(campaign)
        await db.delete(lead1)
        await db.delete(lead2)
        await db.delete(lead3)
        await db.commit()
        print("\nAll assertions passed successfully! Cleanup complete.")

if __name__ == "__main__":
    asyncio.run(test_lead_transfer_isolation())
