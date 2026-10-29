"""
CallZenza — Multi-Agent Conference Calling API Routes
Handles inviting, listing, and removing Voice Agents in real-time multi-party conference calls.
"""
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User, Call
from app.services.conference_service import (
    list_conference_participants,
    invite_voice_agent_to_conference,
    accept_conference_invite,
    reject_conference_invite,
    remove_conference_participant,
    get_or_create_conference_room,
)

router = APIRouter(prefix="/api/conference", tags=["Multi-Agent Conference"])


class InviteAgentRequest(BaseModel):
    call_id: Optional[str] = None
    agent_id: Optional[str] = None
    target_agent_id: Optional[str] = None
    transfer_type: Optional[str] = "conference"
    notes: Optional[str] = None

    @property
    def effective_agent_id(self) -> str:
        return self.target_agent_id or self.agent_id or ""


class RemoveParticipantRequest(BaseModel):
    call_id: Optional[str] = None
    participant_id: str


class RejectInviteRequest(BaseModel):
    call_id: Optional[str] = None
    reason: Optional[str] = "Declined by agent"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 1. List Current Conference Participants
# ---------------------------------------------------------------------------
@router.get("/participants")
@router.get("/participants/")
@router.get("/{call_id}/participants")
@router.get("/{call_id}/participants/")
async def get_conference_participants_endpoint(
    call_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    if not call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_id must be provided in URL or query parameter."
        )

    try:
        call, conf_sid = await get_or_create_conference_room(call_id, db, current_user=user)

        if user.role != "SUPER_ADMIN" and call.business_id and user.business_id:
            if call.business_id != user.business_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Call '{call_id}' not found in your organization."
                )

        data = await list_conference_participants(call.id, db)
        return {"success": True, **data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CONFERENCE_API_ERROR] Error fetching participants for call {call_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch conference participants: {str(e)}"
        )


# ---------------------------------------------------------------------------
# 2. Invite / Inspect Voice Agent Conference Invitation
# ---------------------------------------------------------------------------
@router.get("/invite-agent")
@router.get("/invite-agent/")
@router.get("/{call_id}/invite-agent")
@router.get("/{call_id}/invite-agent/")
@router.get("/{call_id}/add-agent")
@router.get("/{call_id}/add-agent/")
async def get_invite_agent_status_endpoint(
    call_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns current conference state and eligibility to invite another Voice Agent.
    Prevents 404/405 errors if client probes or navigates via GET.
    """
    if not call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_id must be provided in URL path or query parameter."
        )

    try:
        call, conf_sid = await get_or_create_conference_room(call_id, db, current_user=user)
        if user.role != "SUPER_ADMIN" and call.business_id and user.business_id:
            if call.business_id != user.business_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Call '{call_id}' not found in your organization."
                )

        data = await list_conference_participants(call.id, db)
        return {
            "success": True,
            "call_id": call.id,
            "conference_sid": conf_sid,
            "status": call.status,
            "capacity": data["capacity"],
            "active_participants_count": data["active_participants_count"],
            "connected_count": data["connected_count"],
            "is_full": data["is_full"],
            "can_invite": not data["is_full"],
            "participants": data["participants"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CONFERENCE_API_ERROR] Error fetching invite status for call {call_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch conference invite status: {str(e)}"
        )


@router.post("/invite-agent")
@router.post("/invite-agent/")
@router.post("/{call_id}/invite-agent")
@router.post("/{call_id}/invite-agent/")
@router.post("/{call_id}/add-agent")
@router.post("/{call_id}/add-agent/")
async def invite_agent_endpoint(
    payload: Optional[InviteAgentRequest] = None,
    call_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Invites another Voice Agent into the ongoing call conference.
    - Customer and Host Agent 1 remain connected.
    - Enforces capacity limit (max 5 participants).
    - Prevents duplicate invitations.
    """
    req_payload = payload or InviteAgentRequest()
    effective_call_id = call_id or req_payload.call_id
    if not effective_call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_id must be provided in URL or request body."
        )

    target_id = req_payload.effective_agent_id
    if not target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_agent_id or agent_id must be provided."
        )

    try:
        res = await invite_voice_agent_to_conference(
            call_id=effective_call_id,
            target_agent_id=target_id,
            inviter=user,
            db=db,
            notes=req_payload.notes,
        )
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CONFERENCE_API_ERROR] Error inviting agent to call {effective_call_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to invite Voice Agent to conference: {str(e)}"
        )


# ---------------------------------------------------------------------------
# 3. Accept Conference Invitation
# ---------------------------------------------------------------------------
@router.post("/accept")
@router.post("/accept/")
@router.post("/{call_id}/accept")
@router.post("/{call_id}/accept/")
async def accept_conference_invite_endpoint(
    call_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Called when an invited Voice Agent accepts and joins the conference."""
    effective_call_id = call_id or (payload.get("call_id") if payload else None)
    if not effective_call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_id must be provided."
        )

    try:
        res = await accept_conference_invite(call_id=effective_call_id, agent_user=user, db=db)
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CONFERENCE_API_ERROR] Error accepting conference invite for call {effective_call_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to join conference: {str(e)}"
        )


# ---------------------------------------------------------------------------
# 4. Reject Conference Invitation
# ---------------------------------------------------------------------------
@router.post("/reject")
@router.post("/reject/")
@router.post("/{call_id}/reject")
@router.post("/{call_id}/reject/")
async def reject_conference_invite_endpoint(
    payload: Optional[RejectInviteRequest] = None,
    call_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Called when an invited Voice Agent declines or times out."""
    effective_call_id = call_id or (payload.call_id if payload else None)
    if not effective_call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_id must be provided."
        )

    reason = (payload.reason if payload else None) or "Declined by agent"
    try:
        res = await reject_conference_invite(
            call_id=effective_call_id,
            agent_user=user,
            db=db,
            reason=reason
        )
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CONFERENCE_API_ERROR] Error rejecting conference invite for call {effective_call_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to decline conference invite: {str(e)}"
        )


# ---------------------------------------------------------------------------
# 5. Remove a Participant from the Conference
# ---------------------------------------------------------------------------
@router.post("/remove-participant")
@router.post("/remove-participant/")
@router.post("/{call_id}/remove-participant")
@router.post("/{call_id}/remove-participant/")
async def remove_participant_endpoint(
    payload: RemoveParticipantRequest,
    call_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Removes an eligible participant (e.g. secondary Voice Agent) from the conference."""
    effective_call_id = call_id or payload.call_id
    if not effective_call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_id must be provided."
        )

    try:
        res = await remove_conference_participant(
            call_id=effective_call_id,
            participant_id=payload.participant_id,
            requester=user,
            db=db,
        )
        return res
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CONFERENCE_API_ERROR] Error removing participant from call {effective_call_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove participant: {str(e)}"
        )


# ---------------------------------------------------------------------------
# 6. Conference Room Status & Overview
# ---------------------------------------------------------------------------
@router.get("/status")
@router.get("/status/")
@router.get("/{call_id}/status")
@router.get("/{call_id}/status/")
async def get_conference_status_endpoint(
    call_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns general conference room health, room SID, and active participant counts."""
    if not call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_id must be provided."
        )

    call, conf_sid = await get_or_create_conference_room(call_id, db, current_user=user)
    data = await list_conference_participants(call.id, db)
    return {
        "success": True,
        "call_id": call.id,
        "conference_sid": conf_sid,
        "status": call.status,
        "is_active": call.status in ["CONFERENCE_3WAY_ACTIVE", "CLIENT_ADDING", "IN_PROGRESS", "HUMAN_HANDLING"],
        "active_participants_count": data["active_participants_count"],
        "capacity": data["capacity"],
        "is_full": data["is_full"],
    }

