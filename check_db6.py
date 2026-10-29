import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import Lead

async def run():
    async with AsyncSessionLocal() as session:
        leads = await session.execute(select(Lead))
        for l in leads.scalars().all():
            print(f"Lead: {l.first_name} {l.last_name} | {l.phone} | {l.email}")

if __name__ == "__main__":
    asyncio.run(run())
