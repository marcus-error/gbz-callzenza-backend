import asyncio
import sys
sys.path.append('.')
from app.db.database import AsyncSessionLocal
from sqlalchemy import text

async def main():
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text('ALTER TABLE calls ALTER COLUMN lead_id DROP NOT NULL'))
            await db.commit()
            print('Successfully made lead_id nullable in calls table')
        except Exception as e:
            print(f"Error: {e}")

asyncio.run(main())
