import asyncio
import json
from datetime import datetime, timezone
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import get_settings
from app.db.models import Call

settings = get_settings()

async def start_customer_hold(db: AsyncSession, call: Call) -> bool:
    """
    Places an active call on hold by redirecting Twilio (if present) to the hold music TwiML,
    and unconditionally broadcasting CALL_ON_HOLD to all active dashboards and participants.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if call and call.status not in ("ON_HOLD", "TRANSFER_REQUESTED"):
        call.status = "TRANSFER_REQUESTED"
        db.add(call)

    # 1. If valid live Twilio call, update call leg to play hold music
    if (
        call.call_sid
        and not call.call_sid.startswith("MOCK")
        and settings.twilio_account_sid
        and settings.twilio_auth_token
    ):
        hold_url = f"{settings.public_base_url}/api/voice/hold-music"
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        def _update():
            try:
                client.calls(call.call_sid).update(url=hold_url, method="GET")
                return True
            except Exception as e:
                logger.error(f"Failed to place call {call.id} on hold: {e}")
                return False

        await asyncio.to_thread(_update)

    # 2. Unconditionally broadcast CALL_ON_HOLD to all listening dashboards
    from app.api.routes.websockets import global_ws_manager
    hold_url = getattr(
        settings,
        "hold_music_url",
        "https://s3.amazonaws.com/com.twilio.music.classical/MARKOVICHAMP-Borghestral.mp3",
    )
    payload = {
        "event": "CALL_ON_HOLD",
        "type": "CALL_ON_HOLD",
        "call_id": call.id,
        "call_session_id": call.id,
        "status": "ON_HOLD",
        "hold_music_url": hold_url,
        "message": "Customer placed on hold.",
        "timestamp": now_iso,
    }
    try:
        await global_ws_manager.broadcast(payload)
        if getattr(call, "business_id", None):
            await global_ws_manager.broadcast_to_business(call.business_id, payload)
    except Exception as ws_err:
        logger.debug(f"[HoldMusicService] Broadcast notice: {ws_err}")

    return True


async def stop_customer_hold(db: AsyncSession, call: Call, target_agent_id: str | None = None) -> bool:
    """
    Removes a call from the hold loop immediately when Agent 2 answers.
    Unconditionally stops hold music, removes customer hold state,
    and notifies all connected dashboards (Agent 1, Agent 2, Supervisor).
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Update call record status from hold to HUMAN_HANDLING if not already a 3-way conference
    is_conf_3way = bool(call and call.status == "CONFERENCE_3WAY_ACTIVE")
    if call and call.status in ("ON_HOLD", "TRANSFER_REQUESTED"):
        call.status = "CONFERENCE_3WAY_ACTIVE" if is_conf_3way else "HUMAN_HANDLING"
        if target_agent_id and not is_conf_3way:
            call.agent_id = target_agent_id
        db.add(call)

    # 2. Twilio carrier bridging/unhold if live Twilio session
    if (
        call.call_sid
        and not call.call_sid.startswith("MOCK")
        and settings.twilio_account_sid
        and settings.twilio_auth_token
    ):
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        def _update():
            try:
                if getattr(call, "conference_sid", None):
                    conf_url = f"{settings.public_base_url}/api/voice/join-conference?conf_sid={call.conference_sid}"
                    client.calls(call.call_sid).update(url=conf_url, method="GET")
                else:
                    resume_url = f"{settings.public_base_url}/api/voice/resume?call_id={call.id}"
                    client.calls(call.call_sid).update(url=resume_url, method="GET")
                return True
            except Exception as e:
                logger.error(f"Failed to remove call {call.id} from hold: {e}")
                return False

        await asyncio.to_thread(_update)

    # 3. UNCONDITIONALLY broadcast HOLD_MUSIC_STOPPED and CALL_CONNECTED to all dashboards
    from app.api.routes.websockets import global_ws_manager
    resolved_status = "CONFERENCE_3WAY_ACTIVE" if is_conf_3way else "HUMAN_HANDLING"
    hold_stopped_payload = {
        "event": "HOLD_MUSIC_STOPPED",
        "type": "HOLD_MUSIC_STOPPED",
        "call_id": call.id,
        "call_session_id": call.id,
        "status": resolved_status,
        "is_3way": is_conf_3way,
        "target_agent_id": target_agent_id or getattr(call, "agent_id", None),
        "timestamp": now_iso,
    }
    call_connected_payload = {
        "event": "CALL_CONNECTED",
        "type": "CALL_CONNECTED",
        "call_id": call.id,
        "call_session_id": call.id,
        "status": resolved_status,
        "is_3way": is_conf_3way,
        "agent_id": target_agent_id or getattr(call, "agent_id", None),
        "agent_type": "HUMAN",
        "timestamp": now_iso,
    }

    try:
        await global_ws_manager.broadcast(hold_stopped_payload)
        await global_ws_manager.broadcast(call_connected_payload)
        if getattr(call, "business_id", None):
            await global_ws_manager.broadcast_to_business(call.business_id, hold_stopped_payload)
            await global_ws_manager.broadcast_to_business(call.business_id, call_connected_payload)
    except Exception as ws_err:
        logger.debug(f"[HoldMusicService] Stop hold broadcast notice: {ws_err}")

    # 4. Flush stale RTP queues so Voice Agent 2 and Customer hear each other with zero latency
    try:
        from app.api.routes.barge_stream import flush_barge_session_queues_for_transfer
        flush_barge_session_queues_for_transfer(call.id)
        if call.call_sid:
            flush_barge_session_queues_for_transfer(call.call_sid)
    except Exception as q_err:
        logger.debug(f"[HoldMusicService] Queue flush note: {q_err}")

    return True
