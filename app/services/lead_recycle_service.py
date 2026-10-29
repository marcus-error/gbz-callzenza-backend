import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, desc

import io
import csv
from app.db.models import Lead, RecycleList, HopperEntry, Campaign, CampaignLead, User, Expert, LeadAssignment, Call, Business
from app.api.routes.websockets import manager
from loguru import logger


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_initials(name: str) -> str:
    parts = (name or "").strip().split()
    if not parts:
        return "CU"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


# ---------------------------------------------------------------------------
# RECYCLE KPIs — real counts from DB
# ---------------------------------------------------------------------------

async def get_recycle_kpis(db: AsyncSession, business_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Calculate real-time KPIs for Lead Recycle dashboard from actual DB data."""
    if not business_id:
        return [
            {"label": "Recycle Lists", "value": 0, "cls": ""},
            {"label": "Eligible Leads", "value": 0, "cls": ""},
            {"label": "Call Back", "value": 0, "cls": "purple"},
            {"label": "Voice Mail", "value": 0, "cls": "orange"},
            {"label": "In Hopper", "value": 0, "cls": "green"},
        ]

    # Count eligible leads (those with recycle-eligible dispositions, uncalled, or unqualified)
    eligible_query = select(func.count(Lead.id)).where(
        Lead.business_id == business_id,
        or_(
            Lead.qualification_status.in_(["UNQUALIFIED", "NOT_QUALIFIED"]),
            Lead.call_result.in_(["—", "CONNECTED", "NO_ANSWER", "BUSY", "FAILED", "Call Back", "Voice Mail", "Other"]),
            Lead.call_result.is_(None),
            Lead.call_result.ilike("%call back%"),
            Lead.call_result.ilike("%callback%"),
            Lead.call_result.ilike("%voice mail%"),
            Lead.call_result.ilike("%voicemail%"),
            Lead.status.in_(["Ready", "NEW", "COMPLETED", "CALLING", "FOLLOW_UP", "CALLBACK", "VOICEMAIL", "RETRY_SCHEDULED", "UNQUALIFIED"])
        )
    )
    eligible_count = (await db.scalar(eligible_query)) or 0

    # Call back count (leads in Call Back recycle lists or with callback disposition / follow up)
    cb_count_from_lists = (await db.scalar(
        select(func.coalesce(func.sum(RecycleList.total_count), 0))
        .where(
            RecycleList.source_disposition.ilike("%call back%"),
            RecycleList.business_id == business_id
        )
    )) or 0

    cb_query = select(func.count(Lead.id)).where(
        Lead.business_id == business_id,
        or_(
            Lead.call_result.ilike("%call back%"),
            Lead.call_result.ilike("%callback%"),
            Lead.status.in_(["CALLBACK", "FOLLOW_UP"]),
            Lead.qualification_status.in_(["UNQUALIFIED", "NOT_QUALIFIED"])
        )
    )
    callback_direct_count = (await db.scalar(cb_query)) or 0
    callback_count = max(int(cb_count_from_lists), int(callback_direct_count))

    # Voice mail count
    vm_query = select(func.count(Lead.id)).where(
        Lead.business_id == business_id,
        or_(
            Lead.call_result.ilike("%voice mail%"),
            Lead.call_result.ilike("%voicemail%"),
            Lead.status == "VOICEMAIL"
        )
    )
    voicemail_count = (await db.scalar(vm_query)) or 0

    # Recycle lists count
    rl_query = select(func.count(RecycleList.id)).where(RecycleList.business_id == business_id)
    recycle_lists_count = (await db.scalar(rl_query)) or 0

    # In Hopper count
    hop_query = select(func.count(HopperEntry.id)).where(
        HopperEntry.business_id == business_id,
        HopperEntry.status.in_(["Ready", "Dialing", "Waiting"])
    )
    in_hopper_count = (await db.scalar(hop_query)) or 0

    return [
        {"label": "Recycle Lists", "value": recycle_lists_count, "cls": ""},
        {"label": "Eligible Leads", "value": eligible_count, "cls": ""},
        {"label": "Call Back", "value": callback_count, "cls": "purple"},
        {"label": "Voice Mail", "value": voicemail_count, "cls": "orange"},
        {"label": "In Hopper", "value": in_hopper_count, "cls": "green"},
    ]


# ---------------------------------------------------------------------------
# RECYCLE LISTS — dynamic live DB lead counts
# ---------------------------------------------------------------------------

async def get_recycle_lists(
    db: AsyncSession,
    business_id: Optional[str] = None,
    query: str = "",
    disposition: str = "",
    campaign: str = "",
    status: str = ""
) -> List[Dict[str, Any]]:
    """Retrieve filtered recycle lists from the database with live synchronized lead counts."""
    if not business_id:
        return []

    # Auto-ensure recycle lists for campaigns that have leads but no recycle list yet
    if business_id:
        camps_with_leads = (await db.execute(
            select(Campaign.id, Campaign.name, func.count(CampaignLead.id))
            .join(CampaignLead, Campaign.id == CampaignLead.campaign_id)
            .where(Campaign.business_id == business_id)
            .group_by(Campaign.id, Campaign.name)
        )).all()

        existing_camps = set((await db.scalars(
            select(RecycleList.source_campaign_name).where(RecycleList.business_id == business_id)
        )).all())

        for c_id, c_name, lead_cnt in camps_with_leads:
            if c_name not in existing_camps:
                auto_list = RecycleList(
                    id=f"rl-{uuid.uuid4().hex[:6]}",
                    business_id=business_id,
                    title=f"Call Back — {c_name}",
                    description=f"Recycle lead list created from Call Back call disposition for {c_name}.",
                    icon="↻",
                    status="READY TO RECYCLE",
                    status_badge="b-w",
                    source_disposition="Call Back",
                    source_campaign_id=c_id,
                    source_campaign_name=c_name,
                    total_count=lead_cnt,
                    pending_count=lead_cnt,
                    recycled_count=0,
                    tags=[
                        {"label": "Call Back", "cls": "b-i"},
                        {"label": c_name, "cls": "b-p"},
                        {"label": "Eligible", "cls": "b-g"}
                    ],
                    created_at=_now()
                )
                db.add(auto_list)
                existing_camps.add(c_name)
        try:
            await db.flush()
        except Exception as flush_err:
            logger.warning(f"Note on auto-list flush: {flush_err}")

    db_query = select(RecycleList)
    if business_id:
        db_query = db_query.where(RecycleList.business_id == business_id)

    if disposition and disposition != "All Dispositions":
        db_query = db_query.where(RecycleList.source_disposition.ilike(f"%{disposition}%"))

    if campaign and campaign != "All Campaigns":
        db_query = db_query.where(RecycleList.source_campaign_name.ilike(f"%{campaign}%"))

    if status and status != "All Status":
        db_query = db_query.where(RecycleList.status.ilike(f"%{status}%"))

    db_query = db_query.order_by(desc(RecycleList.created_at))
    lists = (await db.execute(db_query)).scalars().all()

    res = []
    has_updates = False
    for rl in lists:
        search_blob = f"{rl.title} {rl.source_disposition} {rl.source_campaign_name} {rl.description} {rl.status}".lower()
        if query and query.lower() not in search_blob:
            continue

        # Resolve campaign to get actual real lead counts
        camp = None
        if rl.source_campaign_id:
            camp = await db.get(Campaign, rl.source_campaign_id)
        if not camp and rl.source_campaign_name:
            camp = (await db.execute(
                select(Campaign).where(Campaign.name == rl.source_campaign_name, Campaign.business_id == business_id)
            )).scalars().first()

        if camp:
            count_q = (
                select(func.count(CampaignLead.id))
                .join(Lead, CampaignLead.lead_id == Lead.id)
                .where(
                    CampaignLead.campaign_id == camp.id,
                    ~Lead.call_result.ilike("%sale%"),
                    ~Lead.call_result.ilike("%won%"),
                    ~Lead.call_result.ilike("%dnc%"),
                    ~Lead.call_result.ilike("%do not call%"),
                    Lead.qualification_status != "QUALIFIED"
                )
            )
            disp_target = rl.source_disposition or "Call Back"
            if "call" in disp_target.lower() and "back" in disp_target.lower():
                count_q = count_q.where(
                    or_(
                        Lead.call_result.ilike("%call%back%"),
                        Lead.call_result.ilike("%callback%"),
                        Lead.status.in_(["CALLBACK", "FOLLOW_UP"]),
                        Lead.call_result == disp_target
                    )
                )
            elif disp_target and disp_target not in ["All", "All Dispositions"]:
                count_q = count_q.where(
                    or_(
                        Lead.call_result.ilike(f"%{disp_target}%"),
                        Lead.status.ilike(f"%{disp_target}%")
                    )
                )
            total_cnt = (await db.scalar(count_q)) or 0
        else:
            total_cnt = (await db.scalar(
                select(func.count(Lead.id)).where(
                    Lead.business_id == business_id,
                    ~Lead.call_result.ilike("%sale%"),
                    ~Lead.call_result.ilike("%won%"),
                    ~Lead.call_result.ilike("%dnc%"),
                    Lead.qualification_status != "QUALIFIED"
                )
            )) or 0

        recycled_cnt = (await db.scalar(
            select(func.count(HopperEntry.id)).where(
                HopperEntry.recycle_list_id == rl.id,
                HopperEntry.business_id == business_id
            )
        )) or rl.recycled_count or 0
        pending_cnt = max(0, total_cnt - recycled_cnt)

        # Update cached counts in DB if changed
        if rl.total_count != total_cnt or rl.pending_count != pending_cnt or rl.recycled_count != recycled_cnt:
            rl.total_count = total_cnt
            rl.pending_count = pending_cnt
            rl.recycled_count = recycled_cnt
            if camp and not rl.source_campaign_id:
                rl.source_campaign_id = camp.id
            db.add(rl)
            has_updates = True

        created_str = rl.created_at.strftime("%I:%M %p") if rl.created_at else ""
        action_label = "Open Lead List →" if "READY" in rl.status else "Review Lead List →"
        action_style = "primary" if "READY" in rl.status else ""

        camp_name = rl.source_campaign_name or (camp.name if camp else "All Campaigns")
        clean_title = f"Call Back — {camp_name}" if camp_name != "All Campaigns" else (rl.title or "Call Back — Leads")

        res.append({
            "id": rl.id,
            "icon": rl.icon,
            "status": rl.status,
            "statusBadge": rl.status_badge,
            "title": clean_title,
            "description": rl.description,
            "tags": rl.tags or [],
            "campaign": camp_name,
            "total": total_cnt,
            "pending": pending_cnt,
            "recycled": recycled_cnt,
            "created": created_str,
            "reason": rl.source_disposition,
            "search": search_blob,
            "actionLabel": action_label,
            "actionStyle": action_style
        })

    if has_updates:
        try:
            await db.commit()
        except Exception as commit_err:
            logger.warning(f"Note on caching recycle counts: {commit_err}")
            await db.rollback()

    return res


# ---------------------------------------------------------------------------
# LEADS FOR A RECYCLE LIST — real leads from Campaign / DB
# ---------------------------------------------------------------------------

async def get_leads_for_recycle_list(
    db: AsyncSession,
    business_id: Optional[str],
    list_id: str,
    limit: int = 200
) -> List[Dict[str, Any]]:
    """Retrieve real leads belonging to the campaign / disposition of this recycle list."""
    rl_stmt = select(RecycleList).where(RecycleList.id == list_id)
    if business_id:
        rl_stmt = rl_stmt.where(RecycleList.business_id == business_id)
    rl = (await db.execute(rl_stmt)).scalars().first()
    disposition = rl.source_disposition if rl else "Call Back"

    # Resolve linked campaign
    camp = None
    if rl:
        if rl.source_campaign_id:
            camp = await db.get(Campaign, rl.source_campaign_id)
            if camp and business_id and camp.business_id != business_id:
                camp = None
        if not camp and rl.source_campaign_name:
            camp = (await db.execute(
                select(Campaign).where(Campaign.name == rl.source_campaign_name, Campaign.business_id == business_id)
            )).scalars().first()

    if camp:
        # Fetch leads joined via CampaignLead filtered by disposition and non-sale
        lead_query = (
            select(Lead, CampaignLead)
            .join(CampaignLead, Lead.id == CampaignLead.lead_id)
            .where(CampaignLead.campaign_id == camp.id)
        )
        if business_id:
            lead_query = lead_query.where(Lead.business_id == business_id)
        
        # Apply disposition filter if specified
        if disposition and disposition not in ["All", "All Dispositions"]:
            disp_lower = disposition.lower().strip()
            if "call" in disp_lower and "back" in disp_lower:
                lead_query = lead_query.where(
                    or_(
                        Lead.call_result.ilike("%call%back%"),
                        Lead.call_result.ilike("%callback%"),
                        Lead.status.in_(["CALLBACK", "FOLLOW_UP"]),
                        Lead.call_result == disposition,
                    )
                )
            else:
                lead_query = lead_query.where(
                    or_(
                        Lead.call_result.ilike(f"%{disposition}%"),
                        Lead.status.ilike(f"%{disposition}%")
                    )
                )

        # STRICTLY EXCLUDE sale, won, dnc, or qualified conversions from recycle list
        lead_query = lead_query.where(
            ~Lead.call_result.ilike("%sale%"),
            ~Lead.call_result.ilike("%won%"),
            ~Lead.call_result.ilike("%dnc%"),
            ~Lead.call_result.ilike("%do not call%"),
            ~Lead.status.ilike("%sale%"),
            Lead.qualification_status != "QUALIFIED"
        ).order_by(Lead.created_at.asc()).limit(limit)

        rows = (await db.execute(lead_query)).all()
        db_leads = [(r[0], r[1]) for r in rows]

        # If strict disposition returned empty (e.g. initial migration), fallback to non-sale campaign leads
        if not db_leads:
            fallback_query = (
                select(Lead, CampaignLead)
                .join(CampaignLead, Lead.id == CampaignLead.lead_id)
                .where(
                    CampaignLead.campaign_id == camp.id,
                    ~Lead.call_result.ilike("%sale%"),
                    ~Lead.call_result.ilike("%won%"),
                    ~Lead.call_result.ilike("%dnc%"),
                    Lead.qualification_status != "QUALIFIED"
                )
            )
            if business_id:
                fallback_query = fallback_query.where(Lead.business_id == business_id)
            fallback_query = fallback_query.order_by(Lead.created_at.asc()).limit(limit)
            rows = (await db.execute(fallback_query)).all()
            db_leads = [(r[0], r[1]) for r in rows]
    else:
        lead_query = (
            select(Lead)
            .where(
                Lead.business_id == business_id,
                ~Lead.call_result.ilike("%sale%"),
                ~Lead.call_result.ilike("%won%"),
                ~Lead.call_result.ilike("%dnc%"),
                Lead.qualification_status != "QUALIFIED"
            )
            .order_by(desc(Lead.updated_at))
            .limit(limit)
        )
        leads_raw = (await db.execute(lead_query)).scalars().all()
        db_leads = [(l, None) for l in leads_raw]

    formatted = []
    seen_ids = set()
    for idx, (lead, camp_lead) in enumerate(db_leads):
        if lead.id in seen_ids:
            continue
        seen_ids.add(lead.id)

        full_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or f"Customer {idx + 1}"
        initials = get_initials(full_name)

        # Get assigned agent name
        agent_name = "Voice Agent"
        try:
            agent_id = getattr(camp_lead, "assigned_expert_id", None) or lead.assigned_expert_id or lead.agent_id
            if agent_id:
                agent_user = await db.get(User, agent_id)
                if agent_user:
                    agent_name = agent_user.full_name or agent_user.email.split("@")[0]
        except Exception:
            pass

        attempts_num = getattr(camp_lead, "attempt_count", None) or lead.attempt_count or 1
        attempts = f"{attempts_num} / 5"

        last_call = ""
        last_dt = getattr(camp_lead, "last_attempt_at", None) or lead.last_attempt_at or lead.call_date_time
        if last_dt:
            last_call = last_dt.strftime("%I:%M %p")
        elif lead.created_at:
            last_call = lead.created_at.strftime("%I:%M %p")
        else:
            last_call = "10:30 AM"

        email = lead.email or f"{full_name.lower().replace(' ', '.')}@gmail.com"
        disp = lead.call_result if (lead.call_result and lead.call_result not in ["—", "-", ""]) else disposition
        if not disp or disp == "—":
            disp = disposition or "Call Back"

        # Determine recycle eligibility: unqualified, non-sale leads are eligible
        is_sale = bool(lead.call_result and any(w in lead.call_result.lower() for w in ["sale", "won", "dnc"]))
        is_qualified = lead.qualification_status == "QUALIFIED" or is_sale
        eligibility = "Not Eligible" if is_qualified else "Eligible"

        formatted.append({
            "id": lead.id,
            "name": full_name,
            "initials": initials,
            "phone": lead.phone or "—",
            "agent": agent_name,
            "attempts": attempts,
            "lastCall": last_call,
            "email": email,
            "status": lead.status or "Ready",
            "disposition": disp,
            "qualification_status": lead.qualification_status or "UNQUALIFIED",
            "eligibility": eligibility
        })

    return formatted


async def export_recycle_list_csv(
    db: AsyncSession,
    business_id: Optional[str],
    list_id: str
) -> str:
    """Generate CSV string for a recycle list with updated statuses."""
    leads = await get_leads_for_recycle_list(db=db, business_id=business_id, list_id=list_id, limit=1000)

    headers = ["ID", "Name", "Phone", "Email", "Agent", "Attempts", "Status", "Disposition", "Last Call"]
    rows = [headers]
    for lead in leads:
        rows.append([
            str(lead.get("id", "")),
            str(lead.get("name", "")),
            str(lead.get("phone", "")),
            str(lead.get("email", "")),
            str(lead.get("agent", "")),
            str(lead.get("attempts", "")),
            str(lead.get("status", "RECYCLED")),
            str(lead.get("disposition", "")),
            str(lead.get("lastCall", "")),
        ])

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(rows)
    return output.getvalue()


# ---------------------------------------------------------------------------
# CREATE RECYCLE LIST — real DB insertion
# ---------------------------------------------------------------------------

async def create_recycle_list(db: AsyncSession, business_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new custom recycle lead list."""
    source_disp = payload.get("source_disposition", "Call Back")
    source_camp = payload.get("source_campaign", "Product Campaign")
    list_name = payload.get("list_name") or f"{source_disp} — {source_camp}"
    retry_window = payload.get("retry_window", "Next available window")
    default_queue = payload.get("default_hopper_queue", "Auto Route")

    # Count real eligible leads matching this disposition
    lead_count_query = select(func.count(Lead.id)).where(
        Lead.business_id == business_id,
        or_(
            Lead.call_result.ilike(f"%{source_disp}%"),
            Lead.status.in_(["FOLLOW_UP", "CALLBACK", "VOICEMAIL", "RETRY_SCHEDULED", "UNQUALIFIED"])
        )
    )
    lead_count = (await db.scalar(lead_count_query)) or 0

    icon = "↻" if "call" in source_disp.lower() else "☎" if "voice" in source_disp.lower() else "⋯"

    new_list = RecycleList(
        id=f"rl-{uuid.uuid4().hex[:6]}",
        business_id=business_id,
        title=list_name,
        description=f"Recycle lead list created from {source_disp} call disposition for {source_camp}.",
        icon=icon,
        status="READY TO RECYCLE",
        status_badge="b-w",
        source_disposition=source_disp,
        source_campaign_name=source_camp,
        total_count=lead_count,
        pending_count=lead_count,
        recycled_count=0,
        retry_window=retry_window,
        default_hopper_queue=default_queue,
        tags=[
            {"label": source_disp, "cls": "b-i"},
            {"label": source_camp, "cls": "b-p"},
            {"label": "Eligible", "cls": "b-g"}
        ],
        created_at=_now()
    )
    db.add(new_list)
    await db.commit()
    await db.refresh(new_list)

    return {
        "success": True,
        "id": new_list.id,
        "title": new_list.title,
        "total": new_list.total_count,
        "status": new_list.status
    }


# ---------------------------------------------------------------------------
# RECYCLE LEADS — move real leads into the hopper
# ---------------------------------------------------------------------------

async def recycle_leads(
    db: AsyncSession,
    business_id: str,
    lead_ids: List[str],
    campaign_name: str,
    lane: str,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Execute the recycling of selected leads into the Lead Hopper."""
    queue_name = "Callback"
    queue_badge = "b-i"
    priority = "High"
    priority_cls = "red"

    if "Priority" in lane:
        queue_name = "Priority"
        queue_badge = "b-r"
        priority = "High"
        priority_cls = "red"
    elif "General" in lane:
        queue_name = "General"
        queue_badge = "b-p"
        priority = "Medium"
        priority_cls = "orange"
    elif "Auto" in lane:
        queue_name = "Callback"
        queue_badge = "b-i"
        priority = "High"
        priority_cls = "red"

    recycled_count = 0

    # Resolve target campaign
    target_camp_stmt = select(Campaign).where(Campaign.name == campaign_name)
    if business_id:
        target_camp_stmt = target_camp_stmt.where(Campaign.business_id == business_id)
    target_camp = (await db.execute(target_camp_stmt)).scalars().first()

    # Pre-fetch matching leads in a single batch query
    stmt_leads = select(Lead).where(Lead.id.in_(lead_ids))
    if business_id:
        stmt_leads = stmt_leads.where(Lead.business_id == business_id)
    matched_leads = (await db.execute(stmt_leads)).scalars().all()

    # Pre-fetch existing CampaignLead records in a single query to eliminate queries inside the loop
    existing_cls = {}
    if target_camp and matched_leads:
        m_ids = [l.id for l in matched_leads]
        stmt_cls = select(CampaignLead).where(
            CampaignLead.campaign_id == target_camp.id,
            CampaignLead.lead_id.in_(m_ids)
        )
        for cl in (await db.execute(stmt_cls)).scalars().all():
            existing_cls[cl.lead_id] = cl

    now_time = _now()
    for lead in matched_leads:
        lead.attempt_count = (lead.attempt_count or 0) + 1
        lead.last_attempt_at = now_time
        lead.status = "RETRY_SCHEDULED"

        entry = HopperEntry(
            id=_uuid(),
            business_id=business_id,
            lead_id=lead.id,
            campaign_id=target_camp.id if target_camp else None,
            campaign_name=campaign_name,
            queue_name=queue_name,
            queue_badge_cls=queue_badge,
            priority=priority,
            priority_cls=priority_cls,
            retry_count=lead.attempt_count,
            assigned_agent_name="Auto Route",
            status="Ready",
            status_badge_cls="b-g",
            entered_at=now_time
        )
        db.add(entry)

        # Synchronize CampaignLead record for the target campaign without running queries
        if target_camp:
            c_lead = existing_cls.get(lead.id)
            if c_lead:
                c_lead.status = "PENDING"
                c_lead.attempt_count = lead.attempt_count
                c_lead.last_attempt_at = now_time
                db.add(c_lead)
            else:
                new_cl = CampaignLead(
                    id=_uuid(),
                    campaign_id=target_camp.id,
                    lead_id=lead.id,
                    status="PENDING",
                    attempt_count=lead.attempt_count,
                    last_attempt_at=now_time,
                    created_at=now_time
                )
                db.add(new_cl)

        recycled_count += 1

    try:
        await db.commit()
    except Exception:
        await db.rollback()

    # Broadcast realtime update
    try:
        await manager.broadcast({
            "type": "LEAD_RECYCLED",
            "business_id": business_id,
            "count": recycled_count,
            "campaign": campaign_name,
            "lane": lane,
            "timestamp": _now().isoformat()
        })
        hop_count_stmt = select(func.count(HopperEntry.id)).where(
            HopperEntry.status.in_(["Ready", "Dialing", "Waiting"])
        )
        if business_id:
            hop_count_stmt = hop_count_stmt.where(HopperEntry.business_id == business_id)
        hop_count = (await db.scalar(hop_count_stmt)) or 0
        await manager.broadcast({
            "type": "HOPPER_UPDATED",
            "business_id": business_id,
            "total_in_hopper": hop_count,
            "timestamp": _now().isoformat()
        })
    except Exception:
        pass

    return {
        "success": True,
        "count": recycled_count,
        "campaign": campaign_name,
        "lane": lane
    }


# ---------------------------------------------------------------------------
# HOPPER LEADS — live calls in-progress, campaign queue, callbacks & hopper leads
# ---------------------------------------------------------------------------

async def get_hopper_leads(
    db: AsyncSession,
    business_id: Optional[str] = None,
    queue: str = "",
    campaign: str = "",
    status: str = "",
    search: str = ""
) -> List[Dict[str, Any]]:
    """Retrieve real-time live hopper calling leads: active calls in progress, campaign leads, callbacks and hopper entries."""
    if not business_id:
        return []

    now = _now()
    active_call_statuses = [
        'RINGING', 'IN_PROGRESS', 'DIALING', 'CALLING', 'QUEUED', 
        'CONNECTED', 'AI_HANDLING', 'HUMAN_HANDLING', 'CLIENT_ADDING', 
        'CONFERENCE_3WAY_ACTIVE', 'TRANSFER_REQUESTED'
    ]

    # Pre-fetch lookup caches strictly for this business_id
    camps_all = (await db.execute(select(Campaign).where(Campaign.business_id == business_id))).scalars().all()
    camp_map = {c.id: c.name for c in camps_all}
    users_all = (await db.execute(select(User).where(User.business_id == business_id))).scalars().all()
    user_map = {u.id: (u.full_name or (u.email.split("@")[0] if u.email else "Agent")) for u in users_all}
    experts_all = (await db.execute(select(Expert).where(Expert.business_id == business_id))).scalars().all()
    expert_map = {e.id: e.name for e in experts_all}

    items = []
    seen_lead_ids = set()

    # 1. LIVE ACTIVE CALLS IN PROGRESS (Being Dialed / Attended right now!)
    # Only include calls from actively RUNNING campaigns initiated TODAY
    today_start = datetime.combine(now.date(), datetime.min.time())
    running_camp_ids = [c.id for c in camps_all if c.status == "RUNNING"]

    active_calls = []
    if running_camp_ids:
        call_stmt = select(Call).where(
            Call.business_id == business_id,
            Call.status.in_(active_call_statuses), 
            Call.ended_at.is_(None),
            Call.campaign_id.in_(running_camp_ids),
            or_(Call.initiated_at >= today_start, Call.created_at >= today_start)
        ).order_by(desc(Call.initiated_at))
        active_calls = (await db.execute(call_stmt)).scalars().all()

    for call in active_calls:
        lead = None
        if call.lead_id:
            lead = await db.get(Lead, call.lead_id)
            if lead and lead.business_id == business_id:
                seen_lead_ids.add(call.lead_id)
            else:
                lead = None
        elif call.client_phone or call.to_number:
            p_num = (call.client_phone or call.to_number or "").strip()
            if len(p_num) >= 6:
                clean_tail = p_num[-8:] if len(p_num) >= 8 else p_num
                l_match = (await db.execute(
                    select(Lead).where(
                        Lead.business_id == business_id,
                        or_(
                            Lead.phone == p_num,
                            Lead.phone.ilike(f"%{clean_tail}%")
                        )
                    )
                )).scalars().first()
                if l_match:
                    lead = l_match
                    seen_lead_ids.add(l_match.id)

        cust_name = ""
        if lead:
            cust_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip()
        if not cust_name:
            cust_name = call.client_name or "Live Customer"
        
        cust_phone = (lead.phone if lead else call.client_phone or call.to_number) or "-"
        camp_name = camp_map.get(call.campaign_id or "") or (call.campaign_id if call.campaign_id else "Live Calling")
        agent_name = expert_map.get(call.agent_id or "") or user_map.get(call.agent_id or "") or "Voice Agent"
        
        # Calculate live duration
        base_time = call.connected_at or call.initiated_at or call.created_at or now
        diff_secs = max(0, int((now - base_time).total_seconds()))
        wait_str = f"{diff_secs // 60}m {diff_secs % 60:02d}s" if diff_secs >= 60 else f"{diff_secs}s"

        call_disp_status = "Dialing"
        if call.status in ["CONNECTED", "IN_PROGRESS", "HUMAN_HANDLING", "AI_HANDLING", "CONFERENCE_3WAY_ACTIVE"]:
            call_disp_status = "In Call"
        elif call.status in ["RINGING"]:
            call_disp_status = "Ringing"

        items.append({
            "id": call.id,
            "lead_id": (lead.id if lead else call.lead_id) or call.id,
            "initials": get_initials(cust_name),
            "name": cust_name,
            "sub": f"{cust_phone} | Live Call In-Progress",
            "campaign": camp_name,
            "queue": "Live Calling",
            "queueCls": "b-r",
            "priority": "High",
            "priorityCls": "red",
            "wait": wait_str,
            "agent": agent_name,
            "status": call_disp_status,
            "statusCls": "b-g" if call_disp_status == "In Call" else "b-r",
            "_order": 1,
            "_raw_wait": diff_secs
        })

    # 2. RECYCLED HOPPER ENTRY RECORDS (Explicitly recycled leads - highest priority queue)
    hop_entries = (await db.execute(
        select(HopperEntry)
        .where(HopperEntry.business_id == business_id)
        .order_by(desc(HopperEntry.entered_at))
    )).scalars().all()

    for e in hop_entries:
        if e.lead_id in seen_lead_ids:
            continue
        lead = await db.get(Lead, e.lead_id)
        if not lead or lead.business_id != business_id:
            continue
        seen_lead_ids.add(e.lead_id)
        cust_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Customer"
        cust_phone = lead.phone or "-"
        diff_secs = max(0, int((now - (e.entered_at or now)).total_seconds()))
        diff_mins = diff_secs // 60
        wait_str = f"{diff_mins} min" if diff_mins > 0 else "< 1 min"

        # Order: Callback/Priority queue = 2, other recycled = 3
        is_high_pri = "callback" in (e.queue_name or "").lower() or "priority" in (e.queue_name or "").lower()
        order_val = 2 if is_high_pri else 3

        items.append({
            "id": e.id,
            "lead_id": e.lead_id,
            "initials": get_initials(cust_name),
            "name": cust_name,
            "sub": f"{cust_phone} | Retry #{e.retry_count or 1}",
            "campaign": e.campaign_name,
            "queue": e.queue_name,
            "queueCls": e.queue_badge_cls,
            "priority": e.priority,
            "priorityCls": e.priority_cls,
            "wait": wait_str,
            "agent": e.assigned_agent_name,
            "status": e.status,
            "statusCls": e.status_badge_cls,
            "_order": order_val,
            "_raw_wait": diff_secs
        })

    # 3. CAMPAIGN LEADS (Active leads queued in campaigns)
    if camp_map:
        cl_stmt = (
            select(CampaignLead, Lead, Campaign)
            .join(Lead, CampaignLead.lead_id == Lead.id)
            .join(Campaign, CampaignLead.campaign_id == Campaign.id)
            .where(
                Campaign.business_id == business_id,
                Lead.business_id == business_id,
                CampaignLead.status.notin_([
                    'COMPLETED', 'DISQUALIFIED', 'DNC', 'PROCESSED', 
                    'NOT_QUALIFIED', 'Sale / Success', 'SALE', 'APPOINTMENT_SET'
                ])
            )
            .order_by(desc(CampaignLead.created_at))
        )
        cl_rows = (await db.execute(cl_stmt)).all()
        for cl, lead, camp in cl_rows:
            if lead.id in seen_lead_ids:
                continue
            seen_lead_ids.add(lead.id)

            cust_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Customer"
            cust_phone = lead.phone or "-"
            agent_name = expert_map.get(cl.assigned_expert_id or lead.assigned_expert_id or "") or (camp.name if camp else "Auto Route")

            cl_status_up = (cl.status or "").upper()
            lead_status_up = (lead.status or "").upper()
            lead_cb = (lead.call_result or "").upper()

            if "CALLBACK" in cl_status_up or "CALL BACK" in cl_status_up or "CALLBACK" in lead_status_up or "CALL BACK" in lead_status_up or "CALLBACK" in lead_cb or "CALL BACK" in lead_cb:
                lead_disp_status = "Call Back"
                lead_queue = "Callback"
                queue_badge = "b-i"
                priority = "High"
                priority_cls = "red"
                status_badge = "b-w"
                order_val = 2
            elif cl_status_up in ["CALLING", "DIALING", "RINGING", "IN_CALL"] and (camp and camp.status == "RUNNING"):
                lead_disp_status = "Dialing"
                lead_queue = "Live Calling"
                queue_badge = "b-r"
                priority = "High"
                priority_cls = "red"
                status_badge = "b-r"
                order_val = 1
            elif cl_status_up in ["WAITING", "QUEUED"]:
                lead_disp_status = "Waiting"
                lead_queue = "General"
                queue_badge = "b-p"
                priority = "Medium"
                priority_cls = "orange"
                status_badge = "b-i"
                order_val = 3
            else:
                lead_disp_status = "Ready"
                lead_queue = "General"
                queue_badge = "b-p"
                priority = "Normal"
                priority_cls = "green"
                status_badge = "b-g"
                order_val = 3

            base_time = cl.last_attempt_at or cl.created_at or lead.created_at or now
            diff_secs = max(0, int((now - base_time).total_seconds()))
            diff_mins = diff_secs // 60
            wait_str = f"{diff_mins} min" if diff_mins > 0 else "< 1 min"

            attempts_txt = f"Retry #{cl.attempt_count}" if (cl.attempt_count or 0) > 0 else "Ready in Queue"

            items.append({
                "id": cl.id,
                "lead_id": lead.id,
                "initials": get_initials(cust_name),
                "name": cust_name,
                "sub": f"{cust_phone} | {attempts_txt}",
                "campaign": camp.name,
                "queue": lead_queue,
                "queueCls": queue_badge,
                "priority": priority,
                "priorityCls": priority_cls,
                "wait": wait_str,
                "agent": agent_name,
                "status": lead_disp_status,
                "statusCls": status_badge,
                "_order": order_val,
                "_raw_wait": diff_secs
            })

    # 4. DIRECT GROUP LEADS for active campaigns without CampaignLead entries
    camps_with_group = [c for c in camps_all if c.lead_group_id]
    for c in camps_with_group:
        g_leads_stmt = select(Lead).where(
            Lead.business_id == business_id,
            Lead.group_id == c.lead_group_id,
            Lead.status.notin_(['COMPLETED', 'DISQUALIFIED', 'DNC', 'PROCESSED'])
        ).limit(100)
        g_leads = (await db.execute(g_leads_stmt)).scalars().all()
        for lead in g_leads:
            if lead.id in seen_lead_ids:
                continue
            seen_lead_ids.add(lead.id)

            cust_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Customer"
            cust_phone = lead.phone or "-"
            agent_name = expert_map.get(lead.assigned_expert_id or "") or "Auto Route"

            diff_secs = max(0, int((now - (lead.created_at or now)).total_seconds()))
            diff_mins = diff_secs // 60
            wait_str = f"{diff_mins} min" if diff_mins > 0 else "< 1 min"

            items.append({
                "id": f"g-{lead.id}",
                "lead_id": lead.id,
                "initials": get_initials(cust_name),
                "name": cust_name,
                "sub": f"{cust_phone} | Campaign Hopper",
                "campaign": c.name,
                "queue": "General",
                "queueCls": "b-p",
                "priority": "Normal",
                "priorityCls": "green",
                "wait": wait_str,
                "agent": agent_name,
                "status": "Ready",
                "statusCls": "b-g",
                "_order": 4,
                "_raw_wait": diff_secs
            })

    # Sort items: live calls first (_order=1), callbacks/priority second (_order=2), ready third (_order=3,4)
    items.sort(key=lambda x: (x.get("_order") if x.get("_order") is not None else 5, x.get("_raw_wait") if x.get("_raw_wait") is not None else 0))

    # Apply filters
    filtered = []
    for it in items:
        if queue and queue != "All Queues":
            q_clean = queue.replace(" Queue", "").lower()
            if q_clean not in (it["queue"] or "").lower():
                continue
        if campaign and campaign != "All Campaigns":
            c_clean = campaign.replace(" Campaign", "").lower()
            if c_clean not in (it["campaign"] or "").lower():
                continue
        if status and status != "All Status":
            s_clean = status.lower()
            if s_clean not in (it["status"] or "").lower():
                continue
        if search:
            blob = f"{it['name']} {it['sub']} {it['campaign']} {it['queue']} {it['priority']} {it['status']} {it['agent']}".lower()
            if search.lower() not in blob:
                continue

        filtered.append(it)

    # Assign sequential numbers
    for idx, f_item in enumerate(filtered):
        f_item["no"] = f"{idx + 1:02d}"

    return filtered


# ---------------------------------------------------------------------------
# HOPPER KPIs — real counts computed from live hopper queue
# ---------------------------------------------------------------------------

async def get_hopper_kpis(db: AsyncSession, business_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Compute real-time KPIs for Lead Hopper calling queue."""
    if not business_id:
        return [
            {"label": "Total in Hopper", "value": 0, "cls": ""},
            {"label": "Ready to Dial", "value": 0, "cls": "green"},
            {"label": "Being Dialed", "value": 0, "cls": ""},
            {"label": "Callback Queue", "value": 0, "cls": "purple"},
            {"label": "Avg Wait", "value": "—", "cls": ""},
        ]

    leads = await get_hopper_leads(db=db, business_id=business_id)
    total_in_hopper = len(leads)
    ready_count = sum(1 for l in leads if l["status"].lower() in ["ready", "waiting"])
    dialing_count = sum(1 for l in leads if l["status"].lower() in ["dialing", "in call", "ringing"])
    callback_count = sum(1 for l in leads if l["status"].lower() in ["call back", "callback"] or "callback" in (l.get("queue") or "").lower())
    
    raw_waits = [l.get("_raw_wait", 0) for l in leads if l.get("_raw_wait") is not None]
    if raw_waits:
        avg_s = sum(raw_waits) / len(raw_waits)
        avg_wait_str = f"{int(avg_s // 60)}m {int(avg_s % 60):02d}s" if avg_s >= 60 else f"{int(avg_s)}s"
    else:
        avg_wait_str = "—"

    return [
        {"label": "Total in Hopper", "value": total_in_hopper, "cls": ""},
        {"label": "Ready to Dial", "value": ready_count, "cls": "green"},
        {"label": "Being Dialed", "value": dialing_count, "cls": ""},
        {"label": "Callback Queue", "value": callback_count, "cls": "purple"},
        {"label": "Avg Wait", "value": avg_wait_str, "cls": ""},
    ]
