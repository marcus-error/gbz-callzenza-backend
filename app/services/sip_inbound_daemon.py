"""
CallZenza SIP Inbound Listener Daemon.
Listens on a dedicated UDP port (default 5062), registers with the Asterisk PBX trunk,
handles incoming SIP INVITE calls in real-time, responds with 180 Ringing,
records the Call in the database, and streams the INCOMING_CALL event over WebSockets
so the CallZenza browser popup rings and allows attending/declining.
When Attended, sends 200 OK with SDP and initiates bidirectional RTP audio bridging.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from sqlalchemy import select, or_

from app.core.config import get_settings
from app.core.deps import PRIMARY_BUSINESS_ID           
from app.services.voice_service import ALAW_TO_ULAW_TABLE

settings = get_settings()

DAEMON_SOCK: Optional[socket.socket] = None


def _sip_send(data: str, host: str, port: int) -> None:
    """Send a SIP packet over UDP using the bound DAEMON_SOCK (port 5062) so NAT rport matches."""
    global DAEMON_SOCK
    clean_host = (host or "").strip()
    if clean_host.startswith("sip:"):
        clean_host = clean_host[4:].strip()
    if ":" in clean_host:
        h_parts = clean_host.split(":", 1)
        clean_host = h_parts[0].strip()
        try:
            port = int(h_parts[1].strip())
        except Exception:
            pass
    try:
        if DAEMON_SOCK:
            DAEMON_SOCK.sendto(data.encode("utf-8"), (clean_host, port))
            return

        listen_port = _get_listen_port()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", listen_port))
        except Exception:
            pass
        s.settimeout(2)
        s.sendto(data.encode("utf-8"), (host, port))
        s.close()
    except Exception as exc:
        raise exc  # Re-raise so callers can log contextually


# Active incoming SIP transactions: call_id -> dict
ACTIVE_SIP_CALLS: Dict[str, Dict[str, Any]] = {}
DAEMON_RUNNING = False
DAEMON_TASK: Optional[asyncio.Task] = None
DAEMON_SOCK: Optional[socket.socket] = None
IS_REGISTERED = False
CURRENT_REGISTERED_HOST: str = ""
CURRENT_REGISTERED_PORT: int = 5060
CURRENT_REGISTERED_USER: str = ""

DAEMON_ACCOUNT_REGISTRATIONS: Dict[str, Dict[str, Any]] = {}

DAEMON_REGISTRATION_INFO: Dict[str, Any] = {
    "is_registered": False,
    "registered_host": "",
    "registered_port": 5060,
    "registered_user": "",
    "status": "unregistered",
    "last_error": None,
    "error_code": None,
    "sip_status_code": None,
    "suggested_action": None,
    "last_checked_at": None,
    "account_id": None,
    "attempt_id": None,
}


def get_daemon_registration_info(account_id: Optional[str] = None) -> Dict[str, Any]:
    global DAEMON_REGISTRATION_INFO, DAEMON_ACCOUNT_REGISTRATIONS
    if account_id:
        if account_id in DAEMON_ACCOUNT_REGISTRATIONS:
            return dict(DAEMON_ACCOUNT_REGISTRATIONS[account_id])
        return {
            "is_registered": False,
            "registered_host": "",
            "registered_port": 5060,
            "registered_user": "",
            "status": "unregistered",
            "last_error": None,
            "error_code": None,
            "sip_status_code": None,
            "suggested_action": None,
            "last_checked_at": None,
            "account_id": account_id,
            "attempt_id": None,
        }
    return dict(DAEMON_REGISTRATION_INFO)


def _record_daemon_registration_event(
    account_id: Optional[str],
    organization_id: Optional[str],
    host: str,
    port: int,
    user: str,
    status: str,
    is_registered: bool,
    error_message: Optional[str] = None,
    error_code: Optional[str] = None,
    sip_status_code: Optional[int] = None,
    suggested_action: Optional[str] = None,
    attempt_id: Optional[str] = None,
) -> None:
    """Updates isolated per-account and global daemon registration state with structured redacted logging."""
    global DAEMON_REGISTRATION_INFO, DAEMON_ACCOUNT_REGISTRATIONS
    now_iso = datetime.now(timezone.utc).isoformat()
    record = {
        "is_registered": is_registered,
        "registered_host": host,
        "registered_port": port,
        "registered_user": user,
        "status": status,
        "last_error": error_message,
        "error_code": error_code,
        "sip_status_code": sip_status_code,
        "suggested_action": suggested_action,
        "last_checked_at": now_iso,
        "account_id": account_id,
        "organization_id": organization_id,
        "attempt_id": attempt_id,
    }
    DAEMON_REGISTRATION_INFO.update(record)
    if account_id:
        DAEMON_ACCOUNT_REGISTRATIONS[account_id] = record
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(_update_account_registration_in_db(
                    account_id=account_id,
                    status=status,
                    error_message=error_message,
                    error_code=error_code,
                    sip_status_code=sip_status_code,
                    suggested_action=suggested_action,
                    attempt_id=attempt_id,
                ))
        except RuntimeError:
            pass

    masked_u = user if len(user) <= 3 else user[:2] + "***" + user[-1:]
    logger.info(
        f"[SIP_REGISTER_EVENT] account_id={account_id} org_id={organization_id} "
        f"registrar={host}:{port} user={masked_u} attempt_id={attempt_id} "
        f"sip_status={sip_status_code} state={status}"
    )


async def _update_account_registration_in_db(
    account_id: str,
    status: str,
    error_message: Optional[str] = None,
    error_code: Optional[str] = None,
    sip_status_code: Optional[int] = None,
    suggested_action: Optional[str] = None,
    attempt_id: Optional[str] = None,
) -> None:
    try:
        from app.db.database import AsyncSessionLocal
        from app.db.models import SIPAccount
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            acc = await db.get(SIPAccount, account_id)
            if acc:
                acc.registration_status = status
                acc.last_error_message = error_message
                acc.last_health_status = "healthy" if status == "registered" else ("unreachable" if status in ("network_error", "dns_error") else "degraded")
                acc.last_health_checked_at = now
                current_settings = dict(acc.settings or {})
                current_settings["registration_diagnostics"] = {
                    "status": status,
                    "error_code": error_code,
                    "message": error_message,
                    "sip_status_code": sip_status_code,
                    "suggested_action": suggested_action,
                    "retryable": status in ("network_error", "unregistered"),
                    "last_checked_at": now.isoformat(),
                    "attempt_id": attempt_id,
                }
                acc.settings = current_settings
                db.add(acc)
                await db.commit()
    except Exception as e:
        logger.warning(f"[SIP_DAEMON] Could not persist registration status to DB: {e}")


import urllib.request

_CACHED_PUBLIC_IP: Optional[str] = None
_CACHED_PUBLIC_IP_TS: float = 0.0


def is_private_or_local_ip(ip: str) -> bool:
    """Returns True if the IP is private, link-local, loopback, or invalid."""
    if not ip:
        return True
    cleaned = ip.strip()
    if cleaned in ("127.0.0.1", "0.0.0.0", "localhost", "auto", ""):
        return True
    parts = cleaned.split(".")
    if len(parts) != 4:
        return True
    try:
        b0, b1 = int(parts[0]), int(parts[1])
        if b0 == 10:  # 10.0.0.0/8
            return True
        if b0 == 172 and (16 <= b1 <= 31):  # 172.16.0.0/12
            return True
        if b0 == 192 and b1 == 168:  # 192.168.0.0/16
            return True
        if b0 == 127:  # 127.0.0.0/8 Loopback
            return True
        if b0 == 169 and b1 == 254:  # 169.254.0.0/16 Link-local
            return True
        if b0 == 0:
            return True
    except (ValueError, IndexError):
        return True
    return False


def _get_local_ip() -> str:
    """Resolves local outbound LAN/server IP."""
    configured = os.getenv("SIP_LOCAL_IP", "").strip()
    if configured and configured not in ("auto", ""):
        return configured
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return configured or "127.0.0.1"


def _get_listen_port() -> int:
    raw = os.getenv("SIP_LISTEN_PORT", "5062").strip()
    try:
        return int(raw)
    except Exception:
        return 5062


def _get_public_ip() -> str:
    """
    Returns the publicly reachable WAN IP for NAT traversal on both local and server.
    Ensures a private LAN address is NEVER advertised to an external Asterisk PBX.
    """
    global _CACHED_PUBLIC_IP, _CACHED_PUBLIC_IP_TS
    now = time.time()
    if _CACHED_PUBLIC_IP and not is_private_or_local_ip(_CACHED_PUBLIC_IP) and (now - _CACHED_PUBLIC_IP_TS < 60.0):
        return _CACHED_PUBLIC_IP

    cfg = (getattr(settings, "sip_rtp_ip", None) or os.getenv("SIP_RTP_IP", "") or os.getenv("SIP_PUBLIC_IP", "")).strip()
    if cfg and cfg != "auto" and not is_private_or_local_ip(cfg):
        _CACHED_PUBLIC_IP = cfg
        _CACHED_PUBLIC_IP_TS = now
        return cfg

    # 1. Try ipify API and fallbacks
    endpoints = [
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://ifconfig.me/ip",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CallZenza/1.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                detected = resp.read().decode("utf-8").strip()
                if detected and not is_private_or_local_ip(detected):
                    _CACHED_PUBLIC_IP = detected
                    _CACHED_PUBLIC_IP_TS = now
                    logger.info(f"[SIP_IP_DISCOVERY] Resolved public WAN IP from {url}: {detected}")
                    return _CACHED_PUBLIC_IP
        except Exception:
            continue

    return _get_local_ip()


def get_stun_mapped_address(
    sock: socket.socket,
    timeout: float = 1.0,
) -> Tuple[Optional[str], Optional[int]]:
    """
    Queries STUN servers using the given pre-bound UDP socket to discover
    its exact NAT-mapped public IP and public UDP port for RTP SDP negotiation.
    """
    servers = [
        ("stun.l.google.com", 19302),
        ("stun1.l.google.com", 19302),
        ("stun2.l.google.com", 19302),
    ]
    magic = 0x2112A442
    tx_id = b"\x12\x34\x56\x78\x9a\xbc\xde\xf0\x11\x22\x33\x44"
    req = struct.pack("!HHI12s", 0x0001, 0, magic, tx_id)
    orig_timeout = sock.gettimeout()
    try:
        sock.settimeout(timeout)
        for host, port in servers:
            try:
                sock.sendto(req, (host, port))
                resp, _ = sock.recvfrom(2048)
                if len(resp) < 20:
                    continue
                idx = 20
                while idx + 4 <= len(resp):
                    attr_type, attr_len = struct.unpack("!HH", resp[idx:idx+4])
                    val = resp[idx+4:idx+4+attr_len]
                    if attr_type == 0x0020 and len(val) >= 8:  # XOR-MAPPED-ADDRESS
                        family = val[1]
                        x_port = struct.unpack("!H", val[2:4])[0]
                        mapped_port = x_port ^ (magic >> 16)
                        if family == 0x01:
                            x_ip = struct.unpack("!I", val[4:8])[0]
                            mapped_ip = socket.inet_ntoa(struct.pack("!I", x_ip ^ magic))
                            return mapped_ip, mapped_port
                    elif attr_type == 0x0001 and len(val) >= 8:  # MAPPED-ADDRESS
                        family = val[1]
                        mapped_port = struct.unpack("!H", val[2:4])[0]
                        if family == 0x01:
                            mapped_ip = socket.inet_ntoa(val[4:8])
                            return mapped_ip, mapped_port
                    idx += 4 + attr_len
            except Exception:
                continue
    finally:
        sock.settimeout(orig_timeout)
    return None, None


def build_register_packet(
    host: str,
    port: int,
    user: str,
    contact_ip: str,
    listen_port: int,
    call_id: str,
    tag: str,
    cseq_num: int = 1,
    auth_header: Optional[str] = None,
) -> str:
    branch = uuid.uuid4().hex[:8]
    headers = [
        f"REGISTER sip:{host}:{port} SIP/2.0",
        f"Via: SIP/2.0/UDP {contact_ip}:{listen_port};branch=z9hG4bK-{branch};rport",
        "Max-Forwards: 70",
        f"From: <sip:{user}@{host}>;tag={tag}",
        f"To: <sip:{user}@{host}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq_num} REGISTER",
        f"Contact: <sip:{user}@{contact_ip}:{listen_port}>",
        "Expires: 300",
        "Allow: INVITE, ACK, CANCEL, OPTIONS, BYE",
    ]
    if auth_header:
        headers.append(f"Authorization: {auth_header}")
    headers.extend(["Content-Length: 0\r\n\r\n"])
    return "\r\n".join(headers)




def compute_digest_auth(
    user: str,
    pwd: str,
    realm: str,
    nonce: str,
    uri: str,
    qop: Optional[str] = None,
    opaque: Optional[str] = None,
    auth_user: Optional[str] = None,
) -> str:
    effective_user = (auth_user or user).strip()
    ha1 = hashlib.md5(f"{effective_user}:{realm}:{pwd}".encode()).hexdigest()
    ha2 = hashlib.md5(f"REGISTER:{uri}".encode()).hexdigest()

    if qop and "auth" in qop:
        cnonce = uuid.uuid4().hex[:8]
        nc = "00000001"
        resp_h = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        auth = (
            f'Digest username="{effective_user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp_h}", algorithm=MD5, cnonce="{cnonce}", qop=auth, nc={nc}'
        )
    else:
        resp_h = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        auth = f'Digest username="{effective_user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp_h}", algorithm=MD5'

    if opaque:
        auth += f', opaque="{opaque}"'
    return auth


def normalize_phone_for_matching(raw_phone: str) -> Tuple[str, str]:
    """
    Returns (normalized_digits, last_10_digits).
    e.g. '+91 9585310712' -> ('919585310712', '9585310712')
    """
    if not raw_phone:
        return ("", "")
    digits = "".join(c for c in str(raw_phone) if c.isdigit())
    digits_clean = digits.lstrip("0")
    last_10 = digits[-10:] if len(digits) >= 10 else digits
    return (digits_clean, last_10)


async def _handle_incoming_invite(sock: socket.socket, data: str, addr: Tuple[str, int]):
    """Processes an incoming SIP INVITE from the Asterisk PBX for Human Voice Agent routing."""
    from app.api.routes.websockets import dispatch_telephony_event, global_ws_manager
    from app.db.database import AsyncSessionLocal
    from app.db.models import Call, User, Expert, Lead, Campaign, Conversation

    lines = data.splitlines()

    via_line = ""
    from_line = ""
    to_line = ""
    call_id_val = ""
    cseq_line = ""

    for l in lines:
        l_lower = l.lower()
        if l_lower.startswith("via:"):
            via_line = l.split(":", 1)[1].strip()
        elif l_lower.startswith("from:"):
            from_line = l.split(":", 1)[1].strip()
        elif l_lower.startswith("to:"):
            to_line = l.split(":", 1)[1].strip()
        elif l_lower.startswith("call-id:"):
            call_id_val = l.split(":", 1)[1].strip()
        elif l_lower.startswith("cseq:"):
            cseq_line = l.split(":", 1)[1].strip()

    # Extract caller number from From header: e.g. <sip:+919876543210@...> or "John" <sip:09876543210@...>
    caller_number = "Unknown"
    caller_name = "Incoming Caller"

    num_m = re.search(r'<sip:([+0-9a-zA-Z_\-]+)@', from_line)
    if num_m:
        caller_number = num_m.group(1)

    name_m = re.search(r'"([^"]+)"', from_line)
    if name_m:
        caller_name = name_m.group(1)
    elif caller_number != "Unknown":
        caller_name = f"Caller {caller_number}"

    # Extract destination extension / DID from To header
    dest_number = "Unknown"
    to_m = re.search(r'<sip:([+0-9a-zA-Z_\-]+)@', to_line)
    if to_m:
        dest_number = to_m.group(1)

    logger.info(f"[INBOUND INVITE RECEIVED] from={caller_number} to={dest_number} call_id={call_id_val} remote_addr={addr}")

    contact_ip = _get_public_ip()
    listen_port = _get_listen_port()
    tag_to = uuid.uuid4().hex[:8]
    to_header_with_tag = f"{to_line};tag={tag_to}" if ";tag=" not in to_line else to_line

    # 1. Immediately send SIP/2.0 100 Trying
    trying_pkt = (
        f"SIP/2.0 100 Trying\r\n"
        f"Via: {via_line}\r\n"
        f"From: {from_line}\r\n"
        f"To: {to_line}\r\n"
        f"Call-ID: {call_id_val}\r\n"
        f"CSeq: {cseq_line}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    try:
        sock.sendto(trying_pkt.encode("utf-8"), addr)
    except Exception:
        pass

    # 2. Immediately send SIP/2.0 180 Ringing so caller hears ringback tone
    ringing_pkt = (
        f"SIP/2.0 180 Ringing\r\n"
        f"Via: {via_line}\r\n"
        f"From: {from_line}\r\n"
        f"To: {to_header_with_tag}\r\n"
        f"Call-ID: {call_id_val}\r\n"
        f"CSeq: {cseq_line}\r\n"
        f"Contact: <sip:{dest_number}@{contact_ip}:{listen_port}>\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    try:
        sock.sendto(ringing_pkt.encode("utf-8"), addr)
    except Exception:
        pass

    # 0. PBX Loopback / Self-Echo / Trunk Ping Filter
    # Check if this incoming INVITE is an automatic loopback or PBX trunk echo from our own DID or trunk
    clean_c = "".join(c for c in str(caller_number) if c.isdigit())
    clean_d = "".join(c for c in str(dest_number) if c.isdigit())
    
    own_numbers = set()
    for raw_n in [
        getattr(settings, "sip_did_number", None),
        getattr(settings, "sip_username", None),
        getattr(settings, "sip_login", None),
        getattr(settings, "sip_account_name", None),
        "09410087540",
        "917935485287",
        "+917935485287",
    ]:
        if raw_n:
            digits = "".join(c for c in str(raw_n) if c.isdigit())
            if digits:
                own_numbers.add(digits)
                if len(digits) >= 10:
                    own_numbers.add(digits[-10:])

    is_self_loopback = False
    if clean_c and clean_d and (clean_c == clean_d or (len(clean_c) >= 10 and len(clean_d) >= 10 and clean_c[-10:] == clean_d[-10:])):
        is_self_loopback = True
    elif clean_c and (clean_c in own_numbers or (len(clean_c) >= 10 and clean_c[-10:] in own_numbers)):
        is_self_loopback = True

    if is_self_loopback:
        logger.info(
            f"[SIP_LOOPBACK_IGNORED] Incoming INVITE is a PBX loopback from own trunk/DID ({caller_number} -> {dest_number}). "
            f"Replying 486 Busy Here to satisfy Asterisk without creating self-inbound popup."
        )
        cseq_num = cseq_line.split()[0] if cseq_line else "1"
        busy_loop_pkt = (
            f"SIP/2.0 486 Busy Here\r\n"
            f"Via: {via_line}\r\n"
            f"From: {from_line}\r\n"
            f"To: {to_header_with_tag}\r\n"
            f"Call-ID: {call_id_val}\r\n"
            f"CSeq: {cseq_num} INVITE\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        try:
            sock.sendto(busy_loop_pkt.encode("utf-8"), addr)
        except Exception:
            pass
        return

    # Check if this incoming INVITE correlates to an active or recently placed OUTBOUND call
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recent_t = now - timedelta(seconds=60)

    existing_outbound = None
    try:
        async with AsyncSessionLocal() as db_check:
            # 1. Match by Call-ID or call_sid
            sid_check = (await db_check.execute(
                select(Call).where(
                    or_(Call.id == call_id_val, Call.call_sid == call_id_val),
                    Call.direction == "OUTBOUND"
                )
            )).scalars().first()
            if sid_check:
                existing_outbound = sid_check
            else:
                # 2. Match active outbound calls created within last 60 seconds by destination / caller phone
                outbound_cands = (await db_check.execute(
                    select(Call).where(
                        Call.direction == "OUTBOUND",
                        Call.created_at >= recent_t,
                        Call.status.in_(["INITIATING", "RINGING", "CONNECTED", "IN_PROGRESS", "HUMAN_HANDLING"])
                    )
                )).scalars().all()
                for oc in outbound_cands:
                    oc_to = "".join(c for c in (oc.to_number or "") if c.isdigit())
                    if (clean_c and len(clean_c) >= 7 and (clean_c in oc_to or oc_to in clean_c)) or \
                       (clean_d and len(clean_d) >= 7 and (clean_d in oc_to or oc_to in clean_d)):
                        existing_outbound = oc
                        break
    except Exception as corr_err:
        logger.debug(f"[SIP_CORRELATION_CHECK] Outbound check note: {corr_err}")

    if existing_outbound:
        logger.info(
            f"[SIP_OUTBOUND_CORRELATION] Correlated incoming INVITE (Call-ID: {call_id_val}, caller: {caller_number}, dest: {dest_number}) "
            f"to existing OUTBOUND call {existing_outbound.id}. Preventing duplicate INBOUND call creation and avoiding self-inbound popup."
        )
        call_info = {
            "sock": sock,
            "addr": addr,
            "via_line": via_line,
            "from_line": from_line,
            "to_line": to_header_with_tag,
            "call_id_val": call_id_val,
            "cseq_line": cseq_line,
            "cseq_num": cseq_line.split()[0] if cseq_line else "1",
            "caller_number": caller_number,
            "dest_number": dest_number,
            "status": "RINGING",
            "invite_data": data,
            "rtp_sock": None,
            "call_active": [True],
            "is_ai_agent": False,
            "db_call_id": existing_outbound.id,
        }
        ACTIVE_SIP_CALLS[existing_outbound.id] = call_info
        ACTIVE_SIP_CALLS[call_id_val] = call_info
        return

    # Inbound call classification
    try:
        from app.services.inbound_routing_service import (
            classify_inbound_call,
            inbound_session_registry,
            InboundRoutingType,
            InboundRoutingStatus,
        )
        session, lead, target_campaign = await classify_inbound_call(
            caller_number=caller_number,
            dest_number=dest_number,
            default_tenant_id=PRIMARY_BUSINESS_ID,
            caller_name=caller_name,
            call_id_val=call_id_val,
        )
        db_call_id = session.call_id
        target_tenant = session.organization_id
    except Exception as route_err:
        logger.warning(f"[INBOUND_ROUTING_FALLBACK] Error classifying inbound call: {route_err}")
        db_call_id = str(uuid.uuid4())
        target_tenant = PRIMARY_BUSINESS_ID

    # Store active transaction in memory for answering, routing, or declining
    ACTIVE_SIP_CALLS[db_call_id] = {
        "sock": sock,
        "addr": addr,
        "via_line": via_line,
        "from_line": from_line,
        "to_line": to_header_with_tag,
        "call_id_val": call_id_val,
        "cseq_line": cseq_line,
        "cseq_num": cseq_line.split()[0] if cseq_line else "1",
        "caller_number": caller_number,
        "dest_number": dest_number,
        "status": session.call_status,
        "inbound_type": session.routing_type,
        "routing_type": session.routing_type,
        "invite_data": data,
        "rtp_sock": None,
        "call_active": [True],
        "is_ai_agent": False,
        "agent_id": session.selected_agent_id,
        "user_id": session.selected_agent_id,
        "session": session,
    }

    # Broadcast event to frontend WebSocket
    event_payload = {
        "event": "INCOMING_CALL",
        "call_id": db_call_id,
        "session_id": session.session_id,
        "caller_name": session.lead_name or (lead.first_name if lead else caller_name) or caller_name,
        "caller_phone": caller_number,
        "from_number": caller_number,
        "to_number": dest_number,
        "agent_type": "VOICE",
        "agent_id": session.selected_agent_id,
        "target_user_id": session.selected_agent_id,
        "inbound_type": session.routing_type,
        "routing_type": session.routing_type,
        "routing_status": session.routing_status,
        "lead_id": session.lead_id,
        "lead_name": session.lead_name,
        "campaign_id": session.campaign_id,
        "campaign_name": session.campaign_name,
        "outbound_call_id": session.outbound_call_id,
        "outbound_call_time": session.outbound_call_time,
        "outbound_agent_id": session.outbound_agent_id,
        "outbound_agent_name": session.outbound_agent_name,
        "outbound_agent_type": session.outbound_agent_type,
        "selected_agent_id": session.selected_agent_id,
        "selected_agent_name": session.selected_agent_name,
        "organization_id": session.organization_id,
        "waiting_for_agent": session.routing_status in (InboundRoutingStatus.WAITING_FOR_ROUTE, InboundRoutingStatus.ROUTE_REQUESTED),
        "status": session.routing_status,
        "notes": (
            f"Lead Callback from {caller_number} (Direct to Original Human Agent {session.selected_agent_name})"
            if session.routing_type == InboundRoutingType.LEAD_CALLBACK_HUMAN
            else (
                f"AI Agent Outbound Callback from {caller_number} (Original AI: {session.outbound_agent_name}) - Waiting for Admin Route"
                if session.routing_type == InboundRoutingType.AI_AGENT_CALLBACK
                else f"Normal Inbound call from {caller_number} - Waiting for Admin Route"
            )
        ),
        "initiated_at": session.initiated_at,
        "created_at": session.created_at,
    }
    await global_ws_manager.broadcast(json.dumps(event_payload))
    await dispatch_telephony_event(
        event="incoming_call",
        call_id=db_call_id,
        tenant_id=target_tenant,
        user_id=session.selected_agent_id,
        status="ringing",
        extra_data=event_payload,
    )

    if session.routing_type == InboundRoutingType.LEAD_CALLBACK_HUMAN:
        logger.info(f"[SIP INBOUND] Lead Callback {db_call_id} routed to original Human Voice Agent {session.selected_agent_name} ({session.selected_agent_id}). Ringing agent.")
    elif session.routing_type == InboundRoutingType.AI_AGENT_CALLBACK:
        logger.info(f"[SIP INBOUND] AI Agent Callback {db_call_id} placed in WAITING_FOR_ROUTE. Waiting for Admin to route to available Human Voice Agent.")
    else:
        logger.info(f"[SIP INBOUND] Normal Inbound {db_call_id} placed in WAITING_FOR_ROUTE. Waiting for Admin to route to available Human Voice Agent.")

    async def _auto_expire_unattended_call(cid: str):
        try:
            await asyncio.sleep(40.0)
            tx_check = ACTIVE_SIP_CALLS.get(cid)
            if tx_check and tx_check.get("status") == "RINGING" and not tx_check.get("answered"):
                logger.info(f"[SIP INBOUND TIMEOUT] Call {cid} unattended after 40s. Auto-declining.")
                await decline_incoming_sip_call(cid)
                from app.services.inbound_routing_service import inbound_session_registry
                await inbound_session_registry.close(cid, reason="MISSED")
        except Exception:
            pass

    asyncio.create_task(_auto_expire_unattended_call(db_call_id))


async def _handle_incoming_cancel(sock: socket.socket, data: str, addr: Tuple[str, int]):
    """Caller cancelled before attending."""
    from app.api.routes.websockets import global_ws_manager
    from app.db.database import AsyncSessionLocal
    from app.db.models import Call

    lines = data.splitlines()
    via_line = ""
    from_line = ""
    to_line = ""
    call_id_val = ""
    cseq_line = ""

    for l in lines:
        l_lower = l.lower()
        if l_lower.startswith("via:"):
            via_line = l.split(":", 1)[1].strip()
        elif l_lower.startswith("from:"):
            from_line = l.split(":", 1)[1].strip()
        elif l_lower.startswith("to:"):
            to_line = l.split(":", 1)[1].strip()
        elif l_lower.startswith("call-id:"):
            call_id_val = l.split(":", 1)[1].strip()
        elif l_lower.startswith("cseq:"):
            cseq_line = l.split(":", 1)[1].strip()

    # 1. 200 OK for CANCEL
    cancel_ok = (
        f"SIP/2.0 200 OK\r\n"
        f"Via: {via_line}\r\n"
        f"From: {from_line}\r\n"
        f"To: {to_line}\r\n"
        f"Call-ID: {call_id_val}\r\n"
        f"CSeq: {cseq_line}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    try:
        sock.sendto(cancel_ok.encode("utf-8"), addr)
    except Exception:
        pass

    # 2. 487 Request Terminated for the original INVITE
    cseq_num = cseq_line.split()[0] if cseq_line else "1"
    term_pkt = (
        f"SIP/2.0 487 Request Terminated\r\n"
        f"Via: {via_line}\r\n"
        f"From: {from_line}\r\n"
        f"To: {to_line}\r\n"
        f"Call-ID: {call_id_val}\r\n"
        f"CSeq: {cseq_num} INVITE\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    try:
        sock.sendto(term_pkt.encode("utf-8"), addr)
    except Exception:
        pass

    # Find matching call
    matched_call_id = None
    for cid, tx in list(ACTIVE_SIP_CALLS.items()):
        if tx.get("call_id_val") == call_id_val:
            matched_call_id = cid
            tx["call_active"][0] = False
            if tx.get("rtp_sock"):
                try:
                    tx["rtp_sock"].close()
                except Exception:
                    pass
            ACTIVE_SIP_CALLS.pop(cid, None)
            break

    if matched_call_id:
        async with AsyncSessionLocal() as db:
            c = await db.get(Call, matched_call_id)
            if c:
                c.status = "MISSED"
                c.call_result = "Missed / Cancelled by caller"
                c.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.add(c)
                await db.commit()

        await global_ws_manager.broadcast(json.dumps({
            "event": "INCOMING_CALL_DECLINED",
            "call_id": matched_call_id,
            "status": "MISSED",
        }))
        logger.info(f"[SIP INBOUND] Call {matched_call_id} cancelled by caller.")


async def _auto_answer_sip_call_for_ai(call_id: str, delay: float = 0.2):
    """Auto-answers the incoming SIP call for the AI Voice Agent after a short ringback."""
    if delay > 0:
        await asyncio.sleep(delay)
    tx = ACTIVE_SIP_CALLS.get(call_id)
    if not tx or tx.get("status") == "CONNECTED":
        return
    logger.info(f"[SIP INBOUND AUTO-ANSWER] Answering inbound call {call_id} for AI Voice Agent.")
    await answer_incoming_sip_call(call_id)



async def answer_incoming_sip_call(call_id: str) -> bool:
    """Sends 200 OK with SDP to answer the call on Asterisk PBX and starts the RTP bridge."""
    tx = ACTIVE_SIP_CALLS.get(call_id)
    if not tx:
        logger.warning(f"[SIP ATTEND] No active transaction found for call_id={call_id}")
        return False
    if tx.get("status") == "CONNECTED":
        return True

    sock = tx["sock"]
    addr = tx["addr"]
    via_line = tx["via_line"]
    from_line = tx["from_line"]
    to_line = tx["to_line"]
    call_id_val = tx["call_id_val"]
    cseq_num = tx["cseq_num"]
    dest_number = tx["dest_number"]
    invite_data = tx.get("invite_data", "")

    contact_ip = _get_public_ip()
    listen_port = _get_listen_port()
    # Pre-bind dedicated RTP socket on dynamic local port and resolve NAT public port via STUN
    rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if sys.platform == "win32":
        try:
            rtp_sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except Exception:
            pass
    rtp_sock.bind(("0.0.0.0", 0))
    rtp_sock.settimeout(0.5)
    local_rtp_port = rtp_sock.getsockname()[1]

    mapped_ip, mapped_port = get_stun_mapped_address(rtp_sock)
    rtp_ip = mapped_ip or contact_ip
    advertised_rtp_port = mapped_port or local_rtp_port
    logger.info(f"[SIP INBOUND ATTEND] Discovered external RTP endpoint: {rtp_ip}:{advertised_rtp_port} (Local: {local_rtp_port})")

    # Parse Asterisk remote RTP IP & port from INVITE's SDP
    remote_rtp_ip = addr[0]
    remote_rtp_port = 10000
    negotiated_pt = 0

    for sdp_line in invite_data.splitlines():
        if sdp_line.startswith("c=IN IP4"):
            remote_rtp_ip = sdp_line.split()[-1].strip()
        elif sdp_line.startswith("m=audio"):
            parts = sdp_line.split()
            if len(parts) > 1 and parts[1].isdigit():
                remote_rtp_port = int(parts[1])
            if len(parts) > 3 and parts[3].isdigit():
                negotiated_pt = int(parts[3])

    # If remote SDP advertised private IP, fallback to PBX address
    if remote_rtp_ip.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.", "127.", "0.")):
        remote_rtp_ip = addr[0]

    logger.info(f"[SIP INBOUND ATTEND] Remote PBX RTP Target: {remote_rtp_ip}:{remote_rtp_port}")

    sdp = (
        "v=0\r\n"
        f"o=CallZenza 2000 2000 IN IP4 {rtp_ip}\r\n"
        "s=CallZenza Inbound\r\n"
        f"c=IN IP4 {rtp_ip}\r\n"
        "t=0 0\r\n"
        f"m=audio {advertised_rtp_port} RTP/AVP 8 0 101\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        "a=sendrecv\r\n"
    )

    resp_200 = (
        f"SIP/2.0 200 OK\r\n"
        f"Via: {via_line}\r\n"
        f"From: {from_line}\r\n"
        f"To: {to_line}\r\n"
        f"Call-ID: {call_id_val}\r\n"
        f"CSeq: {cseq_num} INVITE\r\n"
        f"Contact: <sip:{dest_number}@{contact_ip}:{listen_port}>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n\r\n"
        f"{sdp}"
    )

    try:
        sock.sendto(resp_200.encode("utf-8"), addr)
        tx["status"] = "CONNECTED"
        logger.info(f"[ANSWERED] Sent 200 OK with SDP to Asterisk PBX for call {call_id} (RTP Port: {advertised_rtp_port})")

        # Update DB Call status
        try:
            from app.db.models import Call
            from app.db.database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                c = await db.get(Call, call_id)
                if c:
                    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                    c.status = "IN_PROGRESS"
                    c.call_result = "ANSWERED"
                    c.connected_at = now_utc
                    c.client_connected_at = now_utc
                    c.updated_at = now_utc
                    db.add(c)
                    await db.commit()
        except Exception as db_e:
            logger.warning(f"[SIP ATTEND] Call DB status update error: {db_e}")

        # Broadcast connected event to UI WebSockets
        try:
            from app.api.routes.websockets import global_ws_manager
            now_iso = datetime.now(timezone.utc).isoformat()
            await global_ws_manager.broadcast(json.dumps({
                "event": "INCOMING_CALL_ANSWERED",
                "call_id": call_id,
                "status": "IN_PROGRESS",
                "connected_at": now_iso,
            }))
            await global_ws_manager.broadcast(json.dumps({
                "event": "CALL_CONNECTED",
                "type": "CALL_CONNECTED",
                "call_id": call_id,
                "status": "CONNECTED",
                "connected_at": now_iso,
            }))
            await global_ws_manager.broadcast(json.dumps({
                "event": "CALL_STATUS_CHANGED",
                "type": "CALL_STATUS_CHANGED",
                "call_id": call_id,
                "status": "CONNECTED",
                "timestamp": now_iso,
            }))
            await global_ws_manager.broadcast(json.dumps({
                "event": "STATE_CHANGE",
                "type": "STATE_CHANGE",
                "call_id": call_id,
                "state": "CONNECTED",
                "reason": "SIP_200_OK_ANSWER",
                "timestamp": now_iso,
            }))
        except Exception:
            pass
    except Exception as exc:
        logger.error(f"Failed to send 200 OK for call {call_id}: {exc}")
        return False

    # Initialize barge audio session for browser WebSocket bridge
    try:
        from app.api.routes.barge_stream import _get_or_create_session, barge_sessions
        sess = await _get_or_create_session(call_id)
        if sess:
            sess["running"] = True
            sess["is_voice_agent_call"] = True
            sess["human_handling"] = True
            sess["agent_type"] = "HUMAN"
        if call_id_val:
            barge_sessions[call_id_val] = sess
            barge_sessions[call_id_val.split("@")[0]] = sess
    except Exception as e:
        logger.warning(f"[RTP_BRIDGE] Failed to init barge session: {e}")

    # Launch RTP audio bridge using the pre-bound socket
    try:
        tx["rtp_sock"] = rtp_sock
        call_active = tx["call_active"]
        remote_target = [remote_rtp_ip, remote_rtp_port]
        logger.info(f"[RTP] initial_destination={remote_rtp_ip}:{remote_rtp_port}")
        logger.info(f"[RTP] active_destination={remote_target[0]}:{remote_target[1]}")

        # Initial NAT hole punch packet with matching negotiated codec & sequence continuity
        punch_pt = negotiated_pt & 0x7F
        punch_silence_byte = b'\xd5' if punch_pt == 8 else b'\xff'
        punch_silence_frame = punch_silence_byte * 160
        init_seq = 1000
        init_ts = 16000
        silence_rtp = bytes([
            0x80, punch_pt,
            (init_seq >> 8) & 0xFF, init_seq & 0xFF,
            (init_ts >> 24) & 0xFF, (init_ts >> 16) & 0xFF,
            (init_ts >> 8) & 0xFF, init_ts & 0xFF,
            0x12, 0x34, 0x56, 0x78
        ]) + punch_silence_frame
        rtp_sock.sendto(silence_rtp, (remote_rtp_ip, remote_rtp_port))
        logger.info(f"[SESSION STARTED] Inbound SIP session started for call {call_id} (Target RTP: {remote_rtp_ip}:{remote_rtp_port}, Local Port={local_rtp_port}, Advertised={advertised_rtp_port})")

        loop = asyncio.get_running_loop()

        def _rtp_recv_loop(s_sock, c_active, c_id, r_target):
            first_rtp_logged = False
            rx_pkt_count = 0
            rx_bytes_total = 0
            while c_active[0]:
                try:
                    data_pkt, sender_addr = s_sock.recvfrom(2048)
                    # Validate RTP packet header (minimum 12 bytes, version==2)
                    if len(data_pkt) < 12:
                        continue
                    rtp_v = (data_pkt[0] >> 6) & 0x03
                    if rtp_v != 2:
                        continue
                    pkt_pt = data_pkt[1] & 0x7F
                    # Telephony audio payload check: only G.711 PCMU (0) or PCMA (8)
                    # Strictly ignores RTCP packets (PT 72-76 / 200-204) and non-audio companion packets
                    if pkt_pt not in (0, 8):
                        continue
                    cc = data_pkt[0] & 0x0F
                    hdr_len = 12 + (cc * 4)
                    if len(data_pkt) <= hdr_len:
                        continue

                    if sender_addr and sender_addr[0] and sender_addr[1]:
                        # Symmetric RTP: route outbound packets back to actual audio sender
                        if r_target[0] != sender_addr[0] or r_target[1] != sender_addr[1]:
                            logger.info(f"[RTP] learned_source={sender_addr[0]}:{sender_addr[1]}")
                            r_target[0] = sender_addr[0]
                            r_target[1] = sender_addr[1]
                            logger.info(f"[RTP] active_destination={r_target[0]}:{r_target[1]}")
                            logger.info(f"[RTP_BRIDGE] Symmetric RTP learned active peer: {r_target[0]}:{r_target[1]}")

                    if c_id:
                        rx_pkt_count += 1
                        payload_audio = data_pkt[hdr_len:]
                        rx_bytes_total += len(payload_audio)

                        if not first_rtp_logged:
                            first_rtp_logged = True
                            logger.info(f"[RTP RECEIVED] First RTP audio packet received from {sender_addr} for call {c_id} (size={len(data_pkt)} bytes, pt={pkt_pt})")

                        if rx_pkt_count % 100 == 0 or rx_pkt_count == 1:
                            codec_name = "PCMA" if pkt_pt == 8 else "PCMU"
                            src_ip = sender_addr[0] if sender_addr else "unknown"
                            src_port = sender_addr[1] if sender_addr else 0
                            dst_port = s_sock.getsockname()[1] if hasattr(s_sock, "getsockname") else 5062
                            logger.info(
                                f"[RTP_RX] source_ip={src_ip} source_port={src_port} destination_port={dst_port} "
                                f"payload_type={pkt_pt} codec={codec_name} packet_count={rx_pkt_count} bytes={rx_bytes_total}"
                            )
                            logger.info(f"[AUDIO_DIRECTION] CUSTOMER_TO_AI bytes={rx_bytes_total}")

                        if pkt_pt == 8 or negotiated_pt == 8:
                            from app.services.voice_service import ALAW_TO_ULAW_TABLE
                            payload_audio = payload_audio.translate(ALAW_TO_ULAW_TABLE)
                        from app.api.routes.barge_stream import barge_sessions, safe_put_audio_frame
                        sess = barge_sessions.get(c_id)
                        if not sess and "@" in str(c_id):
                            sess = barge_sessions.get(str(c_id).split("@")[0])
                        if sess:
                            in_q = sess.get("inbound_queue")
                            barge_q = sess.get("barge_inbound_queue")
                            if barge_q:
                                while barge_q.qsize() > 10:
                                    try:
                                        barge_q.get_nowait()
                                    except Exception:
                                        break
                            if loop and not loop.is_closed():
                                try:
                                    if in_q:
                                        loop.call_soon_threadsafe(safe_put_audio_frame, in_q, payload_audio, "inbound_queue", c_id)
                                    if barge_q:
                                        loop.call_soon_threadsafe(safe_put_audio_frame, barge_q, payload_audio, "barge_inbound_queue", c_id)
                                except RuntimeError:
                                    break
                            else:
                                if in_q:
                                    safe_put_audio_frame(in_q, payload_audio, "inbound_queue", c_id)
                                if barge_q:
                                    safe_put_audio_frame(barge_q, payload_audio, "barge_inbound_queue", c_id)
                except socket.timeout:
                    continue
                except Exception as rx_err:
                    err_str = str(rx_err)
                    win_err = getattr(rx_err, "winerror", None)
                    if win_err == 10054 or "10054" in err_str:
                        continue
                    is_teardown = (
                        not c_active[0]
                        or "10038" in err_str
                        or "EBADF" in err_str
                        or win_err in (10038,)
                        or getattr(rx_err, "errno", None) in (9, 10038)
                    )
                    if is_teardown:
                        break
                    logger.warning(f"[RTP_RX_ERROR] Inbound RX warning: {rx_err}")
                    continue
            logger.info(f"[RTP_RX_END] Total customer audio received: {rx_bytes_total} bytes ({rx_pkt_count} packets)")

        def _rtp_send_loop(s_sock, c_active, c_id, r_target, pt):
            import time
            import sys
            from app.api.routes.barge_stream import barge_sessions
            is_win = (sys.platform == 'win32')
            if is_win:
                try:
                    import ctypes
                    ctypes.windll.winmm.timeBeginPeriod(1)
                except Exception:
                    pass
            tx_pkt_count = 0
            tx_bytes_total = 0
            real_audio_pkt_count = 0
            try:
                seq = 1000
                ts = 16000
                ssrc = 0x12345678
                is_alaw = (pt == 8)
                silence_byte = b'\xd5' if is_alaw else b'\xff'
                silence_frame = silence_byte * 160
                
                frame_duration = 0.020  # Exactly 20ms per packet
                start_perf = time.perf_counter()
                pkt_seq_counter = 0

                while c_active[0]:
                    pkt_seq_counter += 1
                    target_time = start_perf + (pkt_seq_counter * frame_duration)
                    now = time.perf_counter()
                    sleep_rem = target_time - now
                    if sleep_rem > 0.0015:
                        time.sleep(sleep_rem - 0.001)
                    while time.perf_counter() < target_time:
                        pass
                    if time.perf_counter() > target_time + 0.080:
                        start_perf = time.perf_counter()
                        pkt_seq_counter = 0

                    sess = barge_sessions.get(c_id) if c_id else None
                    if not sess and c_id and "@" in str(c_id):
                        sess = barge_sessions.get(str(c_id).split("@")[0])
                    if not sess and c_id:
                        clean_k = str(c_id).split("@")[0]
                        for cand in list(barge_sessions.values()):
                            if cand.get("db_call_id") in (c_id, clean_k) or c_id in cand.get("aliases", set()) or clean_k in cand.get("aliases", set()):
                                sess = cand
                                break
                    if not sess:
                        continue

                    cur_ip = r_target[0]
                    cur_port = r_target[1]
                    if not cur_ip or not cur_port:
                        continue

                    # Meter out one 160-byte (20ms) frame per tick from authoritative ai_rtp_tx_queue or rtp_outbound_queue
                    frame_payload = None
                    ai_q = sess.get('ai_rtp_tx_queue') if sess else None
                    if ai_q and len(ai_q) > 0:
                        try:
                            frame_payload = ai_q.popleft()
                        except Exception:
                            frame_payload = None
                    elif sess and sess.get('rtp_outbound_queue') and len(sess['rtp_outbound_queue']) > 0:
                        try:
                            frame_payload = sess['rtp_outbound_queue'].popleft()
                        except Exception:
                            frame_payload = None

                    is_real_audio = False
                    if frame_payload:
                        is_real_audio = True
                        if len(frame_payload) < 160:
                            frame_payload = frame_payload + (silence_byte * (160 - len(frame_payload)))
                        elif len(frame_payload) > 160:
                            frame_payload = frame_payload[:160]
                        if is_alaw:
                            from app.services.voice_service import ULAW_TO_ALAW_TABLE
                            frame_payload = frame_payload.translate(ULAW_TO_ALAW_TABLE)
                    else:
                        frame_payload = silence_frame

                    seq = (seq + 1) & 0xFFFF
                    ts = (ts + 160) & 0xFFFFFFFF
                    hdr = bytes([
                        0x80, pt & 0x7F,
                        (seq >> 8) & 0xFF, seq & 0xFF,
                        (ts >> 24) & 0xFF, (ts >> 16) & 0xFF,
                        (ts >> 8) & 0xFF, ts & 0xFF,
                        (ssrc >> 24) & 0xFF, (ssrc >> 16) & 0xFF,
                        (ssrc >> 8) & 0xFF, ssrc & 0xFF,
                    ])
                    out_pkt = hdr + frame_payload
                    try:
                        s_sock.sendto(out_pkt, (cur_ip, cur_port))
                        tx_pkt_count += 1
                        if is_real_audio:
                            real_audio_pkt_count += 1
                            tx_bytes_total += len(frame_payload)
                            q_rem = len(ai_q) if ai_q is not None else 0
                            if real_audio_pkt_count == 1 or real_audio_pkt_count % 10 == 0 or q_rem == 0:
                                logger.info(f"[RTP_TX] destination={cur_ip}:{cur_port} payload_type={pt} sequence={seq} timestamp={ts} packet_size={len(out_pkt)} packets_sent={real_audio_pkt_count}")
                                logger.info(f"[AUDIO_DIRECTION] AI_TO_CUSTOMER bytes={tx_bytes_total}")
                        else:
                            real_audio_pkt_count = 0
                    except Exception as tx_err:
                        logger.warning(f"[RTP_TX_ERROR] {tx_err}")
            finally:
                if is_win:
                    try:
                        import ctypes
                        ctypes.windll.winmm.timeEndPeriod(1)
                    except Exception:
                        pass

        t_recv = threading.Thread(target=_rtp_recv_loop, args=(rtp_sock, call_active, call_id, remote_target), daemon=True)
        t_send = threading.Thread(target=_rtp_send_loop, args=(rtp_sock, call_active, call_id, remote_target, negotiated_pt), daemon=True)
        t_recv.start()
        t_send.start()
        logger.info(f"[RTP_BRIDGE] Started RTP audio bridge for call {call_id} on port {advertised_rtp_port}")

        # Check if AI bot should be dispatched
        is_ai_call = tx.get("is_ai_agent", False) is True
        if is_ai_call:
            from app.services.sip_ai_bot import start_sip_ai_bot
            start_sip_ai_bot(call_id, is_inbound=True)
            logger.info(f"[AI STARTED] Dispatched AI Voice Agent session for call {call_id}")
        else:
            logger.info(f"[SIP CALL] Pure human two-way audio bridge active for call {call_id}. AI processing (STT/LLM/TTS/RAG/AgentEngine) is COMPLETELY DISABLED.")
    except Exception as rtp_err:
        logger.warning(f"[RTP_BRIDGE] RTP socket bind failed on port {advertised_rtp_port}: {rtp_err}")

    return True



async def decline_incoming_sip_call(call_id: str) -> bool:
    """Sends 486 Busy Here or BYE to Asterisk PBX to terminate the call."""
    tx = ACTIVE_SIP_CALLS.pop(call_id, None)
    if not tx:
        return False

    tx["call_active"][0] = False
    try:
        from app.services.sip_ai_bot import stop_sip_ai_bot
        stop_sip_ai_bot(call_id)
    except Exception:
        pass
    if tx.get("rtp_sock"):
        try:
            tx["rtp_sock"].close()
        except Exception:
            pass

    try:
        from app.api.routes.barge_stream import barge_sessions
        b_sess = barge_sessions.pop(call_id, None)
        if b_sess:
            b_sess["running"] = False
            b_sess["active"] = False
    except Exception:
        pass

    sock = tx["sock"]
    addr = tx["addr"]
    via_line = tx["via_line"]
    from_line = tx["from_line"]
    to_line = tx["to_line"]
    call_id_val = tx["call_id_val"]
    cseq_num = tx["cseq_num"]

    if tx.get("status") == "CONNECTED":
        host = tx.get("remote_host") or (addr[0] if addr else "127.0.0.1")
        port = tx.get("remote_port") or (addr[1] if addr else 5060)
        contact_ip = _get_public_ip()
        listen_port = _get_listen_port()
        bye_branch = uuid.uuid4().hex[:8]
        bye_pkt = (
            f"BYE sip:{host}:{port} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {contact_ip}:{listen_port};branch=z9hG4bK-{bye_branch};rport\r\n"
            f"Max-Forwards: 70\r\n"
            f"From: {to_line}\r\n"
            f"To: {from_line}\r\n"
            f"Call-ID: {call_id_val}\r\n"
            f"CSeq: 100 BYE\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        try:
            sock.sendto(bye_pkt.encode("utf-8"), addr)
            logger.info(f"[SIP INBOUND HANGUP] Sent BYE to Asterisk PBX for active call {call_id}")
            return True
        except Exception as exc:
            logger.error(f"Failed to send BYE: {exc}")
            return False
    else:
        busy_pkt = (
            f"SIP/2.0 486 Busy Here\r\n"
            f"Via: {via_line}\r\n"
            f"From: {from_line}\r\n"
            f"To: {to_line}\r\n"
            f"Call-ID: {call_id_val}\r\n"
            f"CSeq: {cseq_num} INVITE\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        try:
            sock.sendto(busy_pkt.encode("utf-8"), addr)
            logger.info(f"[SIP INBOUND DECLINED] Sent 486 Busy Here to Asterisk PBX for call {call_id}")
            return True
        except Exception as exc:
            logger.error(f"Failed to send 486 Busy Here: {exc}")
            return False


def _extract_header_val(msg: str, header_name: str) -> str:
    compact_map = {
        "via": "v",
        "from": "f",
        "to": "t",
        "call-id": "i",
        "contact": "m",
        "content-length": "l",
        "content-type": "c",
        "subject": "s",
        "supported": "k",
    }
    target_names = [header_name.lower()]
    c_form = compact_map.get(header_name.lower())
    if c_form:
        target_names.append(c_form)

    for line in msg.splitlines():
        line_clean = line.strip()
        for t_name in target_names:
            prefix = t_name + ":"
            if line_clean.lower().startswith(prefix):
                return line_clean[len(prefix):].strip()
    return ""


ACTIVE_INBOUND_ACCOUNT = None
CURRENT_REGISTERED_HOST = ""
CURRENT_REGISTERED_PORT = 5060
CURRENT_REGISTERED_USER = ""
RELOAD_REGISTRATION_EVENT = asyncio.Event()


def notify_sip_account_changed():
    """Wakes the SIP daemon loop to reload registration from the database."""
    global RELOAD_REGISTRATION_EVENT
    try:
        RELOAD_REGISTRATION_EVENT.set()
    except Exception:
        pass


async def _load_active_inbound_account():
    """Queries the database for the active inbound SIPAccount."""
    try:
        from app.db.database import AsyncSessionLocal
        from app.db.models import SIPAccount
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            stmt = select(SIPAccount).where(
                SIPAccount.enabled == True,
                SIPAccount.direction.in_(["inbound", "both", "BIDIRECTIONAL"])
            ).order_by(SIPAccount.created_at.desc())
            accounts = (await db.scalars(stmt)).all()
            if not accounts:
                return None
            for acc in accounts:
                if acc.is_default:
                    return acc
            valid_accs = [
                a for a in accounts
                if a.host and a.host.strip() and a.host.strip().lower() not in ("host", "test", "dummy", "placeholder", "localhost", "127.0.0.1", "0.0.0.0", "none", "null")
            ]
            if valid_accs:
                return valid_accs[0]
            # Fall back to any enabled account configured with a real remote SIP registrar
            all_valid = (await db.scalars(
                select(SIPAccount).where(SIPAccount.enabled == True).order_by(SIPAccount.created_at.desc())
            )).all()
            remote_accs = [
                a for a in all_valid
                if a.host and a.host.strip() and a.host.strip().lower() not in ("host", "test", "dummy", "placeholder", "localhost", "127.0.0.1", "0.0.0.0", "none", "null")
            ]
            if remote_accs:
                return remote_accs[0]
            return None
    except Exception as e:
        logger.warning(f"[SIP_DAEMON] Could not query active inbound SIP account from DB: {e}")
        return None


async def _run_sip_daemon_loop():
    """Background listener loop for UDP socket and periodic REGISTER keep-alive."""
    global DAEMON_RUNNING, DAEMON_SOCK, IS_REGISTERED, ACTIVE_INBOUND_ACCOUNT
    global CURRENT_REGISTERED_HOST, CURRENT_REGISTERED_PORT, CURRENT_REGISTERED_USER

    contact_ip = os.getenv("SIP_CONTACT_IP", "").strip() or getattr(settings, "sip_contact_ip", None) or _get_public_ip()
    listen_port = _get_listen_port()

    # Load active inbound account from database
    active_acc = await _load_active_inbound_account()
    ACTIVE_INBOUND_ACCOUNT = active_acc
    if active_acc:
        DAEMON_REGISTRATION_INFO["account_id"] = active_acc.id
        host = (active_acc.host or "").strip()
        port = int(active_acc.port or 5060)
        user = (active_acc.username or "").strip()
        auth_user = (active_acc.auth_username or active_acc.username or "").strip()
        from app.services.sip_account_service import decrypt_sip_secret
        pwd = decrypt_sip_secret(active_acc.password_hash) if active_acc.password_hash else ""
        keepalive_interval = active_acc.keep_alive_interval or 15
        CURRENT_REGISTERED_HOST = host
        CURRENT_REGISTERED_PORT = port
        CURRENT_REGISTERED_USER = user
        masked_host = host if len(host) <= 4 else host[:2] + "***" + host[-2:]
        masked_user = user if len(user) <= 2 else user[:2] + "***"
        logger.info(f"[SIP_CONFIG] Loaded active inbound SIP account '{active_acc.name}' from database")
        logger.info(f"[SIP_CONFIG] SIP host: {masked_host}:{port}")
        logger.info(f"[SIP_CONFIG] SIP username: {masked_user}")
    else:
        host = ""
        port = 5060
        user = ""
        pwd = ""
        keepalive_interval = 15
        CURRENT_REGISTERED_HOST = ""
        CURRENT_REGISTERED_PORT = 5060
        CURRENT_REGISTERED_USER = ""
        logger.info(f"[SIP_CONFIG] No enabled inbound SIP account configured in database. UDP listener active on port {listen_port}.")

    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if sys.platform == "win32":
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setblocking(False)

    try:
        sock.bind(("0.0.0.0", listen_port))
    except Exception as exc:
        logger.error(f"[SIP DAEMON] Could not bind port {listen_port}: {exc}")
        return

    DAEMON_SOCK = sock
    DAEMON_RUNNING = True
    logger.info(f"[SIP REG DEBUG] Local SIP socket bound: 0.0.0.0:{listen_port}")
    logger.info(f"[SIP REG DEBUG] REGISTER destination: {host}:{port}")
    logger.info(f"[SIP REG DEBUG] Contact advertised: <sip:{user}@{contact_ip}:{listen_port}>")
    logger.info(f"[SIP DAEMON] Listener started on UDP 0.0.0.0:{listen_port} (Contact: {contact_ip}:{listen_port})")

    loop = asyncio.get_running_loop()

    # Explicit UDP reader for uvloop.
    recv_queue = asyncio.Queue()

    def _sip_udp_reader():
        while True:
            try:
                data_bytes, addr = sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as exc:
                win_err = getattr(exc, "winerror", None)
                if win_err in (10038,) or exc.errno in (9, 10038):
                    try:
                        loop.remove_reader(sock.fileno())
                    except Exception:
                        pass
                elif win_err == 10054 or exc.errno == 10054 or "10054" in str(exc):
                    # WinSock ICMP Port Unreachable artifact on Windows UDP — ignore cleanly
                    pass
                else:
                    logger.warning(
                        f"[SIP RECV ERROR] UDP socket receive error: {exc!r}"
                    )
                break

            try:
                recv_queue.put_nowait((data_bytes, addr))
            except Exception as exc:
                logger.warning(
                    f"[SIP RECV ERROR] Could not queue UDP packet: {exc!r}"
                )

    try:
        loop.add_reader(sock.fileno(), _sip_udp_reader)
    except (NotImplementedError, AttributeError):
        def _win_udp_reader_thread():
            import select
            while DAEMON_RUNNING:
                try:
                    r, _, _ = select.select([sock], [], [], 0.5)
                    if r:
                        try:
                            d, a = sock.recvfrom(4096)
                            loop.call_soon_threadsafe(recv_queue.put_nowait, (d, a))
                        except Exception:
                            pass
                except Exception:
                    pass
        threading.Thread(target=_win_udp_reader_thread, daemon=True).start()

    # Initial registration if configured
    reg_call_id = f"reg-{uuid.uuid4().hex[:12]}@callzenza"
    reg_tag = uuid.uuid4().hex[:8]
    cseq_current = 1
    auth_sent_for_cseq: Optional[int] = None
    current_attempt_id = f"att-{uuid.uuid4().hex[:8]}"

    if host and user:
        logger.info(f"[SIP REG] Registering with {user}@{host} attempt={current_attempt_id}")
        req1 = build_register_packet(host, port, user, contact_ip, listen_port, reg_call_id, reg_tag, cseq_num=cseq_current)
        try:
            _sip_send(req1, host, port)
            logger.info(f"[SIP_REGISTER] Sent initial REGISTER to {host}:{port} (attempt_id={current_attempt_id})")
        except socket.gaierror as dns_err:
            register_sent_time = time.time() + 50.0
            logger.warning(f"[SIP_REGISTER_FAILURE] Cannot resolve SIP registrar host '{host}': {dns_err}. Backing off 60s.")
        except Exception as e:
            logger.warning(f"[SIP_REGISTER_FAILURE] Initial REGISTER send error: {e}")

    last_reg_time = time.time()
    register_sent_time = time.time()

    while DAEMON_RUNNING:
        try:
            # Check if an administrator updated/added an account in the UI
            if RELOAD_REGISTRATION_EVENT.is_set():
                RELOAD_REGISTRATION_EVENT.clear()
                logger.info("[SIP_DAEMON] Reload event triggered — reloading active inbound account from database...")
                new_acc = await _load_active_inbound_account()
                ACTIVE_INBOUND_ACCOUNT = new_acc
                if new_acc:
                    host = (new_acc.host or "").strip()
                    port = int(new_acc.port or 5060)
                    user = (new_acc.username or "").strip()
                    auth_user = (new_acc.auth_username or new_acc.username or "").strip()
                    from app.services.sip_account_service import decrypt_sip_secret
                    pwd = decrypt_sip_secret(new_acc.password_hash) if new_acc.password_hash else ""
                    keepalive_interval = new_acc.keep_alive_interval or 15
                    CURRENT_REGISTERED_HOST = host
                    CURRENT_REGISTERED_PORT = port
                    CURRENT_REGISTERED_USER = user
                    IS_REGISTERED = False
                    register_sent_time = 0.0
                    reg_call_id = f"reg-{uuid.uuid4().hex[:12]}@callzenza"
                    reg_tag = uuid.uuid4().hex[:8]
                    cseq_current = 1
                    auth_sent_for_cseq = None
                    current_attempt_id = f"att-{uuid.uuid4().hex[:8]}"
                    logger.info(f"[SIP_DAEMON] Reloaded inbound account '{new_acc.name}'. Re-registering with {host}:{port}...")
                else:
                    host = ""
                    port = 5060
                    user = ""
                    pwd = ""
                    CURRENT_REGISTERED_HOST = ""
                    CURRENT_REGISTERED_PORT = 5060
                    CURRENT_REGISTERED_USER = ""
                    IS_REGISTERED = False
                    auth_sent_for_cseq = None
                    logger.info("[SIP_DAEMON] No active inbound account after reload. Registration cleared.")

            now_t = time.time()
            if host and user:
                if not IS_REGISTERED and (now_t - register_sent_time >= 10.0):
                    register_sent_time = now_t
                    last_reg_time = now_t
                    reg_call_id = f"reg-{uuid.uuid4().hex[:12]}@callzenza"
                    reg_tag = uuid.uuid4().hex[:8]
                    cseq_current = 1
                    auth_sent_for_cseq = None
                    current_attempt_id = f"att-{uuid.uuid4().hex[:8]}"
                    req = build_register_packet(host, port, user, contact_ip, listen_port, reg_call_id, reg_tag, cseq_num=cseq_current)
                    try:
                        _sip_send(req, host, port)
                        logger.info(f"[SIP_REGISTER] Sent retry REGISTER to {host}:{port} (attempt_id={current_attempt_id})")
                    except socket.gaierror as dns_err:
                        register_sent_time = now_t + 50.0  # Back off 60s total on DNS resolution failure
                        logger.warning(f"[SIP DAEMON] Cannot resolve SIP registrar host '{host}': {dns_err}. Backing off 60s before retry.")
                    except Exception as e:
                        logger.warning(f"[SIP DAEMON] Retry REGISTER send error: {e}")
                elif IS_REGISTERED and (now_t - last_reg_time >= keepalive_interval):
                    last_reg_time = now_t
                    # RFC 3261 Section 10.2: Start each refresh as a fresh transaction to ensure digest challenges are handled cleanly
                    reg_call_id = f"reg-{uuid.uuid4().hex[:12]}@callzenza"
                    reg_tag = uuid.uuid4().hex[:8]
                    cseq_current = 1
                    auth_sent_for_cseq = None
                    current_attempt_id = f"att-{uuid.uuid4().hex[:8]}"
                    req = build_register_packet(host, port, user, contact_ip, listen_port, reg_call_id, reg_tag, cseq_num=cseq_current)
                    try:
                        _sip_send(req, host, port)
                        logger.info(f"[SIP_REGISTER] Sent keepalive REGISTER to {host}:{port} (attempt_id={current_attempt_id})")
                    except Exception as e:
                        logger.warning(f"[SIP_REGISTER_FAILURE] Keepalive REGISTER error: {e}")

            # Receive UDP packets from the uvloop-compatible reader queue.
            try:
                data_bytes, addr = await asyncio.wait_for(
                    recv_queue.get(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                if not DAEMON_RUNNING:
                    break
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    f"[SIP RECV ERROR] UDP receive queue error: {exc!r}"
                )
                if not DAEMON_RUNNING:
                    break
                await asyncio.sleep(0.05)
                continue

            msg = data_bytes.decode("utf-8", errors="ignore")
            first_line = msg.splitlines()[0].strip() if msg else ""

            call_id_hdr = _extract_header_val(msg, "Call-ID")
            cseq_hdr = _extract_header_val(msg, "CSeq")
            via_hdr = _extract_header_val(msg, "Via")
            branch_match = re.search(r'branch=([^;\s]+)', via_hdr, re.IGNORECASE)
            via_branch = branch_match.group(1) if branch_match else ""

            # OPTIONS = Asterisk keep-alive probe (very frequent) — log at DEBUG only
            if first_line.startswith("OPTIONS"):
                logger.debug(f"[SIP RECV FROM {addr}] {first_line}")
            else:
                logger.info(f"[SIP REG DEBUG] Received UDP packet from {addr[0]}:{addr[1]}")
                logger.info(f"[SIP REG DEBUG] SIP response first line: {first_line}")
                logger.info(f"[SIP REG DEBUG] Call-ID: {call_id_hdr}")
                logger.info(f"[SIP REG DEBUG] CSeq: {cseq_hdr}")
                logger.info(f"[SIP REG DEBUG] Via branch: {via_branch}")

            # Check if this is a response to our REGISTER
            if "REGISTER" in msg:
                # Extract actual public WAN IP as observed by Asterisk PBX
                rec_m = re.search(r'received=([0-9.]+)', msg)
                if rec_m:
                    detected_wan_ip = rec_m.group(1).strip()
                    if detected_wan_ip and detected_wan_ip != contact_ip and not detected_wan_ip.startswith("10.") and not detected_wan_ip.startswith("192.168."):
                        contact_ip = detected_wan_ip
                        _CACHED_PUBLIC_IP = detected_wan_ip
                        _CACHED_PUBLIC_IP_TS = time.time()
                        logger.info(f"[SIP DAEMON] Synchronized Contact IP to PBX-observed WAN IP: {contact_ip}")

                acc_id = ACTIVE_INBOUND_ACCOUNT.id if ACTIVE_INBOUND_ACCOUNT else None
                org_id = getattr(ACTIVE_INBOUND_ACCOUNT, "organization_id", None) if ACTIVE_INBOUND_ACCOUNT else None

                if ("401 Unauthorized" in msg or "407 Proxy Authentication" in msg):
                    is_initial_challenge = (auth_sent_for_cseq is None or auth_sent_for_cseq != cseq_current)
                    if is_initial_challenge:
                        # Check for unsupported digest algorithm
                        algo_m = re.search(r'algorithm="?([^",\r\n]+)"?', msg, re.IGNORECASE)
                        if algo_m and algo_m.group(1).upper() not in ("MD5", "MD5-SESS"):
                            logger.warning(f"[SIP_REGISTER_FAILURE] Unsupported digest algorithm '{algo_m.group(1)}' from {host}:{port}. MD5 required.")
                            register_sent_time = now_t + 110.0
                            IS_REGISTERED = False
                            _record_daemon_registration_event(
                                account_id=acc_id,
                                organization_id=org_id,
                                host=host,
                                port=port,
                                user=user,
                                status="auth_failed",
                                is_registered=False,
                                error_message=f"Unsupported digest algorithm '{algo_m.group(1)}' from {host}:{port}. MD5 required.",
                                error_code="UNSUPPORTED_DIGEST_ALGORITHM",
                                sip_status_code=401,
                                suggested_action="Configure your SIP provider or PBX to use standard MD5 digest authentication.",
                                attempt_id=current_attempt_id,
                            )
                        else:
                            logger.info(f"[SIP_REGISTER_CHALLENGE] Received challenge from {host}:{port} for attempt={current_attempt_id}")
                            realm_m = re.search(r'realm="([^"]+)"', msg)
                            nonce_m = re.search(r'nonce="([^"]+)"', msg)
                            qop_m = re.search(r'qop="?([^,\r\n"]+)"?', msg)
                            opaque_m = re.search(r'opaque="([^"]+)"', msg)

                            if realm_m and nonce_m:
                                realm = realm_m.group(1)
                                nonce = nonce_m.group(1)
                                qop = qop_m.group(1) if qop_m else None
                                opaque = opaque_m.group(1) if opaque_m else None
                                uri = f"sip:{host}:{port}"

                                auth_header = compute_digest_auth(user, pwd, realm, nonce, uri, qop, opaque, auth_user=auth_user)
                                cseq_current += 1
                                auth_sent_for_cseq = cseq_current
                                req2 = build_register_packet(
                                    host, port, user, contact_ip, listen_port,
                                    reg_call_id, reg_tag, cseq_num=cseq_current, auth_header=auth_header
                                )
                                _sip_send(req2, host, port)
                                logger.info(f"[SIP_REGISTER] Sent authenticated REGISTER step 2 (Contact: {contact_ip}:{listen_port}, attempt_id={current_attempt_id})")
                            else:
                                logger.warning(f"[SIP_REGISTER_FAILURE] Incomplete challenge received from {host}:{port}.")
                                _record_daemon_registration_event(
                                    account_id=acc_id,
                                    organization_id=org_id,
                                    host=host,
                                    port=port,
                                    user=user,
                                    status="auth_failed",
                                    is_registered=False,
                                    error_message=f"Incomplete authentication challenge (missing realm/nonce) from {host}:{port}.",
                                    error_code="AUTH_CHALLENGE_INCOMPLETE",
                                    sip_status_code=401,
                                    suggested_action="Check SIP server configuration for compliant digest challenge headers.",
                                    attempt_id=current_attempt_id,
                                )
                    else:
                        # Auth was already sent for this transaction!
                        stale_m = re.search(r'stale="?([^",\r\n]+)"?', msg, re.IGNORECASE)
                        is_stale = bool(stale_m and stale_m.group(1).lower() in ("true", "yes", "1"))
                        if is_stale and cseq_current <= 2:
                            logger.info(f"[SIP_REGISTER_CHALLENGE] Stale nonce reported from {host}:{port}. Retrying with fresh nonce (RFC 3261 Section 22.4)...")
                            realm_m = re.search(r'realm="([^"]+)"', msg)
                            nonce_m = re.search(r'nonce="([^"]+)"', msg)
                            qop_m = re.search(r'qop="?([^,\r\n"]+)"?', msg)
                            opaque_m = re.search(r'opaque="([^"]+)"', msg)
                            if realm_m and nonce_m:
                                realm = realm_m.group(1)
                                nonce = nonce_m.group(1)
                                qop = qop_m.group(1) if qop_m else None
                                opaque = opaque_m.group(1) if opaque_m else None
                                uri = f"sip:{host}:{port}"
                                auth_header = compute_digest_auth(user, pwd, realm, nonce, uri, qop, opaque, auth_user=auth_user)
                                cseq_current += 1
                                auth_sent_for_cseq = cseq_current
                                req3 = build_register_packet(
                                    host, port, user, contact_ip, listen_port,
                                    reg_call_id, reg_tag, cseq_num=cseq_current, auth_header=auth_header
                                )
                                _sip_send(req3, host, port)
                                logger.info(f"[SIP_REGISTER] Sent authenticated REGISTER step 3 with fresh nonce (Contact: {contact_ip}:{listen_port}, attempt_id={current_attempt_id})")
                            else:
                                logger.warning(f"[SIP_REGISTER_FAILURE] Authentication challenge incomplete on stale retry from {host}:{port}.")
                                register_sent_time = now_t + 50.0
                                IS_REGISTERED = False
                        else:
                            logger.warning(
                                f"[SIP_REGISTER_FAILURE] Authentication retry rejected (401/407) from {host}:{port} for attempt={current_attempt_id}. "
                                f"Verify SIP authentication username and password, and confirm the provider's digest authentication requirements."
                            )
                            # Bounded backoff: do not retry invalid credentials indefinitely
                            register_sent_time = now_t + 50.0
                            IS_REGISTERED = False
                            err_msg = (
                                f"Registration rejected: authentication retry rejected (401/407) from {host}:{port}. "
                                "Verify SIP authentication username and password, and confirm the provider's digest authentication requirements."
                            )
                            _record_daemon_registration_event(
                                account_id=acc_id,
                                organization_id=org_id,
                                host=host,
                                port=port,
                                user=user,
                                status="auth_failed",
                                is_registered=False,
                                error_message=err_msg,
                                error_code="AUTH_REJECTED",
                                sip_status_code=401,
                                suggested_action="Verify SIP authentication username and password in account configuration.",
                                attempt_id=current_attempt_id,
                            )
                elif "200 OK" in msg:
                    IS_REGISTERED = True
                    logger.info(f"[SIP_REGISTER_SUCCESS] Registered successfully with Asterisk PBX {host}:{port} as '{user}' on port {listen_port} (attempt_id={current_attempt_id})")
                    _record_daemon_registration_event(
                        account_id=acc_id,
                        organization_id=org_id,
                        host=host,
                        port=port,
                        user=user,
                        status="registered",
                        is_registered=True,
                        error_message=None,
                        error_code=None,
                        sip_status_code=200,
                        suggested_action="Account is registered and active for inbound and outbound calls.",
                        attempt_id=current_attempt_id,
                    )
                elif "403 Forbidden" in msg:
                    logger.warning(f"[SIP_REGISTER_FAILURE] Registration explicitly denied (403 Forbidden) from {host}:{port}: {first_line}")
                    register_sent_time = now_t + 110.0
                    IS_REGISTERED = False
                    err_msg = f"Registration explicitly denied (403 Forbidden) from {host}:{port}."
                    _record_daemon_registration_event(
                        account_id=acc_id,
                        organization_id=org_id,
                        host=host,
                        port=port,
                        user=user,
                        status="auth_failed",
                        is_registered=False,
                        error_message=err_msg,
                        error_code="FORBIDDEN",
                        sip_status_code=403,
                        suggested_action="Check with SIP provider if registration is permitted and server IP is whitelisted.",
                        attempt_id=current_attempt_id,
                    )
                elif "404 Not Found" in msg:
                    logger.warning(f"[SIP_REGISTER_FAILURE] Registration resource not found (404 Not Found) from {host}:{port}: {first_line}")
                    register_sent_time = now_t + 110.0
                    IS_REGISTERED = False
                    err_msg = f"Registration resource not found (404 Not Found) from {host}:{port}."
                    _record_daemon_registration_event(
                        account_id=acc_id,
                        organization_id=org_id,
                        host=host,
                        port=port,
                        user=user,
                        status="error",
                        is_registered=False,
                        error_message=err_msg,
                        error_code="NOT_FOUND",
                        sip_status_code=404,
                        suggested_action="Confirm SIP username, domain, and registrar host with provider.",
                        attempt_id=current_attempt_id,
                    )
                elif "408 Request Timeout" in msg or "408 Timeout" in msg:
                    logger.warning(f"[SIP_REGISTER_FAILURE] Registration request timeout (408) from {host}:{port}: {first_line}")
                    register_sent_time = now_t + 20.0
                    IS_REGISTERED = False
                    err_msg = f"Registration request timeout (408) from {host}:{port}."
                    _record_daemon_registration_event(
                        account_id=acc_id,
                        organization_id=org_id,
                        host=host,
                        port=port,
                        user=user,
                        status="network_error",
                        is_registered=False,
                        error_message=err_msg,
                        error_code="REQUEST_TIMEOUT",
                        sip_status_code=408,
                        suggested_action="Check network latency or server load and retry.",
                        attempt_id=current_attempt_id,
                    )


            # Check if incoming request from PBX
            if first_line.startswith("INVITE"):
                await _handle_incoming_invite(sock, msg, addr)
            elif first_line.startswith("CANCEL"):
                await _handle_incoming_cancel(sock, msg, addr)
            elif first_line.startswith("OPTIONS"):
                # Answer PBX heartbeat / qualify probe
                try:
                    lines = msg.splitlines()
                    via = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("via:")), "")
                    frm = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("from:")), "")
                    to = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("to:")), "")
                    cid = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("call-id:")), "")
                    cseq = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("cseq:")), "")
                    options_ok = (
                        f"SIP/2.0 200 OK\r\n"
                        f"Via: {via}\r\n"
                        f"From: {frm}\r\n"
                        f"To: {to}\r\n"
                        f"Call-ID: {cid}\r\n"
                        f"CSeq: {cseq}\r\n"
                        f"Contact: <sip:{user}@{contact_ip}:{listen_port}>\r\n"
                        f"Allow: INVITE, ACK, CANCEL, OPTIONS, BYE\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    sock.sendto(options_ok.encode("utf-8"), addr)
                    logger.info("[SIP OPTIONS] Replied 200 OK to Asterisk qualify ping")
                except Exception:
                    pass
            elif first_line.startswith("NOTIFY"):
                # PBX subscription event (MWI, presence) — acknowledge
                try:
                    lines = msg.splitlines()
                    via = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("via:")), "")
                    frm = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("from:")), "")
                    to = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("to:")), "")
                    cid = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("call-id:")), "")
                    cseq = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("cseq:")), "")
                    notify_ok = (
                        f"SIP/2.0 200 OK\r\n"
                        f"Via: {via}\r\n"
                        f"From: {frm}\r\n"
                        f"To: {to}\r\n"
                        f"Call-ID: {cid}\r\n"
                        f"CSeq: {cseq}\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    sock.sendto(notify_ok.encode("utf-8"), addr)
                    logger.debug(f"[SIP DAEMON] Responded 200 OK to NOTIFY from PBX {addr}")
                except Exception:
                    pass
            elif first_line.startswith("INFO"):
                # PBX INFO (DTMF relay etc.) — acknowledge
                try:
                    lines = msg.splitlines()
                    via = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("via:")), "")
                    frm = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("from:")), "")
                    to = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("to:")), "")
                    cid = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("call-id:")), "")
                    cseq = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("cseq:")), "")
                    info_ok = (
                        f"SIP/2.0 200 OK\r\n"
                        f"Via: {via}\r\n"
                        f"From: {frm}\r\n"
                        f"To: {to}\r\n"
                        f"Call-ID: {cid}\r\n"
                        f"CSeq: {cseq}\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    sock.sendto(info_ok.encode("utf-8"), addr)
                except Exception:
                    pass
            elif first_line.startswith("ACK"):
                logger.debug(f"[SIP DAEMON] Received ACK from PBX {addr} - call is active.")
            elif first_line.startswith("BYE"):
                # PBX ended the call
                try:
                    lines = msg.splitlines()
                    via = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("via:")), "")
                    frm = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("from:")), "")
                    to = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("to:")), "")
                    cid = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("call-id:")), "")
                    cseq = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("cseq:")), "")
                    bye_ok = (
                        f"SIP/2.0 200 OK\r\n"
                        f"Via: {via}\r\n"
                        f"From: {frm}\r\n"
                        f"To: {to}\r\n"
                        f"Call-ID: {cid}\r\n"
                        f"CSeq: {cseq}\r\n"
                        f"Content-Length: 0\r\n\r\n"
                    )
                    sock.sendto(bye_ok.encode("utf-8"), addr)

                    # Terminate matched call transaction
                    for mc_id, tx in list(ACTIVE_SIP_CALLS.items()):
                        if tx.get("call_id_val") == cid:
                            tx["call_active"][0] = False
                            if tx.get("rtp_sock"):
                                try:
                                    tx["rtp_sock"].close()
                                except Exception:
                                    pass
                            ACTIVE_SIP_CALLS.pop(mc_id, None)

                            # Signal barge mixer to finish and save real recording immediately
                            try:
                                from app.api.routes.barge_stream import barge_sessions
                                for s_id in (mc_id, cid, str(cid).split("@")[0]):
                                    if s_id in barge_sessions:
                                        barge_sessions[s_id]["running"] = False
                            except Exception:
                                pass

                            # Brief yield so mixer loop writes the genuine recording file
                            await asyncio.sleep(0.08)

                            from app.api.routes.websockets import global_ws_manager
                            from app.db.database import AsyncSessionLocal
                            from app.db.models import Call

                            async with AsyncSessionLocal() as db:
                                c = await db.get(Call, mc_id)
                                if c:
                                    c.status = "COMPLETED"
                                    end_t = datetime.now(timezone.utc).replace(tzinfo=None)
                                    c.ended_at = end_t
                                    dur = 0
                                    if c.connected_at:
                                        dur = max(1, int((end_t - c.connected_at).total_seconds()))
                                    elif c.initiated_at:
                                        dur = max(1, int((end_t - c.initiated_at).total_seconds()))
                                    c.duration_seconds = dur
                                    if not c.recording_url and dur > 0:
                                        from app.services.recording_service import ensure_call_recording
                                        rec_u = ensure_call_recording(c.id, duration_sec=dur or 15)
                                        if rec_u:
                                            c.recording_url = rec_u
                                    db.add(c)
                                    target_camp = c.campaign_id
                                    if target_camp:
                                        from app.db.models import CampaignLead
                                        from sqlalchemy import select
                                        cl_row = None
                                        if c.id:
                                            cl_row = (await db.execute(select(CampaignLead).where(CampaignLead.call_id == c.id))).scalars().first()
                                        if not cl_row and c.lead_id:
                                            cl_row = (await db.execute(select(CampaignLead).where(CampaignLead.campaign_id == target_camp, CampaignLead.lead_id == c.lead_id))).scalars().first()
                                        if cl_row:
                                            cl_row.status = "COMPLETED"
                                            db.add(cl_row)
                                    await db.commit()
                                    if target_camp:
                                        from app.workers.tasks import trigger_campaign_refill
                                        trigger_campaign_refill(target_camp)

                            await global_ws_manager.broadcast(json.dumps({
                                "event": "CALL_ENDED",
                                "call_id": mc_id,
                                "status": "COMPLETED",
                            }))
                            logger.info(f"[SIP INBOUND] Call {mc_id} ended by remote PBX (BYE).")
                            break
                except Exception as bye_err:
                    logger.warning(f"[SIP DAEMON] Error handling BYE: {bye_err}")

        except asyncio.CancelledError:
            break
        except Exception as err:
            logger.warning(f"[SIP DAEMON] Loop error: {err}")
            await asyncio.sleep(0.5)

    try:
        if sock and sock.fileno() != -1:
            try:
                loop.remove_reader(sock.fileno())
            except Exception:
                pass
            sock.close()
    except Exception:
        pass
    DAEMON_SOCK = None
    DAEMON_RUNNING = False
    logger.info("[SIP DAEMON] Listener stopped.")


def start_sip_daemon():
    """Starts the background SIP daemon task if not already running and enabled."""
    global DAEMON_TASK, DAEMON_RUNNING
    try:
        from dotenv import load_dotenv
        load_dotenv(override=True)
    except Exception:
        pass
    inbound_enabled = os.getenv("SIP_INBOUND_ENABLED", "true").strip().lower()
    sip_enabled = os.getenv("SIP_ENABLED", "true").strip().lower()
    if inbound_enabled in ("false", "0", "no", "disabled") or sip_enabled in ("false", "0", "no", "disabled"):
        logger.info("[SIP DAEMON] Inbound SIP daemon is disabled via configuration (SIP_INBOUND_ENABLED=false or SIP_ENABLED=false).")
        stop_sip_daemon()
        return
    if DAEMON_RUNNING and DAEMON_TASK and not DAEMON_TASK.done():
        return
    DAEMON_TASK = asyncio.create_task(_run_sip_daemon_loop())


def stop_sip_daemon():
    """Stops the background SIP daemon task and unregisters cleanly from Asterisk PBX."""
    global DAEMON_RUNNING, DAEMON_TASK, DAEMON_SOCK, IS_REGISTERED
    was_registered = IS_REGISTERED
    daemon_sock_ref = DAEMON_SOCK
    DAEMON_RUNNING = False
    IS_REGISTERED = False

    # 1. Send unregister (Expires: 0) while socket is still valid
    try:
        if was_registered and daemon_sock_ref and CURRENT_REGISTERED_HOST and CURRENT_REGISTERED_USER:
            host = CURRENT_REGISTERED_HOST
            port = CURRENT_REGISTERED_PORT
            user = CURRENT_REGISTERED_USER
            contact_ip = _get_public_ip()
            listen_port = _get_listen_port()
            unreg = (
                f"REGISTER sip:{host}:{port} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {contact_ip}:{listen_port};branch=z9hG4bK-{uuid.uuid4().hex[:8]};rport\r\n"
                f"Max-Forwards: 70\r\n"
                f"From: <sip:{user}@{host}>;tag={uuid.uuid4().hex[:8]}\r\n"
                f"To: <sip:{user}@{host}>\r\n"
                f"Call-ID: unreg-{uuid.uuid4().hex[:8]}@callzenza\r\n"
                f"CSeq: 1 REGISTER\r\n"
                f"Contact: <sip:{user}@{contact_ip}:{listen_port}>\r\n"
                f"Expires: 0\r\n"
                f"Content-Length: 0\r\n\r\n"
            )
            try:
                _sip_send(unreg, host, port)
                logger.info(f"[SIP DAEMON] Sent specific unregister (Expires: 0) for <sip:{user}@{contact_ip}:{listen_port}> to SIP server.")
            except Exception:
                pass
    except Exception as exc:
        logger.warning(f"[SIP DAEMON] Error sending unregister packet: {exc}")

    # 2. Safely remove reader from loop before closing to avoid WinError 10038
    if DAEMON_SOCK and DAEMON_SOCK.fileno() != -1:
        try:
            loop = asyncio.get_event_loop()
            if loop and not loop.is_closed():
                loop.remove_reader(DAEMON_SOCK.fileno())
        except Exception:
            pass

    # 3. Cancel daemon loop task
    if DAEMON_TASK and not DAEMON_TASK.done():
        try:
            DAEMON_TASK.cancel()
        except Exception:
            pass


