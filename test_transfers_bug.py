import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import CallTransfer, Lead, Call, User, Campaign, Client
from sqlalchemy import select, or_

async def main():
    async with AsyncSessionLocal() as db:
        # Let's get the first user to simulate
        user_res = await db.scalars(select(User).limit(1))
        user = user_res.first()
        
        stmt = (
            select(CallTransfer)
            .where(
                or_(
                    CallTransfer.to_agent_id == user.id,
                    CallTransfer.from_agent_id == user.id,
                )
            )
            .order_by(CallTransfer.created_at.desc())
        )
        transfers = (await db.execute(stmt)).scalars().all()
        print("Transfers found:", len(transfers))
        for t in transfers:
            lead = await db.get(Lead, t.lead_id) if t.lead_id else None
            call = await db.get(Call, t.call_id) if t.call_id else None
            from_user = await db.get(User, t.from_agent_id) if t.from_agent_id else None

            campaign_id = t.campaign_id or (call.campaign_id if call else None)
            campaign = await db.get(Campaign, campaign_id) if campaign_id else None

            client_id = t.client_id or (campaign.client_id if campaign else (call.client_id if call else None))
            client = await db.get(Client, client_id) if client_id else None
            
            customer_name = (
                f"{lead.first_name} {lead.last_name}".strip()
                if lead and (lead.first_name or lead.last_name)
                else (call.client_name if call else "Customer")
            )
            print("OK")

if __name__ == "__main__":
    asyncio.run(main())
