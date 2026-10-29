import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import Client

async def run():
    async with AsyncSessionLocal() as session:
        clients = await session.execute(select(Client))
        for c in clients.scalars().all():
            print(f"Client: {c.name} | {c.email} | {c.contact_number} | {c.original_filename}")

if __name__ == "__main__":
    asyncio.run(run())
