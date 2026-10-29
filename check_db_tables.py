import asyncio
from app.db.database import AsyncSessionLocal
from sqlalchemy import text

async def check():
    async with AsyncSessionLocal() as session:
        res = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        tables = [r[0] for r in res.fetchall()]
        print("Tables in DB:", len(tables))
        dnd_tables = [t for t in tables if 'dnd' in t.lower() or 'dnc' in t.lower() or 'suppress' in t.lower() or 'black' in t.lower()]
        print("DND-related tables:", dnd_tables)

if __name__ == '__main__':
    asyncio.run(check())
