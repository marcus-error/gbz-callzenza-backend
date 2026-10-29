import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import Campaign, User
from app.api.routes.campaigns import dialer_status
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as db:
        camp_id = "3950e31a-9e70-4dd6-b3e1-68cf7fa6103f"
        user = (await db.execute(select(User))).scalars().first()
        res = await dialer_status(id=camp_id, user=user, db=db)
        print("DIALER STATUS RESULT:")
        print("status:", res.get("status"))
        print("active_calls:", res.get("active_calls"))
        print("ratio:", res.get("ratio"))
        print("target_capacity:", res.get("target_capacity"))
        print("available_slots:", res.get("available_slots"))
        print("pending_leads:", res.get("pending_leads"))
        print("ringing_calls:", res.get("ringing_calls"))
        print("breakdown:", res.get("active_by_status"))

asyncio.run(main())
