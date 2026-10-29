import sys
import asyncio
import time
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Lead, Campaign, AIAgent, Call
from app.services.voice_service import initiate_outbound_call, ACTIVE_SIP_CALLS

async def trigger_physical_call():
    print("==================================================", flush=True)
    print("INITIATING REAL OUTBOUND SIP CALL TO PHYSICAL PHONE", flush=True)
    print("Target: MANONMANI Viswanathan (09585310712)", flush=True)
    print("==================================================", flush=True)

    async with AsyncSessionLocal() as db:
        # 1. Resolve or Create Lead
        lead_stmt = select(Lead).where(Lead.phone.in_(["09585310712", "9585310712", "+919585310712"]))
        lead = (await db.execute(lead_stmt)).scalars().first()
        if not lead:
            lead = Lead(
                business_id="default_biz",
                first_name="MANONMANI",
                last_name="Viswanathan",
                phone="09585310712"
            )
            db.add(lead)
            await db.commit()
            await db.refresh(lead)

        print(f"[1. LEAD RESOLVED] ID={lead.id} Name={lead.first_name} {lead.last_name} Phone={lead.phone}", flush=True)

        # 2. Resolve Agent & Campaign
        agent = await db.scalar(select(AIAgent).where(AIAgent.status == "ACTIVE"))
        if not agent:
            agent = await db.scalar(select(AIAgent))

        campaign = await db.scalar(select(Campaign).where(Campaign.sip_account_id.isnot(None)).order_by(Campaign.created_at.desc()))

        print(f"[2. AGENT & CAMPAIGN RESOLVED] Agent={getattr(agent, 'name', 'Alex')} | Campaign={getattr(campaign, 'name', 'Default')} (SIP Acc: {getattr(campaign, 'sip_account_id', None)})", flush=True)

        # 3. Place Call via SIPVoiceProvider
        print("[3. DISPATCHING SIP INVITE] Sending authenticated INVITE to Asterisk 49.249.142.147:5060...", flush=True)
        t_start = time.time()
        call_record = await initiate_outbound_call(db, lead, agent=agent, campaign=campaign)
        await db.commit()
        await db.refresh(call_record)

        call_id = call_record.id
        print(f"[4. CALL RECORD CREATED] DB call_id={call_id} SID={call_record.call_sid} status={call_record.status} caller_id={call_record.from_number}", flush=True)
        print("\n>>> PHONE IS RINGING NOW. PLEASE ANSWER THE PHYSICAL HANDSET. <<<\n", flush=True)

        # 4. Monitor Call State Machine & Telemetry for 60 seconds
        last_status = None
        for sec in range(60):
            async with AsyncSessionLocal() as check_db:
                c = await check_db.get(Call, call_id)
                cur_status = c.status if c else "UNKNOWN"
                sip_entry = ACTIVE_SIP_CALLS.get(call_id) or ACTIVE_SIP_CALLS.get(call_record.call_sid)
                answered = sip_entry.get("answered", False) if sip_entry else False
                if cur_status != last_status:
                    t_rel = round(time.time() - t_start, 2)
                    print(f"[{t_rel}s] [STATUS_CHANGE] call_id={call_id} status={cur_status} answered={answered}", flush=True)
                    last_status = cur_status
                if cur_status in ("ENDED", "FAILED", "COMPLETED"):
                    print(f"\nCall reached terminal state: {cur_status} after {round(time.time() - t_start, 2)}s\n")
                    break
            await asyncio.sleep(1.0)

if __name__ == "__main__":
    asyncio.run(trigger_physical_call())
