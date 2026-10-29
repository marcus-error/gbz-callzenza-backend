from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_business_id, get_current_db_user, require_supervisor_or_admin
from app.db.database import get_db
from app.db.models import AIAgent, User, Business
from app.schemas.agent import AgentCreate, AgentOut, AgentUpdate, VoicePreviewRequest

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(
    payload: AgentCreate,
    user: User = Depends(require_supervisor_or_admin),
    db: AsyncSession = Depends(get_db),
) -> AIAgent:
    business_id = user.business_id
    if not business_id or business_id.strip() == "":
        res = await db.execute(select(Business).limit(1))
        b = res.scalar()
        if b:
            business_id = b.id
        else:
            import uuid
            biz = Business(id=str(uuid.uuid4()), name="Default Business", owner_user_id=user.id)
            db.add(biz)
            await db.flush()
            business_id = biz.id

    data = payload.model_dump()
    if payload.voice_config_id:
        from app.db.models import Integration
        integ = await db.get(Integration, payload.voice_config_id)
        if integ and (not business_id or integ.business_id == business_id or user.role == "SUPER_ADMIN"):
            cfg = integ.config or {}
            data["voice_config_id"] = integ.id
            data["voice_provider"] = integ.provider
            data["voice_id"] = payload.voice_id or cfg.get("voice_id") or data.get("voice_id")
            data["voice_name"] = payload.voice_name or cfg.get("voice_name") or cfg.get("name") or data.get("voice_name")
            data["voice_model_id"] = payload.voice_model_id or cfg.get("model_id") or data.get("voice_model_id")

    agent = AIAgent(business_id=business_id, **data)
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@router.get("", response_model=list[AgentOut])
async def list_agents(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> list[AIAgent]:
    user_biz = user.business_id
    if user_biz:
        result = await db.scalars(
            select(AIAgent).where(AIAgent.business_id == user_biz).order_by(AIAgent.created_at.desc())
        )
        return list(result.all())
    user_role = (user.role or "").strip().upper()
    if user_role == "SUPER_ADMIN":
        result = await db.scalars(select(AIAgent).order_by(AIAgent.created_at.desc()))
        return list(result.all())
    return []


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> AIAgent:
    agent = await db.get(AIAgent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if user.business_id and agent.business_id != user.business_id:
        raise HTTPException(status_code=403, detail="Forbidden: You do not have access to this agent")
    return agent


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    user: User = Depends(require_supervisor_or_admin),
    db: AsyncSession = Depends(get_db),
) -> AIAgent:
    agent = await db.get(AIAgent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if user.business_id and agent.business_id != user.business_id:
        raise HTTPException(status_code=403, detail="Forbidden: You do not have access to this agent")
    updates = payload.model_dump(exclude_unset=True)
    if "voice_config_id" in updates and updates["voice_config_id"]:
        from app.db.models import Integration
        integ = await db.get(Integration, updates["voice_config_id"])
        if integ and (not user.business_id or integ.business_id == user.business_id or user.role == "SUPER_ADMIN"):
            cfg = integ.config or {}
            updates["voice_config_id"] = integ.id
            updates["voice_provider"] = integ.provider
            if "voice_id" not in updates or not updates["voice_id"]:
                updates["voice_id"] = cfg.get("voice_id") or agent.voice_id
            if "voice_name" not in updates or not updates["voice_name"]:
                updates["voice_name"] = cfg.get("voice_name") or cfg.get("name") or agent.voice_name
            if "voice_model_id" not in updates or not updates["voice_model_id"]:
                updates["voice_model_id"] = cfg.get("model_id") or agent.voice_model_id

    for field, value in updates.items():
        setattr(agent, field, value)
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    user: User = Depends(require_supervisor_or_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    agent = await db.get(AIAgent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if user.business_id and agent.business_id != user.business_id:
        raise HTTPException(status_code=403, detail="Forbidden: You do not have access to this agent")
    from app.db.models import Campaign, CampaignLead, Lead, Appointment, Call, Conversation
    from sqlalchemy import update, delete

    # Find campaign IDs using this agent
    campaign_ids_stmt = select(Campaign.id).where(Campaign.agent_id == agent_id)
    campaign_ids_res = await db.execute(campaign_ids_stmt)
    campaign_ids = list(campaign_ids_res.scalars().all())

    if campaign_ids:
        # Delete related campaign leads
        await db.execute(delete(CampaignLead).where(CampaignLead.campaign_id.in_(campaign_ids)))
        # Delete related campaigns
        await db.execute(delete(Campaign).where(Campaign.id.in_(campaign_ids)))

    # Nullify references in other entities
    await db.execute(update(Lead).where(Lead.agent_id == agent_id).values(agent_id=None))
    await db.execute(update(Appointment).where(Appointment.agent_id == agent_id).values(agent_id=None))
    await db.execute(update(Call).where(Call.agent_id == agent_id).values(agent_id=None))
    await db.execute(update(Conversation).where(Conversation.agent_id == agent_id).values(agent_id=None))

    await db.delete(agent)
    await db.commit()


class AgentChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None


class AgentChatResponse(BaseModel):
    response: str
    conversation_id: str
    agent_id: str
    language: str = "en"
    latency_ms: Optional[int] = 0


@router.post("/{agent_id}/chat", response_model=AgentChatResponse)
async def chat_with_agent(
    agent_id: str,
    payload: AgentChatRequest,
    db: AsyncSession = Depends(get_db),
) -> AgentChatResponse:
    """Conversational AI Chat endpoint powered by the unified Agent Engine and Agent-Specific RAG."""
    from app.services.ai.agent_engine import AgentEngine

    res = await AgentEngine.process_user_query(
        db=db,
        agent_id=agent_id,
        message=payload.message,
        conversation_id=payload.conversation_id,
        session_id=payload.session_id,
    )
    return AgentChatResponse(
        response=res.response,
        conversation_id=res.conversation_id,
        agent_id=res.agent_id,
        language=res.language,
        latency_ms=res.latency_ms,
    )


@router.post("/{agent_id}/test")
async def test_agent(
    agent_id: str,
    payload: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Module 31 — 'Test Agent' text-chat sandbox."""
    from app.services.ai.agent_engine import AgentEngine

    msg = payload.get("message", "")
    res = await AgentEngine.process_user_query(
        db=db,
        agent_id=agent_id,
        message=msg,
    )
    return {"reply": res.response}


@router.post("/{agent_id}/voice-preview")
async def voice_preview(
    agent_id: str,
    payload: VoicePreviewRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Module 31 — 'Test Agent' voice demo. Streams back an mp3 generated
    with this agent's ElevenLabs voice settings so you can hear it before
    it ever calls a real lead."""
    from app.services.tts_service import synthesize_preview

    agent = await db.get(AIAgent, agent_id)
    if not agent:
        agent = AIAgent(
            id=agent_id,
            name="Preview Agent",
            business_id=user.business_id or "default",
            voice_provider=payload.voice_provider or "elevenlabs",
            voice_id=payload.voice_id or "21m00Tcm4TlvDq8ikWAM",
            voice_stability=0.5,
            voice_similarity_boost=0.75,
            voice_model_id=payload.voice_model_id or "eleven_multilingual_v2",
        )

    # Apply user overrides
    if payload.voice_config_id:
        agent.voice_config_id = payload.voice_config_id
    if payload.voice_provider:
        agent.voice_provider = payload.voice_provider
    if payload.voice_id and payload.voice_id.strip():
        agent.voice_id = payload.voice_id.strip()
    if payload.voice_model_id and payload.voice_model_id.strip():
        agent.voice_model_id = payload.voice_model_id.strip()
    elif payload.gender and (not agent.voice_id or not agent.voice_id.strip()):
        if payload.gender.lower() == "male":
            agent.voice_id = "nPczCjzI2devNBz1zQrb"  # Brian
        else:
            agent.voice_id = "EXAVITQu4vr4xnSDxMaL"  # Sarah

    try:
        audio_bytes = await synthesize_preview(
            payload.text,
            agent=agent,
            lang=payload.lang,
            business_id=user.business_id,
            db=db,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    media_type = "audio/wav" if audio_bytes.startswith(b"RIFF") else "audio/mpeg"
    return Response(content=audio_bytes, media_type=media_type)
