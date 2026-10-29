import asyncio
from app.db.database import AsyncSessionLocal
from app.services.lead_recycle_service import get_hopper_kpis, get_recycle_kpis, get_hopper_leads, get_recycle_lists

async def test():
    async with AsyncSessionLocal() as session:
        print("Hopper KPIs:")
        try:
            print(await get_hopper_kpis(session))
        except Exception as e:
            print("Error Hopper KPIs:", e)
            import traceback
            traceback.print_exc()

        print("\nRecycle KPIs:")
        try:
            print(await get_recycle_kpis(session))
        except Exception as e:
            print("Error Recycle KPIs:", e)
            import traceback
            traceback.print_exc()
            
        print("\nHopper Leads:")
        try:
            print(await get_hopper_leads(session))
        except Exception as e:
            print("Error Hopper Leads:", e)
            import traceback
            traceback.print_exc()

        print("\nRecycle Lists:")
        try:
            print(await get_recycle_lists(session))
        except Exception as e:
            print("Error Recycle Lists:", e)
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test())
