from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User
from app.schemas.call_transfer import (
    IntentCheckRequest,
    IntentCheckResponse,
    CallTransferInitiateRequest,
    CallTransferResponse,
    CallTransferAcceptRequest,
    CallTransferDeclineRequest,
    AgentPresenceUpdateRequest,
    AgentPresenceItem,
    AgentPresenceListResponse,
    TransferStatusDetailResponse,
)
from app.services.call_transfer_service import CallTransferService
from app.services.agent_availability_service import AgentAvailabilityService
from app.services.human_handoff_intent import HumanHandoffIntentDetector
from app.repositories.call_transfer_repository import CallTransferRepository

router = APIRouter(prefix="/api/v1/call-transfer", tags=["AI Agent Call Transfer"])
api_router = APIRouter(prefix="/api/call-transfer", tags=["AI Agent Call Transfer"])


@router.post("/intent", response_model=IntentCheckResponse)
@api_router.post("/intent", response_model=IntentCheckResponse)
async def check_transfer_intent(
    body: IntentCheckRequest,
):
    """Classifies customer message for handoff intent across English, Tamil, Tanglish, and Hindi."""
    res = HumanHandoffIntentDetector.detect(body.text)
    return IntentCheckResponse(
        has_intent=res.has_intent,
        confidence=res.confidence,
        language=res.language,
        matched_phrase=res.matched_phrase,
        requires_confirmation=res.requires_confirmation,
        reason=res.reason,
    )


@router.post("/initiate", response_model=CallTransferResponse)
@api_router.post("/initiate", response_model=CallTransferResponse)
async def initiate_call_transfer(
    body: CallTransferInitiateRequest,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Initiates warm transfer from AI agent to an available voice agent."""
    res = await CallTransferService.initiate_warm_transfer(
        call_id=body.call_id,
        business_id=current_user.business_id or "default",
        session_id=body.session_id,
        campaign_id=body.campaign_id,
        lead_id=body.lead_id,
        customer_name=body.customer_name or "Customer",
        customer_phone=body.customer_phone or "",
        language=body.language or "en",
        notes=body.notes or "",
        transcript=body.transcript or "",
        target_agent_id=body.target_agent_id,
        db=db,
    )
    return CallTransferResponse(
        transfer_id=res["transfer_id"],
        call_id=res["call_id"],
        status=res["status"],
        assigned_agent_id=res.get("assigned_agent_id"),
        assigned_agent_name=res.get("assigned_agent_name"),
        message=res["message"],
        is_fallback=res.get("is_fallback", False),
    )


@router.post("/{transfer_id}/accept")
@api_router.post("/{transfer_id}/accept")
async def accept_transfer(
    transfer_id: str,
    body: Optional[CallTransferAcceptRequest] = None,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Voice agent accepts incoming transfer request."""
    agent_id = body.agent_id if (body and body.agent_id) else current_user.id
    try:
        res = await CallTransferService.accept_warm_transfer(
            transfer_id=transfer_id,
            agent_id=agent_id,
            db=db,
            notes=body.notes if body else None,
        )
        return res
    except ValueError as val_err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(val_err))


@router.post("/{transfer_id}/decline")
@api_router.post("/{transfer_id}/decline")
async def decline_transfer(
    transfer_id: str,
    body: Optional[CallTransferDeclineRequest] = None,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Voice agent declines incoming transfer request."""
    agent_id = body.agent_id if (body and body.agent_id) else current_user.id
    reason = body.reason if (body and body.reason) else "Agent declined"
    try:
        res = await CallTransferService.decline_warm_transfer(
            transfer_id=transfer_id,
            agent_id=agent_id,
            reason=reason,
            db=db,
        )
        return res
    except ValueError as val_err:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(val_err))


@router.post("/{transfer_id}/timeout")
@api_router.post("/{transfer_id}/timeout")
async def timeout_transfer(
    transfer_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Triggered on 20s ring expiry without acceptance."""
    res = await CallTransferService.timeout_warm_transfer(transfer_id=transfer_id, db=db)
    return res


@router.post("/{transfer_id}/complete")
@api_router.post("/{transfer_id}/complete")
async def complete_transfer(
    transfer_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Marks transfer completed and restores voice agent to AVAILABLE."""
    res = await CallTransferService.complete_warm_transfer(transfer_id=transfer_id, db=db)
    return res


@router.get("/{transfer_id}", response_model=TransferStatusDetailResponse)
@api_router.get("/{transfer_id}", response_model=TransferStatusDetailResponse)
async def get_transfer_status(
    transfer_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Retrieves full transfer state."""
    rec = await CallTransferRepository.get_transfer_session(transfer_id, db)
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer session not found")
    return TransferStatusDetailResponse(
        transfer_id=rec.id,
        call_id=rec.call_id,
        status=rec.status,
        customer_name=rec.customer_name or "Customer",
        customer_phone=rec.customer_phone or "",
        assigned_voice_agent_id=rec.assigned_voice_agent_id,
        assigned_voice_agent_name=rec.assigned_voice_agent_name,
        intent_detected_at=rec.intent_detected_at.isoformat() if rec.intent_detected_at else None,
        ringing_at=rec.ringing_at.isoformat() if rec.ringing_at else None,
        accepted_at=rec.accepted_at.isoformat() if rec.accepted_at else None,
        ai_disconnected_at=rec.ai_disconnected_at.isoformat() if rec.ai_disconnected_at else None,
        completed_at=rec.completed_at.isoformat() if rec.completed_at else None,
        failure_reason=rec.failure_reason,
        transcript_snapshot=rec.transcript_snapshot,
    )


@router.get("/presence/list", response_model=AgentPresenceListResponse)
@api_router.get("/presence/list", response_model=AgentPresenceListResponse)
async def list_voice_agents_presence(
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns real-time presence of voice agents."""
    res = await AgentAvailabilityService.list_agents_presence(current_user.business_id, db)
    items = [
        AgentPresenceItem(
            agent_id=a["agent_id"],
            agent_name=a["agent_name"],
            agent_email=a.get("agent_email"),
            status=a["status"],
            is_available=a["is_available"],
            skills=a.get("skills", []),
            current_call_id=a.get("current_call_id"),
            last_status_change=a.get("last_status_change"),
        )
        for a in res["agents"]
    ]
    return AgentPresenceListResponse(
        agents=items,
        total_available=res["total_available"],
        total_agents=res["total_agents"],
    )


@router.post("/presence/update")
@api_router.post("/presence/update")
async def update_voice_agent_presence(
    body: AgentPresenceUpdateRequest,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Updates voice agent presence (AVAILABLE, BREAK, OFFLINE, etc.)."""
    res = await AgentAvailabilityService.set_presence(
        agent_id=body.agent_id,
        status=body.status,
        business_id=current_user.business_id or "default",
        db=db,
    )
    return res
