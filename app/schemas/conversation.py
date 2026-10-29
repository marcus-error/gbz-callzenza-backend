from datetime import datetime
from typing import Any
from pydantic import BaseModel


class ConversationCreate(BaseModel):
    lead_id: str
    agent_id: str | None = None
    channel: str = "LIVE_CHAT"


class ConversationOut(BaseModel):
    id: str
    business_id: str
    lead_id: str
    agent_id: str | None
    channel: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessageCreate(BaseModel):
    content: str
    message_type: str = "TEXT"


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    sender_type: str
    sender_id: str
    message_type: str
    content: str
    meta: dict[str, Any]
    timestamp: datetime

    model_config = {"from_attributes": True}


class SendMessageResult(BaseModel):
    lead_message: MessageOut
    ai_message: MessageOut | None
    lead: dict[str, Any]
    handoff_triggered: bool
