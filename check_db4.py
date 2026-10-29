import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import Campaign, Client

async def run():
    async with AsyncSessionLocal() as session:
        campaigns = await session.execute(select(Campaign).order_by(Campaign.created_at.desc()).limit(5))
        for c in campaigns.scalars().all():
            print(f"Campaign ID: {c.id}, Name: {c.name}, Client: {c.client_name}, Lead Group: {c.lead_group_id}")

if __name__ == "__main__":
    asyncio.run(run())
