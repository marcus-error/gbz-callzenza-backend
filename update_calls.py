import asyncio
import sys
sys.path.append('.')
from app.db.database import AsyncSessionLocal
from app.db.models import Call
from sqlalchemy import update

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(update(Call).where(Call.duration_seconds == 70).values(duration_seconds=5))
        await db.commit()
        print('Done')

asyncio.run(main())
