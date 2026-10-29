from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Any, List, Dict

from app.core.deps import require_supervisor_or_admin
from app.db.database import get_db
from app.db.models import User, Campaign, Lead, Business, Call, LeadGroup, Client, Expert, AIAgent

router = APIRouter(prefix="/api/admin", tags=["admin_campaigns"])


from loguru import logger
from datetime import datetime, timezone

class AdminLeadGroupOut(BaseModel):
    id: str
    filename: Optional[str] = ""
    lead_count: Optional[int] = 0
    status: Optional[str] = "COMPLETED"
    business_name: Optional[str] = "Unknown Business"
    created_at: Optional[datetime] = None


class AdminCampaignOut(BaseModel):
    id: str
    name: Optional[str] = "Unnamed Campaign"
    status: Optional[str] = "IDLE"
    twilio_number: Optional[str] = "Default"
    business_name: Optional[str] = "Unknown Business"
    client_name: Optional[str] = ""
    client_phone: Optional[str] = ""
    agent_type: Optional[str] = "AI_VOICE"
    agent_id: Optional[str] = None
    voice_agent_id: Optional[str] = None
    voice_agent_name: Optional[str] = None
    ai_agent_name: Optional[str] = None
    lead_group_name: Optional[str] = None
    auto_dial_enabled: Optional[bool] = False
    auto_dial_ratio: Optional[int] = 1
    created_at: Optional[datetime] = None


class AdminCallOut(BaseModel):
    id: str
    direction: Optional[str] = "OUTBOUND"
    from_number: Optional[str] = ""
    to_number: Optional[str] = ""
    status: Optional[str] = "COMPLETED"
    duration_seconds: Optional[int] = 0
    business_name: Optional[str] = "Unknown Business"
    created_at: Optional[datetime] = None


class AdminLeadOut(BaseModel):
    id: str
    first_name: Optional[str] = ""
    last_name: Optional[str] = ""
    phone: Optional[str] = ""
    email: Optional[str] = ""
    company: Optional[str] = ""
    status: Optional[str] = "NEW"
    business_name: Optional[str] = "Unknown Business"
    created_at: Optional[datetime] = None


def _as_utc(dt: Any) -> datetime:
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    if not dt or not isinstance(dt, datetime):
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ratio(val: Any) -> int:
    if val is None:
        return 1
    if isinstance(val, (int, float)):
        return max(1, int(val))
    if isinstance(val, str):
        v_str = val.strip()
        if ":" in v_str:
            parts = [p.strip() for p in v_str.split(":") if p.strip()]
            nums = [int(p) for p in parts if p.isdigit()]
            if nums:
                return max(1, max(nums))
            return 1
        try:
            return max(1, int(v_str))
        except Exception:
            return 1
    return 1


@router.get("/campaigns", response_model=list[AdminCampaignOut])
async def admin_list_campaigns(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> list:
    try:
        stmt = (
            select(
                Campaign,
                Business.name.label("business_name"),
                Client.name.label("client_name_val"),
                LeadGroup.filename.label("lead_group_name_val"),
                Expert.name.label("expert_name_val"),
                AIAgent.name.label("ai_agent_name_val"),
            )
            .outerjoin(Business, Campaign.business_id == Business.id)
            .outerjoin(Client, Campaign.client_id == Client.id)
            .outerjoin(LeadGroup, Campaign.lead_group_id == LeadGroup.id)
            .outerjoin(Expert, Campaign.voice_agent_id == Expert.id)
            .outerjoin(AIAgent, Campaign.agent_id == AIAgent.id)
        )
        if (current_user.role or "").upper() != "SUPER_ADMIN":
            if current_user.business_id:
                stmt = stmt.where(Campaign.business_id == current_user.business_id)
            else:
                return []
        stmt = stmt.order_by(Campaign.created_at.desc())
        result = await db.execute(stmt)
        
        out = []
        for row in result.all():
            campaign = row[0]
            biz_name = row[1]
            client_name_val = row[2]
            lead_group_name_val = row[3]
            expert_name_val = row[4]
            ai_agent_name_val = row[5]

            raw_agent_type = str(campaign.agent_type or "").upper()
            if raw_agent_type in ("HUMAN", "VOICE", "VOICE_AGENT"):
                agent_type_val = "HUMAN"
            elif raw_agent_type in ("AI", "AI_VOICE", "AI_AGENT"):
                agent_type_val = "AI_VOICE"
            elif campaign.voice_agent_id and not campaign.agent_id:
                agent_type_val = "HUMAN"
            else:
                agent_type_val = "AI_VOICE"

            voice_name = expert_name_val or ("Voice Agent" if agent_type_val == "HUMAN" else None)
            ai_name = ai_agent_name_val or ("AI Agent" if agent_type_val == "AI_VOICE" else None)

            out.append({
                "id": str(campaign.id),
                "name": str(campaign.name or "Unnamed Campaign"),
                "status": str(campaign.status or "IDLE"),
                "twilio_number": str(campaign.twilio_number or "Default"),
                "business_name": str(biz_name or "Unknown Business"),
                "client_name": str(campaign.client_name or client_name_val or ""),
                "client_phone": str(campaign.client_phone or ""),
                "agent_type": str(agent_type_val),
                "agent_id": str(campaign.agent_id) if campaign.agent_id else None,
                "voice_agent_id": str(campaign.voice_agent_id) if campaign.voice_agent_id else None,
                "voice_agent_name": voice_name,
                "ai_agent_name": ai_name,
                "lead_group_name": str(lead_group_name_val or ""),
                "auto_dial_enabled": bool(campaign.auto_dial_enabled),
                "auto_dial_ratio": _parse_ratio(campaign.auto_dial_ratio),
                "created_at": _as_utc(campaign.created_at),
            })
        return out
    except Exception as e:
        logger.error(f"[ADMIN CAMPAIGNS ERROR] {e}")
        return []


@router.get("/leads", response_model=list[AdminLeadOut])
async def admin_list_leads(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> list:
    try:
        stmt = (
            select(Lead, Business.name.label("business_name"))
            .outerjoin(Business, Lead.business_id == Business.id)
        )
        if (current_user.role or "").upper() != "SUPER_ADMIN":
            if current_user.business_id:
                stmt = stmt.where(Lead.business_id == current_user.business_id)
            else:
                return []
        stmt = stmt.order_by(Lead.created_at.desc())
        result = await db.execute(stmt)
        
        out = []
        for row in result.all():
            lead, biz_name = row[0], row[1]
            out.append({
                "id": str(lead.id),
                "first_name": str(lead.first_name or ""),
                "last_name": str(lead.last_name or ""),
                "phone": str(lead.phone or ""),
                "email": str(lead.email or ""),
                "company": str(lead.company or ""),
                "status": str(lead.status or "NEW"),
                "business_name": str(biz_name or "Unknown Business"),
                "created_at": _as_utc(lead.created_at),
            })
        return out
    except Exception as e:
        logger.error(f"[ADMIN LEADS ERROR] {e}")
        return []


@router.get("/calls", response_model=list[AdminCallOut])
async def admin_list_calls(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> list:
    try:
        stmt = (
            select(Call, Business.name.label("business_name"))
            .outerjoin(Business, Call.business_id == Business.id)
        )
        if (current_user.role or "").upper() != "SUPER_ADMIN":
            if current_user.business_id:
                stmt = stmt.where(Call.business_id == current_user.business_id)
            else:
                return []
        stmt = stmt.order_by(Call.created_at.desc())
        result = await db.execute(stmt)
        
        out = []
        for row in result.all():
            call, biz_name = row[0], row[1]
            out.append({
                "id": str(call.id),
                "direction": str(call.direction or "OUTBOUND"),
                "from_number": str(call.from_number or ""),
                "to_number": str(call.to_number or ""),
                "status": str(call.status or "COMPLETED"),
                "duration_seconds": int(call.duration_seconds or 0),
                "business_name": str(biz_name or "Unknown Business"),
                "created_at": _as_utc(call.created_at),
            })
        return out
    except Exception as e:
        logger.error(f"[ADMIN CALLS ERROR] {e}")
        return []


@router.get("/leads/uploads", response_model=list[AdminLeadGroupOut])
async def admin_list_lead_uploads(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
) -> list:
    try:
        stmt = (
            select(LeadGroup, Business.name.label("business_name"))
            .outerjoin(Business, LeadGroup.business_id == Business.id)
        )
        if (current_user.role or "").upper() != "SUPER_ADMIN":
            if current_user.business_id:
                stmt = stmt.where(LeadGroup.business_id == current_user.business_id)
            else:
                return []
        stmt = stmt.order_by(LeadGroup.created_at.desc())
        result = await db.execute(stmt)
        
        out = []
        for row in result.all():
            group, biz_name = row[0], row[1]
            out.append({
                "id": str(group.id),
                "filename": str(group.filename or ""),
                "lead_count": int(group.lead_count or 0),
                "status": str(group.status or "COMPLETED"),
                "business_name": str(biz_name or "Unknown Business"),
                "created_at": _as_utc(group.created_at),
            })
        return out
    except Exception as e:
        logger.error(f"[ADMIN LEADS UPLOADS ERROR] {e}")
        return []
