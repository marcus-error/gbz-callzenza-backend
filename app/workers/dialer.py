import asyncio
from typing import Any
from datetime import datetime
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.models import Campaign, CampaignLead, Lead, AIAgent, DialJob, DialerRun, Call, Expert
from app.services.voice_service import initiate_outbound_call


# Authoritative active statuses for capacity accounting
ACTIVE_DIALER_STATUSES = ["DIALING", "RINGING", "CALLING", "CONNECTED", "IN_CALL", "RESERVED"]
PENDING_STATUSES = ["PENDING", "RETRY_SCHEDULED", "NEW", "UNASSIGNED"]


_active_dialer_campaigns: set[str] = set()
_pending_refill_campaigns: set[str] = set()
_campaign_dispatch_locks: dict[str, asyncio.Lock] = {}


def get_campaign_dispatch_lock(campaign_id: str) -> asyncio.Lock:
    if campaign_id not in _campaign_dispatch_locks:
        _campaign_dispatch_locks[campaign_id] = asyncio.Lock()
    return _campaign_dispatch_locks[campaign_id]


async def count_campaign_active_calls(db: AsyncSession, campaign_id: str) -> tuple[int, dict[str, int]]:
    """
    Authoritative active-call reconciliation and concurrency accounting for a campaign.
    Reconciles stale DB records against live telephony registries, prevents duplicate
    counting across tables and aliases, and returns (authoritative_count, breakdown).
    """
    from app.services.voice_service import reconcile_unended_call, is_call_genuinely_active_sip

    now_utc = datetime.utcnow()
    changed = False

    # 1. Fetch candidate un-ended calls for this campaign and reconcile
    stmt_unended_calls = select(Call).where(
        Call.campaign_id == campaign_id,
        Call.ended_at.is_(None)
    )
    unended_calls = list((await db.scalars(stmt_unended_calls)).all())
    for call in unended_calls:
        if await reconcile_unended_call(db, call, now_utc):
            changed = True

    # 2. Fetch candidate active CampaignLead records and reconcile against calls/sessions
    stmt_active_cls = select(CampaignLead).where(
        CampaignLead.campaign_id == campaign_id,
        CampaignLead.status.in_(ACTIVE_DIALER_STATUSES)
    )
    active_cls = list((await db.scalars(stmt_active_cls)).all())
    for cl in active_cls:
        if cl.call_id:
            call = await db.get(Call, cl.call_id)
            if call:
                if await reconcile_unended_call(db, call, now_utc):
                    changed = True
            else:
                cl_age = (now_utc - (cl.last_attempt_at or cl.created_at)).total_seconds() if (cl.last_attempt_at or cl.created_at) else 9999.0
                if cl_age > 45.0:
                    prior_st = cl.status
                    cl.status = "FAILED"
                    db.add(cl)
                    changed = True
                    logger.info(
                        "[CALL_STATE_RECONCILIATION] campaign_id={cid} lead_id={lid} prior_state={prior} "
                        "reconciled_state=FAILED reason=missing_call_row age={age}s",
                        cid=campaign_id,
                        lid=cl.lead_id,
                        prior=prior_st,
                        age=int(cl_age),
                    )
        else:
            cl_age = (now_utc - (cl.last_attempt_at or cl.created_at)).total_seconds() if (cl.last_attempt_at or cl.created_at) else 9999.0
            if cl_age > 30.0:
                prior_st = cl.status
                cl.status = "PENDING"
                db.add(cl)
                changed = True
                logger.info(
                    "[CALL_STATE_RECONCILIATION] campaign_id={cid} lead_id={lid} prior_state={prior} "
                    "reconciled_state=PENDING reason=orphaned_reservation age={age}s",
                    cid=campaign_id,
                    lid=cl.lead_id,
                    prior=prior_st,
                    age=int(cl_age),
                )

    if changed:
        await db.commit()

    # 3. Compute deduplicated authoritative active calls and breakdown
    remaining_cls = list((await db.scalars(stmt_active_cls)).all())
    active_call_ids: set[str] = set()
    active_lead_ids: set[str] = set()
    breakdown: dict[str, int] = {}

    for cl in remaining_cls:
        if cl.lead_id and cl.lead_id in active_lead_ids:
            continue
        if cl.call_id and cl.call_id in active_call_ids:
            continue

        if cl.lead_id:
            active_lead_ids.add(cl.lead_id)
        if cl.call_id:
            active_call_ids.add(cl.call_id)

        cat = "CONNECTED" if cl.status in ("CONNECTED", "IN_PROGRESS", "IN_CALL", "AI_HANDLING", "TALKING") else (
            "RINGING" if cl.status in ("RINGING",) else (
                "DIALING" if cl.status in ("DIALING", "CALLING") else (
                    "RESERVED" if cl.status == "RESERVED" else cl.status
                )
            )
        )
        breakdown[cat] = breakdown.get(cat, 0) + 1

    # Also account for any unended Call record for this campaign not linked to CampaignLead
    stmt_remaining_calls = select(Call).where(
        Call.campaign_id == campaign_id,
        Call.ended_at.is_(None),
        Call.status.in_(["RINGING", "DIALING", "CALLING", "CONNECTED", "IN_PROGRESS", "IN_CALL", "AI_HANDLING", "TALKING"])
    )
    for call in (await db.scalars(stmt_remaining_calls)).all():
        if call.id in active_call_ids or (call.lead_id and call.lead_id in active_lead_ids):
            continue
        active_call_ids.add(call.id)
        if call.lead_id:
            active_lead_ids.add(call.lead_id)
        cat = "CONNECTED" if call.status in ("CONNECTED", "IN_PROGRESS", "IN_CALL", "AI_HANDLING", "TALKING") else "RINGING"
        breakdown[cat] = breakdown.get(cat, 0) + 1

    authoritative_count = max(len(active_call_ids), len(active_lead_ids), sum(breakdown.values()))
    logger.info(
        "[{ts}] [ACTIVE_CALL_COUNT] campaign_id={cid} authoritative_count={cnt} breakdown={bd}",
        ts=now_utc.isoformat(),
        cid=campaign_id,
        cnt=authoritative_count,
        bd=breakdown,
    )
    return authoritative_count, breakdown


def is_campaign_dialer_active(campaign_id: str) -> bool:
    return campaign_id in _active_dialer_campaigns


def mark_campaign_refill_pending(campaign_id: str) -> None:
    _pending_refill_campaigns.add(campaign_id)


def _parse_ratio(val: Any) -> int:
    if val is None:
        return 1
    if isinstance(val, str):
        s = val.strip()
        if ":" in s:
            parts = [p.strip() for p in s.split(":") if p.strip()]
            nums = [int(p) for p in parts if p.isdigit()]
            if nums:
                return max(1, max(nums))
            return 1
        try:
            return max(1, int(s))
        except Exception:
            return 1
    try:
        return max(1, int(val))
    except Exception:
        return 1


async def run_predictive_dialer_async(campaign_id: str) -> None:
    _active_dialer_campaigns.add(campaign_id)
    dispatch_lock = get_campaign_dispatch_lock(campaign_id)
    try:
        while True:
            leads_to_dial = []
            target_capacity = 1
            agent_id = None
            is_local = False
            is_human_camp = False
            dialer_run = None

            async with dispatch_lock:
                async with AsyncSessionLocal() as db:
                    campaign = await db.get(Campaign, campaign_id)
                    if not campaign or campaign.status != "RUNNING":
                        logger.debug("Dialer: Campaign {} not found or not RUNNING (status={}).", campaign_id, getattr(campaign, "status", None))
                        return

                    # Enforce isolation: Campaigns created for a Voice Agent must not be accessible to or dialed by the AI Agent
                    camp_agent_type = (campaign.agent_type or "AI_VOICE").upper()
                    is_human_camp = (
                        camp_agent_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT")
                        or "HUMAN" in camp_agent_type
                        or (campaign.voice_agent_id and not campaign.agent_id)
                    )
                    if is_human_camp:
                        logger.info(
                            "Dialer [{}]: Campaign is assigned to Voice Agent — skipping automated AI dialer. "
                            "Voice Agent campaigns must be dialed exclusively by the assigned Voice Agent.",
                            campaign_id
                        )
                        return

                    logger.info(
                        "[DIALER_DIAGNOSTIC] campaign_id={cid} actual_status={status} auto_dial_enabled={enabled}",
                        cid=campaign_id,
                        status=campaign.status,
                        enabled=campaign.auto_dial_enabled,
                    )

                    if not campaign.auto_dial_enabled:
                        logger.debug("Dialer: Campaign {} has auto_dial disabled.", campaign_id)
                        return

                    ratio = _parse_ratio(campaign.auto_dial_ratio)
                    target_capacity = max(1, ratio)

                    # Check currently active calls occupying dialing capacity with status breakdown
                    active_count, active_by_status = await count_campaign_active_calls(db, campaign_id)
                    available_slots = max(0, target_capacity - active_count)

                    trunk_cap_log = "NONE"
                    # Check trunk channel ceiling if configured on campaign (global across all campaigns sharing this trunk)
                    if getattr(campaign, "trunk_id", None):
                        try:
                            from app.db.models import SIPTrunk
                            trunk = await db.get(SIPTrunk, campaign.trunk_id)
                            if trunk and trunk.max_concurrency and trunk.max_concurrency > 0:
                                trunk_cap_log = str(trunk.max_concurrency)
                                target_capacity = min(target_capacity, trunk.max_concurrency)
                                trunk_active_calls = (await db.scalar(
                                    select(func.count(Call.id))
                                    .join(Campaign, Call.campaign_id == Campaign.id)
                                    .where(
                                        Campaign.trunk_id == campaign.trunk_id,
                                        Call.ended_at.is_(None),
                                        Call.status.in_(["RINGING", "DIALING", "CALLING", "CONNECTED", "IN_PROGRESS", "IN_CALL", "AI_HANDLING", "TALKING"])
                                    )
                                )) or 0
                                remaining_trunk_slots = max(0, trunk.max_concurrency - trunk_active_calls)
                                available_slots = min(available_slots, remaining_trunk_slots)
                        except Exception as trunk_err:
                            logger.warning(f"Failed to query trunk concurrency limit: {trunk_err}")

                    # Subquery to exclude any lead currently in an active unended call across any campaign
                    active_lead_ids_subquery = (
                        select(Call.lead_id)
                        .where(
                            Call.ended_at.is_(None),
                            Call.lead_id.is_not(None),
                            Call.status.in_(["DIALING", "RINGING", "CALLING", "CONNECTED", "IN_PROGRESS", "IN_CALL", "AI_HANDLING", "TALKING"])
                        )
                    )

                    # Query pending leads count (excluding leads assigned to a Voice Agent or currently in an active call)
                    stmt_pending = (
                        select(func.count(CampaignLead.id))
                        .join(Lead, CampaignLead.lead_id == Lead.id)
                        .where(
                            CampaignLead.campaign_id == campaign_id,
                            CampaignLead.status.in_(PENDING_STATUSES),
                            CampaignLead.assigned_expert_id.is_(None),
                            CampaignLead.lead_id.not_in(active_lead_ids_subquery),
                        )
                    )
                    pending_leads_count = (await db.scalar(stmt_pending)) or 0

                    logger.info(
                        "[{ts}] [DIALER_CAPACITY] campaign_id={cid}: actual_status={status}, configured_ratio=1:{ratio}, "
                        "effective_concurrency_limit={cap}, carrier_trunk_limit={t_limit}",
                        ts=datetime.utcnow().isoformat(),
                        cid=campaign_id,
                        status=campaign.status,
                        ratio=ratio,
                        cap=target_capacity,
                        t_limit=trunk_cap_log,
                    )
                    logger.info(
                        "[{ts}] [DIALER_METRICS] campaign_id={cid}: current_active_call_count={active} (breakdown={breakdown}), "
                        "eligible_pending_lead_count={pending}, available_slots={slots}",
                        ts=datetime.utcnow().isoformat(),
                        cid=campaign_id,
                        active=active_count,
                        breakdown=active_by_status,
                        pending=pending_leads_count,
                        slots=available_slots,
                    )

                    if available_slots <= 0:
                        logger.info(
                            "[{ts}] [DIALER_EARLY_RETURN] campaign_id={cid}: reason=FULL_CAPACITY, active={active}/{cap}, "
                            "preventing additional calls from starting until a slot completes",
                            ts=datetime.utcnow().isoformat(),
                            cid=campaign_id,
                            active=active_count,
                            cap=target_capacity,
                        )
                        if campaign_id in _pending_refill_campaigns:
                            _pending_refill_campaigns.discard(campaign_id)
                            logger.info("Dialer [{}]: Refill was requested while busy, re-evaluating capacity.", campaign_id)
                            await asyncio.sleep(0.01)
                            continue
                        logger.info("Dialer [{}]: At full capacity ({}/{}). Waiting for calls to complete.", campaign_id, active_count, target_capacity)
                        return

                    # Select pending candidates in ascending order (excluding leads assigned to a Voice Agent, DND, or currently in an active call)
                    stmt_candidate_ids = (
                        select(CampaignLead.id)
                        .join(Lead, CampaignLead.lead_id == Lead.id)
                        .where(
                            CampaignLead.campaign_id == campaign_id,
                            CampaignLead.status.in_(PENDING_STATUSES),
                            Lead.status.notin_(["DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED"]),
                            CampaignLead.assigned_expert_id.is_(None),
                            CampaignLead.lead_id.not_in(active_lead_ids_subquery),
                        )
                        .order_by(CampaignLead.created_at.asc(), CampaignLead.id.asc())
                        .limit(available_slots)
                    )
                    candidate_ids = list((await db.scalars(stmt_candidate_ids)).all())

                    if not candidate_ids:
                        # Verify if there are really 0 pending leads remaining
                        stmt_any_pending = (
                            select(func.count(CampaignLead.id))
                            .join(Lead, CampaignLead.lead_id == Lead.id)
                            .where(
                                CampaignLead.campaign_id == campaign_id,
                                CampaignLead.status.in_(PENDING_STATUSES),
                                CampaignLead.assigned_expert_id.is_(None),
                            )
                        )
                        pending_remaining = (await db.scalar(stmt_any_pending)) or 0
                        if pending_remaining == 0 and active_count == 0:
                            logger.info("Dialer [{}]: No pending leads and no active calls. Marking campaign as COMPLETED.", campaign_id)
                            campaign.status = "COMPLETED"
                            campaign.completed_at = datetime.utcnow()
                            await db.commit()
                        return

                    # Atomic conditional reservation across workers
                    from sqlalchemy import update
                    stmt_atomic_reserve = (
                        update(CampaignLead)
                        .where(
                            CampaignLead.id.in_(candidate_ids),
                            CampaignLead.status.in_(PENDING_STATUSES)
                        )
                        .values(status="RESERVED")
                        .returning(CampaignLead.id)
                    )
                    reserved_ids = list((await db.scalars(stmt_atomic_reserve)).all())
                    await db.flush()

                    # Fetch only the leads that this worker successfully claimed as RESERVED
                    if not reserved_ids:
                        leads_to_dial = []
                    else:
                        stmt_claimed = (
                            select(CampaignLead)
                            .where(
                                CampaignLead.id.in_(reserved_ids),
                                CampaignLead.status == "RESERVED"
                            )
                            .order_by(CampaignLead.created_at.asc(), CampaignLead.id.asc())
                        )
                        leads_to_dial = list((await db.scalars(stmt_claimed)).all())

                    # Re-verify total active/reserved count within transaction to guarantee capacity limit under multi-worker race
                    total_active_now, _ = await count_campaign_active_calls(db, campaign_id)
                    if total_active_now > target_capacity:
                        excess = total_active_now - target_capacity
                        # Roll back excess reserved leads to PENDING
                        leads_to_revert = leads_to_dial[-excess:]
                        leads_to_dial = leads_to_dial[:-excess]
                        for rev_lead in leads_to_revert:
                            rev_lead.status = "PENDING"
                        await db.flush()

                    await db.commit()

                    if not leads_to_dial:
                        logger.info("Dialer [{}]: No leads reserved after concurrency check (active={}/{}).", campaign_id, total_active_now, target_capacity)
                        return

                    logger.info(
                        "[{ts}] [DIALER_LEADS_RESERVED] campaign_id={cid}: count={count}, "
                        "distinct_lead_ids={lead_ids}, campaign_lead_ids={cl_ids}",
                        ts=datetime.utcnow().isoformat(),
                        cid=campaign_id,
                        count=len(leads_to_dial),
                        lead_ids=[l.lead_id for l in leads_to_dial],
                        cl_ids=[l.id for l in leads_to_dial],
                    )
                    for slot_idx, r_lead in enumerate(leads_to_dial):
                        logger.info(
                            "[{ts}] [DIALER_SLOT_RESERVED] campaign_id={cid}: lead_id={lid}, "
                            "campaign_lead_id={clid}, slot_index={s_idx}/{cap}",
                            ts=datetime.utcnow().isoformat(),
                            cid=campaign_id,
                            lid=r_lead.lead_id,
                            clid=r_lead.id,
                            s_idx=slot_idx + 1,
                            cap=target_capacity,
                        )

                    # Find active dialer run or create one
                    stmt_run = select(DialerRun).where(DialerRun.campaign_id == campaign_id, DialerRun.status == "RUNNING")
                    dialer_run = (await db.scalars(stmt_run)).first()
                    if not dialer_run:
                        dialer_run = DialerRun(campaign_id=campaign_id, status="RUNNING")
                        db.add(dialer_run)
                        await db.commit()
                        await db.refresh(dialer_run)

                    dialer_run_id = dialer_run.id
                    agent_id = campaign.agent_id
                    is_local = bool(campaign.schedule and isinstance(campaign.schedule, dict) and campaign.schedule.get("calling_mode") == "local")
                    lead_ids_to_dial = [c.id for c in leads_to_dial]

            if not lead_ids_to_dial:
                return

            # Outbound calls are dispatched concurrently outside the campaign dispatch lock
            # so dispatch decisions do not block other workers or status checks.
            async def _dial_one(c_lead_id: str):
                try:
                    async with AsyncSessionLocal() as local_db:
                        c_lead = await local_db.get(CampaignLead, c_lead_id)
                        if not c_lead:
                            return
                        lead = await local_db.get(Lead, c_lead.lead_id)
                        if not lead or not lead.phone:
                            c_lead.status = "FAILED"
                            await local_db.commit()
                            return

                        from app.services.dnd_service import is_number_suppressed
                        if await is_number_suppressed(local_db, lead.phone, lead.business_id):
                            logger.info(f"[DIALER_DND_BLOCKED] Lead {lead.id} phone {lead.phone} is on DND registry. Skipping dial.")
                            c_lead.status = "DNC"
                            lead.status = "DNC"
                            lead.call_status = "DNC"
                            lead.call_result = "Do Not Call (DNC)"
                            local_db.add(c_lead)
                            local_db.add(lead)
                            await local_db.commit()
                            return
                            
                        c_lead.status = "DIALING"
                        c_lead.attempt_count += 1
                        c_lead.last_attempt_at = datetime.utcnow()
                        await local_db.commit()

                        dispatch_start_ts = datetime.utcnow().isoformat()
                        logger.info(
                            "[{ts}] [DIALER_OUTBOUND_DISPATCH_START] campaign_id={cid}: lead_id={lid}, "
                            "campaign_lead_id={clid}, attempt={attempt}, dispatch_start_time={dts}",
                            ts=dispatch_start_ts,
                            cid=campaign_id,
                            lid=lead.id,
                            clid=c_lead_id,
                            attempt=c_lead.attempt_count,
                            dts=dispatch_start_ts,
                        )

                        try:
                            # Re-fetch agent to attach to this session if needed
                            local_agent = await local_db.get(AIAgent, agent_id) if agent_id else None
                            if not local_agent and not is_human_camp:
                                local_agent = (await local_db.execute(select(AIAgent).where(AIAgent.status == "ACTIVE"))).scalars().first()
                                if not local_agent:
                                    local_agent = (await local_db.execute(select(AIAgent).order_by(AIAgent.created_at.asc()))).scalars().first()
                            
                            local_camp = await local_db.get(Campaign, campaign_id)
                            call = await initiate_outbound_call(local_db, lead, local_agent, campaign=local_camp, campaign_id=campaign_id, force_mock=is_local)
                            call.campaign_id = campaign_id
                            local_db.add(call)
                            
                            c_lead.call_id = call.id
                            if call.status in ("FAILED", "COMPLETED"):
                                c_lead.status = call.status
                                if call.status == "FAILED" and call.notes:
                                    logger.warning(f"[DIALER_CALL_BLOCKED] campaign_id={campaign_id} lead_id={lead.id}: {call.notes}")
                            else:
                                c_lead.status = "RINGING"
                            
                            dial_job = DialJob(
                                dialer_run_id=dialer_run_id,
                                campaign_id=campaign_id,
                                lead_id=lead.id,
                                call_id=call.id,
                                attempt_number=c_lead.attempt_count,
                                status="RINGING",
                                started_at=datetime.utcnow()
                            )
                            local_db.add(dial_job)
                            await local_db.commit()

                            dispatch_end_ts = datetime.utcnow().isoformat()
                            logger.info(
                                "[{ts}] [DIALER_OUTBOUND_DISPATCH_RESULT] campaign_id={cid}: lead_id={lid}, "
                                "call_id={call_id}, call_sid={call_sid}, dispatch_start_time={start_ts}, "
                                "dispatch_end_time={end_ts}, sip_result={sip_res}, status={status}",
                                ts=dispatch_end_ts,
                                cid=campaign_id,
                                lid=lead.id,
                                call_id=call.id,
                                call_sid=getattr(call, "call_sid", None),
                                start_ts=dispatch_start_ts,
                                end_ts=dispatch_end_ts,
                                sip_res=call.status,
                                status=call.status,
                            )
                            
                        except Exception as e:
                            logger.error(
                                "[DIALER_EXCEPTION] campaign_id={} lead_id={} campaign_lead_id={} error={}",
                                campaign_id, lead.id, c_lead_id, e
                            )
                            c_lead.status = "FAILED"
                            await local_db.commit()
                            from app.workers.tasks import trigger_campaign_refill
                            trigger_campaign_refill(campaign_id)
                except Exception as ex:
                    logger.error("[DIALER_EXCEPTION] campaign_id={} campaign_lead_id={} error={}", campaign_id, c_lead_id, ex)

            tasks = [_dial_one(c_id) for c_id in lead_ids_to_dial]
            await asyncio.gather(*tasks)

            # Safety net: release any un-dispatched RESERVED leads back to PENDING
            async with AsyncSessionLocal() as db:
                stmt_cleanup_res = (
                    update(CampaignLead)
                    .where(
                        CampaignLead.id.in_(lead_ids_to_dial),
                        CampaignLead.status == "RESERVED"
                    )
                    .values(status="PENDING")
                )
                await db.execute(stmt_cleanup_res)
                await db.commit()

                # Check if all leads in campaign are processed
                stmt_remaining_pending = select(func.count(CampaignLead.id)).where(
                    CampaignLead.campaign_id == campaign_id,
                    CampaignLead.status.in_(PENDING_STATUSES)
                )
                rem_pending = (await db.scalar(stmt_remaining_pending)) or 0

                stmt_remaining_active = select(func.count(CampaignLead.id)).where(
                    CampaignLead.campaign_id == campaign_id,
                    CampaignLead.status.in_(ACTIVE_DIALER_STATUSES)
                )
                rem_active = (await db.scalar(stmt_remaining_active)) or 0

                if rem_pending == 0 and rem_active == 0:
                    logger.info("Dialer [{}]: All leads processed and all active calls completed. Marking campaign as COMPLETED.", campaign_id)
                    campaign = await db.get(Campaign, campaign_id)
                    if campaign:
                        campaign.status = "COMPLETED"
                        campaign.completed_at = datetime.utcnow()
                        await db.commit()
                elif rem_pending == 0:
                    logger.info("Dialer [{}]: No more pending leads. Waiting for {} active calls.", campaign_id, rem_active)
                    return

                await asyncio.sleep(0.5)
    except Exception as err:
        logger.error("Dialer [{}]: Fatal dialer error: {}", campaign_id, err)
    finally:
        _active_dialer_campaigns.discard(campaign_id)
        _pending_refill_campaigns.discard(campaign_id)
