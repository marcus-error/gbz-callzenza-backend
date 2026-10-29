import asyncio
import sys
sys.path.append('.')
from app.db.database import AsyncSessionLocal
from sqlalchemy import text

async def main():
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text('ALTER TABLE campaigns ADD COLUMN started_at TIMESTAMP'))
            print("Added started_at")
        except Exception as e:
            print(f"Error adding started_at: {e}")
            
        try:
            await db.execute(text('ALTER TABLE campaigns ADD COLUMN completed_at TIMESTAMP'))
            print("Added completed_at")
        except Exception as e:
            print(f"Error adding completed_at: {e}")
            
        await db.commit()
        print('Done')

asyncio.run(main())
