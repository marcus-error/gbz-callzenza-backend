import asyncio
from app.db.database import SessionLocal
from app.db.models import User
from sqlalchemy import select
from app.api.routes.agent_dashboard import get_daily_report

async def run():
    async with SessionLocal() as db:
        user = (await db.scalars(select(User).limit(1))).first()
        print("Testing with user:", user.email)
        try:
            res = await get_daily_report(date="2026-09-03", user=user, db=db)
            print("Success:", res)
        except Exception as e:
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run())

