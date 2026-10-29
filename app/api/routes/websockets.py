from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import List, Dict, Any
import json
import asyncio
from app.core.security import decode_access_token

router = APIRouter(prefix="/api/ws", tags=["websockets"])

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[Dict[str, Any]] = []

    async def connect(self, websocket: WebSocket, business_id: str = "default", user_id: str = "default"):
        await websocket.accept()
        self.active_connections.append({
            "websocket": websocket,
            "business_id": business_id,
            "user_id": user_id,
        })

    def disconnect(self, websocket: WebSocket):
        self.active_connections = [
            c for c in self.active_connections if c["websocket"] != websocket
        ]

    async def send_to_user(self, user_id: str, message: Any):
        payload = json.dumps(message) if isinstance(message, dict) else str(message)
        dead = []
        for conn in list(self.active_connections):
            if conn["user_id"] == user_id:
                try:
                    await conn["websocket"].send_text(payload)
                except Exception:
                    dead.append(conn)
        if dead:
            self.active_connections = [c for c in self.active_connections if c not in dead]

    async def broadcast_to_business(self, business_id: str, message: Any):
        payload = json.dumps(message) if isinstance(message, dict) else str(message)
        dead = []
        for conn in list(self.active_connections):
            if conn["business_id"] == business_id or conn["business_id"] == "global" or business_id == "global":
                try:
                    await conn["websocket"].send_text(payload)
                except Exception:
                    dead.append(conn)
        if dead:
            self.active_connections = [c for c in self.active_connections if c not in dead]

    async def broadcast(self, message: Any):
        payload = json.dumps(message) if isinstance(message, dict) else str(message)
        dead = []
        for conn in list(self.active_connections):
            try:
                await conn["websocket"].send_text(payload)
            except Exception:
                dead.append(conn)
        if dead:
            self.active_connections = [c for c in self.active_connections if c not in dead]

    async def broadcast_json(self, message: Any):
        await self.broadcast(message)

    async def broadcast_to_user(self, user_id: str, message: Any):
        await self.send_to_user(user_id, message)

manager = ConnectionManager()

# Global manager instance accessible by other services to broadcast live calls
global_ws_manager = manager

@router.websocket("/live-calls")
@router.websocket("/call-events")
async def websocket_endpoint(websocket: WebSocket):
    from loguru import logger
    token = websocket.query_params.get("token")
    business_id = websocket.query_params.get("business_id", "default")
    user_id = "default"

    if token:
        try:
            payload = decode_access_token(token)
            user_id = payload.get("sub", "default")
            biz_val = payload.get("business_id") or payload.get("tenant_id")
            if biz_val and str(biz_val) not in ("None", "", "null"):
                business_id = str(biz_val)
        except Exception as te:
            logger.warning(f"[WS] ERROR token_decode_failed={te}")

    logger.info(f"[WS] CONNECT client={websocket.client} user_id={user_id} business_id={business_id}")
    try:
        await manager.connect(websocket, business_id=business_id, user_id=user_id)
        try:
            # Initial greeting confirmation to keep browser connection state synchronized
            await websocket.send_text(json.dumps({
                "event": "CONNECTED",
                "status": "connected",
                "business_id": business_id,
                "user_id": user_id,
            }))
        except (WebSocketDisconnect, RuntimeError, ConnectionResetError) as init_disc:
            logger.info(f"[WS] Client disconnected during initial handshake user_id={user_id}: {init_disc}")
            return
        except Exception as init_err:
            logger.warning(f"[WS] ERROR send_initial_connected_failed={init_err}")
            return

        while True:
            try:
                # 25-second wait before sending server keepalive ping to prevent proxy/browser timeout
                data = await asyncio.wait_for(websocket.receive_text(), timeout=25.0)
                logger.info(f"[WS] MESSAGE data_len={len(data)} user_id={user_id}")
                try:
                    await websocket.send_text(json.dumps({"status": "heartbeat", "business_id": business_id}))
                    logger.info(f"[WS] PONG user_id={user_id}")
                except Exception as hb_err:
                    logger.warning(f"[WS] ERROR heartbeat_send_failed={hb_err}")
                    break
            except asyncio.TimeoutError:
                # Periodic keepalive heartbeat to prevent idle connection reset
                try:
                    logger.info(f"[WS] PING user_id={user_id}")
                    await websocket.send_text(json.dumps({"event": "PING", "status": "heartbeat"}))
                except Exception as ping_err:
                    logger.warning(f"[WS] ERROR ping_send_failed={ping_err}")
                    break
    except WebSocketDisconnect as wsd:
        logger.info(f"[WS] CLOSE close_code={getattr(wsd, 'code', 'unknown')} close_reason=\"{getattr(wsd, 'reason', '')}\" user_id={user_id} business_id={business_id}")
    except ConnectionResetError as cre:
        logger.warning(f"[WS] ERROR connection_reset={cre} user_id={user_id}")
    except Exception as general_err:
        logger.warning(f"[WS] ERROR websocket_loop_failed={general_err}")
    finally:
        logger.info(f"[WS] DISCONNECT user_id={user_id} business_id={business_id}")
        manager.disconnect(websocket)


async def dispatch_telephony_event(
    event: str,
    call_id: str,
    tenant_id: str,
    user_id: str | None = None,
    agent_id: str | None = None,
    status: str | None = None,
    extra_data: Dict[str, Any] | None = None,
):
    """
    Standardized real-time event dispatcher for SIP/WebRTC telephony calls.
    Ensures:
      - Required fields: event, call_id, tenant_id, user_id, agent_id, timestamp, status
      - Strict tenant isolation: dispatches only to sockets belonging to the target tenant/business_id
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    payload = {
        "event": event,
        "call_id": call_id,
        "tenant_id": tenant_id,
        "business_id": tenant_id,
        "user_id": user_id or "",
        "agent_id": agent_id or "",
        "timestamp": now_iso,
        "status": status or event,
    }
    if extra_data:
        payload.update(extra_data)

    await global_ws_manager.broadcast_to_business(tenant_id, payload)
    if user_id:
        await global_ws_manager.send_to_user(user_id, payload)

