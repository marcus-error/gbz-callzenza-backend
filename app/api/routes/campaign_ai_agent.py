from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, func, or_, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
import json
from loguru import logger

from app.core.deps import get_current_db_user, get_current_business_id
from app.db.database import get_db
from app.db.models import (
    Campaign,
    CampaignLead,
    Lead,
    AIAgent,
    Call,
    LeadGroup,
    Client,
    InboundNumber,
    User,
    Conversation,
)
from app.api.routes.campaigns import check_campaign_access

router = APIRouter(prefix="/api/campaigns", tags=["campaign-ai-agent"])


class InboundTogglePayload(BaseModel):
    enabled: bool
    twilio_number: Optional[str] = None
    agent_id: Optional[str] = None


@router.get("/{campaign_id}/ai-agent/dashboard")
async def get_campaign_ai_agent_dashboard(
    campaign_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns real-time campaign-specific AI Agent dashboard metrics,
    lead calling summary, qualification rate, recent calls, and inbound status.
    All data is directly computed from PostgreSQL/SQLite database models.
    """
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    # Enforce isolation: Campaigns created for a Voice Agent must not be accessible to or dialed by the AI Agent
    camp_type = (campaign.agent_type or "").upper()
    is_voice_agent_camp = (
        camp_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT")
        or "HUMAN" in camp_type
        or (campaign.voice_agent_id and not campaign.agent_id)
    )
    if is_voice_agent_camp:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Campaigns created for a Voice Agent are not accessible to or dialed by the AI Agent."
        )

    # 1. Resolve Assigned AI Agent
    agent = None
    if campaign.agent_id:
        agent = await db.get(AIAgent, campaign.agent_id)
    if not agent:
        # Fallback to active AI Agent in the same business
        agent = await db.scalar(
            select(AIAgent).where(
                AIAgent.business_id == campaign.business_id,
                AIAgent.status == "ACTIVE"
            ).order_by(AIAgent.created_at.asc())
        )
    if not agent:
        # Global fallback active AI Agent
        agent = await db.scalar(
            select(AIAgent).where(AIAgent.status == "ACTIVE").order_by(AIAgent.created_at.asc())
        )

    # 2. Resolve Lead Group & Client details
    lead_group_name = "Lead Library"
    if campaign.lead_group_id:
        lg = await db.get(LeadGroup, campaign.lead_group_id)
        if lg and lg.filename:
            lead_group_name = lg.filename

    # 3. Compute Real Lead Statistics
    # Check CampaignLead first
    cl_count = await db.scalar(
        select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == campaign.id)
    )
    if cl_count and cl_count > 0:
        total_leads = cl_count
    elif campaign.lead_group_id:
        total_leads = await db.scalar(
            select(func.count(Lead.id)).where(Lead.group_id == campaign.lead_group_id)
        ) or 0
    else:
        total_leads = 0

    # 4. Compute Call Statistics for this specific Campaign
    calls_query = select(Call).where(Call.campaign_id == campaign.id)
    camp_calls = (await db.execute(calls_query)).scalars().all()

    calls_made = len(camp_calls)
    connected = 0
    no_response = 0
    qualified = 0
    not_qualified = 0
    callback = 0
    dnc = 0
    inbound_calls_count = 0
    outbound_calls_count = 0

    for c in camp_calls:
        st = (c.status or "").upper()
        res = (c.call_result or "").upper()
        dur = c.duration_seconds or 0
        direction = (c.direction or "OUTBOUND").upper()

        if direction == "INBOUND":
            inbound_calls_count += 1
        else:
            outbound_calls_count += 1

        # Connected logic
        if st in ("CONNECTED", "IN_PROGRESS", "COMPLETED", "AI_HANDLING", "HUMAN_HANDLING") or dur > 0 or "ANSWER" in res:
            connected += 1

        # No Response logic
        if st in ("NO_ANSWER", "MISSED", "BUSY", "FAILED") or "NO_ANSWER" in res or "BUSY" in res or "MISSED" in res:
            no_response += 1

        # Qualified / Not Qualified
        if ("QUALIFIED" in res and "NOT" not in res and "UN" not in res) or st == "QUALIFIED":
            qualified += 1
        elif ("NOT_QUALIFIED" in res or "UNQUALIFIED" in res or "NOT QUALIFIED" in res) or st == "NOT_QUALIFIED":
            not_qualified += 1

        # Callback & DNC
        if "CALLBACK" in res or "CALL_BACK" in res or st == "CALLBACK":
            callback += 1
        if "DNC" in res or "DO NOT CALL" in res or "DO_NOT_CALL" in res:
            dnc += 1

    # Also check Lead table qualification statuses if leads exist
    if qualified == 0 and not_qualified == 0 and campaign.lead_group_id:
        q_count = await db.scalar(
            select(func.count(Lead.id)).where(
                Lead.group_id == campaign.lead_group_id,
                Lead.qualification_status == "QUALIFIED"
            )
        )
        if q_count:
            qualified = q_count

        unq_count = await db.scalar(
            select(func.count(Lead.id)).where(
                Lead.group_id == campaign.lead_group_id,
                Lead.qualification_status.in_(["UNQUALIFIED", "NOT_QUALIFIED"])
            )
        )
        if unq_count:
            not_qualified = unq_count

    # Qualification rate
    denom = connected if connected > 0 else (calls_made if calls_made > 0 else total_leads)
    qualification_rate = round((qualified / denom * 100), 1) if denom > 0 else 0.0

    # 5. Inbound Configuration
    sched = campaign.schedule if isinstance(campaign.schedule, dict) else {}
    inbound_enabled = sched.get("inbound_enabled", True)

    # Check InboundNumber table synchronization
    inbound_number_record = None
    if campaign.twilio_number:
        clean_num = campaign.twilio_number.replace("+", "").strip()
        inbound_number_record = await db.scalar(
            select(InboundNumber).where(
                or_(
                    InboundNumber.phone_number == campaign.twilio_number,
                    InboundNumber.phone_number == clean_num,
                    InboundNumber.phone_number == f"+{clean_num}",
                )
            )
        )
        if inbound_number_record:
            # Sync status if needed
            inbound_enabled = inbound_number_record.status == "ACTIVE"

    # 6. Recent Call Activity (Last 15 calls)
    recent_calls_stmt = (
        select(Call)
        .where(Call.campaign_id == campaign.id)
        .order_by(Call.created_at.desc())
        .limit(15)
    )
    recent_calls_rows = (await db.execute(recent_calls_stmt)).scalars().all()
    recent_calls = []
    for rc in recent_calls_rows:
        lead_display_name = rc.client_name or "Prospect"
        lead_phone = rc.client_phone or rc.from_number if rc.direction == "INBOUND" else rc.to_number

        # Derive sentiment and intent from transcript or call result
        sentiment = "Positive" if "qualif" in (rc.call_result or "").lower() else ("Neutral" if rc.duration_seconds and rc.duration_seconds > 10 else "Inquiry")
        intent = "Product Inquiry" if rc.direction == "INBOUND" else "Campaign Outreach"
        if rc.call_result and rc.call_result != "—":
            intent = rc.call_result.title()

        recent_calls.append({
            "id": rc.id,
            "lead_name": lead_display_name,
            "phone": lead_phone,
            "direction": (rc.direction or "OUTBOUND").upper(),
            "status": rc.status or "COMPLETED",
            "duration_seconds": rc.duration_seconds or 0,
            "qualification": "Qualified" if ("qualif" in (rc.call_result or "").lower() and "not" not in (rc.call_result or "").lower()) else ("Not Qualified" if "not" in (rc.call_result or "").lower() else "Pending"),
            "disposition": rc.call_result or "Completed",
            "sentiment": sentiment,
            "intent": intent,
            "created_at": rc.created_at.isoformat() if rc.created_at else None,
            "recording_url": rc.recording_url or "",
            "transcript": rc.transcript or "",
        })

    return {
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "status": campaign.status or "IDLE",
            "agent_type": campaign.agent_type or "AI_VOICE",
            "twilio_number": campaign.twilio_number or "Not Configured",
            "lead_group_name": lead_group_name,
            "client_name": campaign.client_name or "—",
            "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
            "script": sched.get("script", ""),
            "auto_dial_enabled": bool(campaign.auto_dial_enabled),
            "auto_dial_ratio": int(campaign.auto_dial_ratio or 1),
        },
        "agent": {
            "id": agent.id if agent else None,
            "name": agent.name if agent else "AI Agent",
            "status": agent.status if agent else "ACTIVE",
            "voice_provider": agent.voice_provider if agent else "elevenlabs",
            "voice_id": agent.voice_id if agent else "rachel",
            "language": agent.language if agent else "en",
            "system_prompt": agent.system_prompt if agent else "",
            "description": agent.description if agent else "",
        },
        "lead_statistics": {
            "total_leads": total_leads,
            "calls_made": calls_made,
            "connected": connected,
            "no_response": no_response,
            "qualified": qualified,
            "not_qualified": not_qualified,
            "callback": callback,
            "dnc": dnc,
            "pending": max(0, total_leads - calls_made),
        },
        "call_statistics": {
            "total_calls": calls_made,
            "inbound_calls": inbound_calls_count,
            "outbound_calls": outbound_calls_count,
            "connected_calls": connected,
        },
        "qualification_statistics": {
            "total_leads": total_leads,
            "calls_attempted": calls_made,
            "connected": connected,
            "qualified": qualified,
            "not_qualified": not_qualified,
            "pending": max(0, total_leads - calls_made),
            "qualification_rate": qualification_rate,
        },
        "inbound_configuration": {
            "enabled": inbound_enabled,
            "twilio_number": campaign.twilio_number or "",
            "inbound_ai_agent": agent.name if agent else "AI Agent",
            "current_status": "Waiting for incoming calls" if inbound_enabled else "Inbound AI Disabled",
            "inbound_calls_count": inbound_calls_count,
        },
        "recent_calls": recent_calls,
    }


@router.get("/{campaign_id}/ai-agent/leads")
async def get_campaign_ai_agent_leads(
    campaign_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Returns campaign-specific leads with real calling & qualification status.
    """
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    # Enforce isolation: Campaigns created for a Voice Agent must not be accessible to or dialed by the AI Agent
    camp_type = (campaign.agent_type or "").upper()
    is_voice_agent_camp = (
        camp_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT")
        or "HUMAN" in camp_type
        or (campaign.voice_agent_id and not campaign.agent_id)
    )
    if is_voice_agent_camp:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Campaigns created for a Voice Agent are not accessible to or dialed by the AI Agent."
        )

    leads_list = []
    seen_ids = set()

    # Coerce limit and offset safely when invoked directly in tests
    limit_val = limit if isinstance(limit, int) else int(getattr(limit, "default", 100) or 100)
    offset_val = offset if isinstance(offset, int) else int(getattr(offset, "default", 0) or 0)

    # 1. Fetch from CampaignLead for this campaign (tenant safe)
    cl_stmt = (
        select(Lead, CampaignLead.status, CampaignLead.call_id)
        .join(CampaignLead, CampaignLead.lead_id == Lead.id)
        .where(CampaignLead.campaign_id == campaign.id)
        .order_by(CampaignLead.created_at.asc(), CampaignLead.id.asc())
    )
    if campaign.business_id:
        cl_stmt = cl_stmt.where(Lead.business_id == campaign.business_id)

    cl_rows = (await db.execute(cl_stmt)).all()
    cl_status_map = {}
    cl_call_id_map = {}
    for l, cl_status, cl_call_id in cl_rows:
        seen_ids.add(l.id)
        leads_list.append(l)
        cl_status_map[l.id] = cl_status
        cl_call_id_map[l.id] = cl_call_id

    # 2. Fallback to LeadGroup if CampaignLead is empty
    if not leads_list and campaign.lead_group_id:
        lg_stmt = (
            select(Lead)
            .where(Lead.group_id == campaign.lead_group_id)
            .order_by(Lead.created_at.asc(), Lead.id.asc())
        )
        if campaign.business_id:
            lg_stmt = lg_stmt.where(Lead.business_id == campaign.business_id)
        lg_leads = (await db.execute(lg_stmt)).scalars().all()
        for l in lg_leads:
            if l.id not in seen_ids:
                seen_ids.add(l.id)
                leads_list.append(l)

    total_assigned = len(leads_list)

    # Query latest call for each lead to show call details
    lead_ids = [l.id for l in leads_list]
    calls_by_lead: Dict[str, Call] = {}
    if lead_ids:
        calls_stmt = (
            select(Call)
            .where(Call.lead_id.in_(lead_ids))
            .order_by(Call.created_at.desc())
        )
        all_calls = (await db.execute(calls_stmt)).scalars().all()
        for c in all_calls:
            if c.lead_id and c.lead_id not in calls_by_lead:
                calls_by_lead[c.lead_id] = c

    agent_name = "AI Agent"
    if campaign.agent_id:
        ag = await db.get(AIAgent, campaign.agent_id)
        if ag:
            agent_name = ag.name

    from app.workers.dialer import ACTIVE_DIALER_STATUSES, PENDING_STATUSES
    from datetime import datetime

    now_utc = datetime.utcnow()
    db_dirty = False

    # Aggregate counts across all assigned campaign leads
    pending_count = 0
    active_count = 0
    completed_count = 0
    failed_count = 0
    dnd_count = 0

    all_resolved_leads = []
    for l in leads_list:
        latest_call = calls_by_lead.get(l.id)
        cl_status = cl_status_map.get(l.id)
        cl_call_id = cl_call_id_map.get(l.id)

        # Reconcile unended active call safely without premature 60s reaping
        if latest_call and latest_call.status in ACTIVE_DIALER_STATUSES and latest_call.ended_at is None:
            from app.services.voice_service import reconcile_unended_call
            if await reconcile_unended_call(db, latest_call, now_utc):
                db_dirty = True

        # Authoritative status resolution: PENDING leads waiting in queue are PENDING
        if cl_status in ("PENDING", "NEW", "RETRY_SCHEDULED", "UNASSIGNED"):
            call_status = "PENDING"
        elif cl_status in ("COMPLETED", "FAILED", "QUALIFIED", "CALLED", "NO_ANSWER", "BUSY", "CANCELLED", "Sale / Success"):
            call_status = cl_status
        elif cl_status in ACTIVE_DIALER_STATUSES:
            if latest_call and latest_call.ended_at is not None:
                call_status = latest_call.status
            else:
                call_status = cl_status
        elif latest_call and latest_call.status in ACTIVE_DIALER_STATUSES and latest_call.ended_at is None:
            call_status = latest_call.status
        elif latest_call:
            call_status = latest_call.status
        else:
            call_status = l.call_status or l.status or "PENDING"

        # Check DND status
        is_dnd = (l.status or "").upper() in ("DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED")
        st_upper = call_status.upper()

        if is_dnd:
            dnd_count += 1
        elif st_upper in [s.upper() for s in ACTIVE_DIALER_STATUSES]:
            active_count += 1
        elif st_upper in ("COMPLETED", "QUALIFIED", "CALLED", "SALE / SUCCESS"):
            completed_count += 1
        elif st_upper in ("FAILED", "NO_ANSWER", "BUSY", "CANCELLED"):
            failed_count += 1
        else:
            pending_count += 1

        # Synchronize terminal call status to lead model if previously left as RINGING
        if l.call_status in ACTIVE_DIALER_STATUSES and call_status not in ACTIVE_DIALER_STATUSES:
            l.call_status = call_status
            db.add(l)
            db_dirty = True

        qualification = l.qualification_status or ("Qualified" if latest_call and "qualif" in (latest_call.call_result or "").lower() else "Unqualified")
        disposition = latest_call.call_result if latest_call else (l.call_result or "—")
        duration = latest_call.duration_seconds if latest_call else (l.call_duration or 0)
        last_call_at = latest_call.created_at if latest_call else l.call_date_time

        # Derive sentiment & intent
        sentiment = "Positive" if "qualif" in str(disposition).lower() else ("Neutral" if duration > 10 else "Inquiry")
        intent = "Property Inquiry" if "realestate" in campaign.name.lower() else "Sales Inquiry"

        all_resolved_leads.append({
            "id": l.id,
            "call_id": (latest_call.id if latest_call else None) or cl_call_id,
            "name": f"{l.first_name or ''} {l.last_name or ''}".strip() or "Prospect",
            "phone": l.phone or "",
            "company": l.company or "",
            "call_status": call_status,
            "qualification": qualification,
            "disposition": disposition,
            "last_call": last_call_at.strftime("%Y-%m-%d %H:%M") if last_call_at else "—",
            "duration": duration,
            "agent": agent_name,
            "sentiment": sentiment,
            "intent": intent,
        })

    # Apply status filter if requested
    filtered_leads = []
    if status_filter and status_filter.upper() != "ALL":
        sf = status_filter.upper().strip()
        for item in all_resolved_leads:
            c_st = item["call_status"].upper()
            q_st = item["qualification"].upper()
            if sf == "ACTIVE" and c_st in [s.upper() for s in ACTIVE_DIALER_STATUSES]:
                filtered_leads.append(item)
            elif sf == "PENDING" and c_st in [s.upper() for s in PENDING_STATUSES]:
                filtered_leads.append(item)
            elif sf == "COMPLETED" and c_st in ("COMPLETED", "QUALIFIED", "CALLED", "SALE / SUCCESS"):
                filtered_leads.append(item)
            elif sf == "FAILED" and c_st in ("FAILED", "NO_ANSWER", "BUSY", "CANCELLED"):
                filtered_leads.append(item)
            elif sf in ("DND", "DNC") and c_st in ("DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED"):
                filtered_leads.append(item)
            elif sf in c_st or sf in q_st:
                filtered_leads.append(item)
    else:
        filtered_leads = all_resolved_leads

    total_matching = len(filtered_leads)
    paginated_leads = filtered_leads[offset_val : offset_val + limit_val]

    if db_dirty:
        try:
            await db.commit()
        except Exception:
            pass

    return {
        "campaign_id": campaign.id,
        "total_assigned": total_assigned,
        "total_returned": len(paginated_leads),
        "counts": {
            "total": total_assigned,
            "pending": pending_count,
            "active": active_count,
            "completed": completed_count,
            "failed": failed_count,
            "dnd": dnd_count,
        },
        "pagination": {
            "limit": limit_val,
            "offset": offset_val,
            "total": total_matching,
            "has_more": (offset_val + len(paginated_leads)) < total_matching,
        },
        "leads": paginated_leads,
    }


@router.get("/{campaign_id}/ai-agent/calls")
async def get_campaign_ai_agent_calls(
    campaign_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Returns campaign-specific call history (inbound & outbound).
    """
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    stmt = (
        select(Call)
        .where(Call.campaign_id == campaign.id)
        .order_by(Call.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    calls_rows = (await db.execute(stmt)).scalars().all()

    items = []
    for c in calls_rows:
        lead_name = c.client_name or "Prospect"
        phone = c.client_phone or c.from_number if c.direction == "INBOUND" else c.to_number
        sentiment = "Positive" if "qualif" in (c.call_result or "").lower() else "Neutral"
        intent = "Inbound Inquiry" if c.direction == "INBOUND" else "Campaign Outreach"
        if c.call_result and c.call_result != "—":
            intent = c.call_result.title()

        items.append({
            "id": c.id,
            "lead_name": lead_name,
            "phone": phone,
            "direction": (c.direction or "OUTBOUND").upper(),
            "status": c.status or "COMPLETED",
            "duration_seconds": c.duration_seconds or 0,
            "qualification": "Qualified" if ("qualif" in (c.call_result or "").lower() and "not" not in (c.call_result or "").lower()) else ("Not Qualified" if "not" in (c.call_result or "").lower() else "Pending"),
            "disposition": c.call_result or "Completed",
            "sentiment": sentiment,
            "intent": intent,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "recording_url": c.recording_url or "",
            "transcript": c.transcript or "",
        })

    total_count = await db.scalar(
        select(func.count(Call.id)).where(Call.campaign_id == campaign.id)
    ) or 0

    return {
        "campaign_id": campaign.id,
        "total_calls": total_count,
        "calls": items,
    }


@router.get("/{campaign_id}/inbound")
async def get_campaign_inbound_config(
    campaign_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns inbound AI calling configuration for this campaign.
    """
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    sched = campaign.schedule if isinstance(campaign.schedule, dict) else {}
    inbound_enabled = sched.get("inbound_enabled", True)

    agent_name = "AI Agent"
    if campaign.agent_id:
        ag = await db.get(AIAgent, campaign.agent_id)
        if ag:
            agent_name = ag.name

    return {
        "campaign_id": campaign.id,
        "campaign_name": campaign.name,
        "enabled": inbound_enabled,
        "twilio_number": campaign.twilio_number or "",
        "agent_id": campaign.agent_id,
        "agent_name": agent_name,
        "status": "ACTIVE" if inbound_enabled else "INACTIVE",
    }


@router.put("/{campaign_id}/inbound")
async def update_campaign_inbound_config(
    campaign_id: str,
    payload: InboundTogglePayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Enables or disables inbound AI calling for this campaign and updates InboundNumber table.
    """
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    # 1. Update Campaign schedule JSON with inbound_enabled flag
    sched = dict(campaign.schedule) if isinstance(campaign.schedule, dict) else {}
    sched["inbound_enabled"] = payload.enabled
    campaign.schedule = sched
    campaign.updated_at = datetime.utcnow()
    db.add(campaign)

    # 2. Update or create InboundNumber record for this Twilio number
    tw_number = payload.twilio_number or campaign.twilio_number
    if tw_number and tw_number.strip():
        tw_clean = tw_number.replace("+", "").strip()
        inb_row = await db.scalar(
            select(InboundNumber).where(
                or_(
                    InboundNumber.phone_number == tw_number,
                    InboundNumber.phone_number == tw_clean,
                    InboundNumber.phone_number == f"+{tw_clean}",
                )
            )
        )
        target_status = "ACTIVE" if payload.enabled else "INACTIVE"
        if inb_row:
            inb_row.status = target_status
            inb_row.routing_type = "VOICE_AGENT"
            if campaign.agent_id:
                inb_row.assigned_ai_agent_id = campaign.agent_id
            inb_row.updated_at = datetime.utcnow()
            db.add(inb_row)
        else:
            new_inb = InboundNumber(
                business_id=campaign.business_id,
                phone_number=tw_number,
                name=f"Inbound - {campaign.name}",
                description=f"AI Inbound line for campaign {campaign.name}",
                status=target_status,
                routing_type="VOICE_AGENT",
                assigned_ai_agent_id=campaign.agent_id,
            )
            db.add(new_inb)

    await db.commit()
    await db.refresh(campaign)

    logger.info(
        f"[CAMPAIGN_INBOUND] Campaign {campaign.id} ({campaign.name}) inbound set to enabled={payload.enabled}"
    )

    return {
        "success": True,
        "message": f"Inbound AI calling has been {'enabled' if payload.enabled else 'disabled'}",
        "enabled": payload.enabled,
        "twilio_number": campaign.twilio_number or "",
        "campaign_id": campaign.id,
    }
