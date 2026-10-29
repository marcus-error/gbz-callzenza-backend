import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import Lead, LeadGroup

async def run():
    async with AsyncSessionLocal() as session:
        # Get the latest lead
        result = await session.execute(select(Lead).order_by(Lead.created_at.desc()).limit(1))
        lead = result.scalar_one_or_none()
        if lead:
            print("LATEST LEAD:")
            print(f"ID: {lead.id}")
            print(f"First Name: {lead.first_name}")
            print(f"Last Name: {lead.last_name}")
            print(f"Phone: {lead.phone}")
            print(f"Email: {lead.email}")
            print(f"Group ID: {lead.group_id}")
            
            group_res = await session.execute(select(LeadGroup).where(LeadGroup.id == lead.group_id))
            group = group_res.scalar_one_or_none()
            if group:
                print(f"\nBELONGS TO GROUP:")
                print(f"Group ID: {group.id}")
                print(f"Filename: {group.filename}")
                
        else:
            print("No leads found.")

if __name__ == "__main__":
    asyncio.run(run())
