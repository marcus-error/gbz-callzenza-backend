from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_business_id
from app.db.database import get_db
from app.db.models import AIAgent, Conversation, Lead, Message
from app.schemas.conversation import (
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageOut,
    SendMessageResult,
)
from app.services.lead_engagement_service import process_incoming_message

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(
    payload: ConversationCreate,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    lead = await db.get(Lead, payload.lead_id)
    if not lead or lead.business_id != business_id:
        raise HTTPException(status_code=404, detail="Lead not found")

    conversation = Conversation(
        business_id=business_id,
        lead_id=payload.lead_id,
        agent_id=payload.agent_id,
        channel=payload.channel,
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    lead_id: str | None = None,
) -> list[Conversation]:
    stmt = select(Conversation).where(Conversation.business_id == business_id)
    if lead_id:
        stmt = stmt.where(Conversation.lead_id == lead_id)
    stmt = stmt.order_by(Conversation.created_at.desc())
    result = await db.scalars(stmt)
    return list(result)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: str,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    conversation = await db.get(Conversation, conversation_id)
    if not conversation or conversation.business_id != business_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    result = await db.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.timestamp.asc())
    )
    return list(result)


@router.post("/{conversation_id}/messages", response_model=SendMessageResult, status_code=201)
async def send_message(
    conversation_id: str,
    payload: MessageCreate,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> SendMessageResult:
    """Drives the full AI engagement flow (module 11/13): stores the lead's
    message, runs the AI agent, scores qualification, and triggers human
    handoff automatically when the lead crosses the QUALIFIED threshold."""
    conversation = await db.get(Conversation, conversation_id)
    if not conversation or conversation.business_id != business_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    lead = await db.get(Lead, conversation.lead_id)
    agent = await db.get(AIAgent, conversation.agent_id) if conversation.agent_id else None

    result = await process_incoming_message(db, lead, conversation, agent, payload.content)
    await db.commit()

    return SendMessageResult(
        lead_message=result["lead_message"],
        ai_message=result["ai_message"],
        lead={"id": lead.id, "score": lead.score, "qualification_status": lead.qualification_status, "status": lead.status},
        handoff_triggered=result["handoff_triggered"],
    )
