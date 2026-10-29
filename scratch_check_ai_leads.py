import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import Campaign, User
from app.api.routes.campaign_ai_agent import get_campaign_ai_agent_leads
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as db:
        camp_id = "3950e31a-9e70-4dd6-b3e1-68cf7fa6103f"
        user = (await db.execute(select(User))).scalars().first()
        res = await get_campaign_ai_agent_leads(campaign_id=camp_id, user=user, db=db)
        print("TOTAL RETURNED LEADS:", res.get("total_returned"))
        active = []
        for l in res.get("leads", []):
            st = l.get("call_status")
            if st and st.upper() not in ("COMPLETED", "FAILED", "DISCONNECTED", "ENDED", "CANCELLED", "PENDING", "NEW"):
                active.append((l.get("id"), l.get("name"), l.get("phone"), st))
        print("ACTIVE LEADS COUNT IN AI AGENT LEADS:", len(active))
        for a in active:
            print("  ", a)

asyncio.run(main())
