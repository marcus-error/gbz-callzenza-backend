import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import Lead, LeadGroup

async def run():
    async with AsyncSessionLocal() as session:
        print("--- LEAD GROUPS ---")
        groups = await session.execute(select(LeadGroup))
        for group in groups.scalars().all():
            print(f"Group ID: {group.id}, Filename: {group.filename}, Lead Count: {group.lead_count}")

        print("\n--- LEADS ---")
        leads = await session.execute(select(Lead).order_by(Lead.created_at.desc()).limit(20))
        for lead in leads.scalars().all():
            print(f"Lead ID: {lead.id}, Name: {lead.first_name} {lead.last_name}, Group: {lead.group_id}")

if __name__ == "__main__":
    asyncio.run(run())
