import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import LeadGroup

async def run():
    async with AsyncSessionLocal() as session:
        groups = await session.execute(select(LeadGroup).order_by(LeadGroup.created_at.desc()).limit(10))
        for g in groups.scalars().all():
            print(f"Group ID: {g.id}, Filename: {g.filename}, Leads: {g.lead_count}")

if __name__ == "__main__":
    asyncio.run(run())
