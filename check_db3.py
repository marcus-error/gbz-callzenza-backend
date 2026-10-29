import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import Lead

async def run():
    async with AsyncSessionLocal() as session:
        leads = await session.execute(select(Lead).order_by(Lead.created_at.desc()).limit(10))
        for lead in leads.scalars().all():
            print(f"Lead ID: {lead.id}, Name: {lead.first_name} {lead.last_name}, Group: {lead.group_id}, Source: {lead.source}, Created At: {lead.created_at}")

if __name__ == "__main__":
    asyncio.run(run())
