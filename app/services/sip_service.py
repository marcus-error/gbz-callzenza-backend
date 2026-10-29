"""
CallZenza SIP Telephony Service
Handles live socket-level SIP OPTIONS / REGISTER probes, health monitoring,
Asterisk PBX response parsing, and failover route resolution.
"""
from __future__ import annotations

import os
import asyncio
import hashlib
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    CarrierHealthLog,
    CarrierRoutingPolicy,
    DIDNumber,
    SIPCarrier,
    SIPTrunk,
    Campaign,
)

settings = get_settings()


def _get_local_ip() -> str:
    """Best-effort local IP resolution for SIP Via headers."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _parse_sip_response(raw_text: str) -> Tuple[int, str, Optional[str]]:
    """
    Extracts (status_code, reason_phrase, user_agent) from raw SIP response.
    """
    lines = raw_text.splitlines()
    status_code = 0
    reason = "Unknown"
    user_agent = None

    if lines:
        first_line = lines[0].strip()
        m = re.match(r"^SIP/2\.0\s+(\d{3})\s*(.*)$", first_line, re.IGNORECASE)
        if m:
            status_code = int(m.group(1))
            reason = m.group(2).strip()

    for line in lines[1:]:
        line_lower = line.lower().strip()
        if line_lower.startswith("user-agent:") or line_lower.startswith("server:"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                user_agent = parts[1].strip()
                break

    return status_code, reason, user_agent


def _sync_sip_options_probe(
    host: str,
    port: int = 5060,
    transport: str = "UDP",
    timeout: float = 4.0
) -> Dict[str, Any]:
    """
    Synchronous socket probe sending an RFC 3261 OPTIONS request.
    Designed to run in asyncio.to_thread() to avoid blocking async loop.
    """
    start_t = time.perf_counter()
    local_ip = _get_local_ip()

    # Sanitize host and extract port if embedded
    if host:
        if host.startswith("sip:"):
            host = host[4:].strip()
        if ":" in host:
            h_parts = host.split(":", 1)
            host = h_parts[0].strip()
            try:
                port = int(h_parts[1].strip())
            except Exception:
                pass
    branch = f"z9hG4bK-{uuid.uuid4().hex[:10]}"
    call_id = f"ping-{uuid.uuid4().hex[:16]}@{local_ip}"
    tag = uuid.uuid4().hex[:8]

    req = (
        f"OPTIONS sip:{host}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/{transport.upper()} {local_ip}:{port};branch={branch};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:ping@{local_ip}>;tag={tag}\r\n"
        f"To: <sip:{host}:{port}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Contact: <sip:ping@{local_ip}:{port}>\r\n"
        f"Accept: application/sdp\r\n"
        f"User-Agent: CallZenza-SIP-Monitor/2.0\r\n"
        f"Content-Length: 0\r\n\r\n"
    )

    is_tcp = transport.upper() == "TCP"

    try:
        if is_tcp:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.sendall(req.encode("utf-8"))
            raw_data = sock.recv(4096)
            sock.close()
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.sendto(req.encode("utf-8"), (host, port))
            raw_data, _ = sock.recvfrom(4096)
            sock.close()

        latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
        resp_text = raw_data.decode("utf-8", errors="ignore")
        code, reason, user_agent = _parse_sip_response(resp_text)

        is_healthy = code in (200, 401, 407, 404, 403, 405, 484, 486, 503)

        return {
            "success": is_healthy,
            "status": "HEALTHY" if is_healthy else "DEGRADED",
            "latency_ms": latency_ms,
            "sip_code": code,
            "sip_reason": reason,
            "user_agent": user_agent or "SIP Endpoint",
            "error": None if is_healthy else f"Unexpected SIP status {code} {reason}",
            "raw_first_line": resp_text.splitlines()[0] if resp_text else ""
        }

    except socket.timeout:
        latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
        return {
            "success": False,
            "status": "UNREACHABLE",
            "latency_ms": latency_ms,
            "sip_code": None,
            "sip_reason": "Timeout",
            "user_agent": None,
            "error": f"Connection timed out after {timeout:.1f}s — SIP host {host}:{port} did not reply.",
            "raw_first_line": ""
        }
    except Exception as ex:
        latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
        return {
            "success": False,
            "status": "UNREACHABLE",
            "latency_ms": latency_ms,
            "sip_code": None,
            "sip_reason": "Socket Error",
            "user_agent": None,
            "error": str(ex),
            "raw_first_line": ""
        }


def _sync_sip_register_probe(
    host: str,
    port: int = 5060,
    username: str = "",
    password: str = "",
    timeout: float = 5.0
) -> Dict[str, Any]:
    """
    Sends SIP REGISTER with MD5 digest challenge handling.
    """
    public_ip = getattr(settings, "sip_rtp_ip", None) or getattr(settings, "sip_local_ip", None) or _get_local_ip()
    listen_port = int(os.getenv("SIP_LISTEN_PORT", "5062"))
    start_t = time.perf_counter()
    branch1 = f"z9hG4bK-{uuid.uuid4().hex[:10]}"
    call_id = f"reg-{uuid.uuid4().hex[:16]}@{host}"
    tag1 = f"tag-{uuid.uuid4().hex[:8]}"

    req1 = (
        f"REGISTER sip:{host}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {public_ip}:{listen_port};branch={branch1};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:{username}@{host}>;tag={tag1}\r\n"
        f"To: <sip:{username}@{host}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 REGISTER\r\n"
        f"Contact: <sip:{username}@{public_ip}:{listen_port}>\r\n"
        f"Expires: 120\r\n"
        f"Content-Length: 0\r\n\r\n"
    )

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(req1.encode("utf-8"), (host, port))
        resp1 = sock.recvfrom(4096)[0].decode("utf-8", errors="ignore")

        code1, _, ua1 = _parse_sip_response(resp1)

        if code1 == 200:
            sock.close()
            latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
            return {
                "success": True,
                "status": "HEALTHY",
                "latency_ms": latency_ms,
                "sip_code": 200,
                "sip_reason": "OK (Registered)",
                "user_agent": ua1 or "Asterisk/PBX",
                "error": None
            }

        if code1 == 401:
            realm_m = re.search(r'realm="([^"]+)"', resp1, re.IGNORECASE)
            nonce_m = re.search(r'nonce="([^"]+)"', resp1, re.IGNORECASE)
            qop_m = re.search(r'qop="?([^",\r\n]+)"?', resp1, re.IGNORECASE)
            opaque_m = re.search(r'opaque="([^"]+)"', resp1, re.IGNORECASE)

            if realm_m and nonce_m and password:
                realm = realm_m.group(1)
                nonce = nonce_m.group(1)
                uri = f"sip:{host}:{port}"
                ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode("utf-8")).hexdigest()
                ha2 = hashlib.md5(f"REGISTER:{uri}".encode("utf-8")).hexdigest()
                nc = "00000001"
                cnonce = uuid.uuid4().hex[:8]

                if qop_m and "auth" in qop_m.group(1):
                    resp_hash = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode("utf-8")).hexdigest()
                    auth = f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp_hash}", algorithm=MD5, cnonce="{cnonce}", qop=auth, nc={nc}'
                else:
                    resp_hash = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()
                    auth = f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp_hash}", algorithm=MD5'

                if opaque_m:
                    auth += f', opaque="{opaque_m.group(1)}"'

                branch2 = f"z9hG4bK-{uuid.uuid4().hex[:10]}"
                req2 = (
                    f"REGISTER sip:{host}:{port} SIP/2.0\r\n"
                    f"Via: SIP/2.0/UDP {public_ip}:{listen_port};branch={branch2};rport\r\n"
                    f"Max-Forwards: 70\r\n"
                    f"From: <sip:{username}@{host}>;tag={tag1}\r\n"
                    f"To: <sip:{username}@{host}>\r\n"
                    f"Call-ID: {call_id}\r\n"
                    f"CSeq: 2 REGISTER\r\n"
                    f"Contact: <sip:{username}@{public_ip}:{listen_port}>\r\n"
                    f"Authorization: {auth}\r\n"
                    f"Expires: 120\r\n"
                    f"Content-Length: 0\r\n\r\n"
                )
                sock.sendto(req2.encode("utf-8"), (host, port))
                resp2 = sock.recvfrom(4096)[0].decode("utf-8", errors="ignore")
                sock.close()

                latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
                code2, reason2, ua2 = _parse_sip_response(resp2)
                is_ok = code2 in (200, 204)
                return {
                    "success": is_ok,
                    "status": "HEALTHY" if is_ok else ("DEGRADED" if code2 == 403 else "HEALTHY"),
                    "latency_ms": latency_ms,
                    "sip_code": code2,
                    "sip_reason": reason2,
                    "user_agent": ua2 or ua1 or "Asterisk/PBX",
                    "error": None if is_ok else f"Registration response: SIP {code2} {reason2}"
                }

        sock.close()
        latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
        return {
            "success": True,
            "status": "HEALTHY",
            "latency_ms": latency_ms,
            "sip_code": code1,
            "sip_reason": "Challenged",
            "user_agent": ua1 or "SIP Endpoint",
            "error": None
        }

    except Exception as ex:
        latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
        return {
            "success": False,
            "status": "UNREACHABLE",
            "latency_ms": latency_ms,
            "sip_code": None,
            "sip_reason": "Error",
            "user_agent": None,
            "error": str(ex)
        }


class SIPService:
    @staticmethod
    async def test_carrier_connection(carrier: SIPCarrier) -> Dict[str, Any]:
        """
        Executes real socket test against carrier's SIP host and port.
        Uses REGISTER if credentials exist, otherwise OPTIONS probe.
        """
        host = carrier.sip_host
        port = carrier.sip_port or 5060
        transport = carrier.transport or "UDP"

        if carrier.auth_type == "CREDENTIAL" and carrier.username and carrier.password_hash:
            res = await asyncio.to_thread(
                _sync_sip_register_probe,
                host=host,
                port=port,
                username=carrier.username,
                password=carrier.password_hash,
                timeout=4.0
            )
        else:
            res = await asyncio.to_thread(
                _sync_sip_options_probe,
                host=host,
                port=port,
                transport=transport,
                timeout=4.0
            )

        return res

    @staticmethod
    async def record_health_result(
        db: AsyncSession,
        carrier_id: str,
        probe_result: Dict[str, Any]
    ) -> CarrierHealthLog:
        """
        Persists a health log entry and updates SIPCarrier status fields.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        health_log = CarrierHealthLog(
            carrier_id=carrier_id,
            check_type="SIP_REGISTER" if probe_result.get("sip_reason", "").startswith("OK (Reg") else "SIP_OPTIONS",
            status=probe_result.get("status", "UNKNOWN"),
            latency_ms=probe_result.get("latency_ms", 0.0),
            sip_code=probe_result.get("sip_code"),
            user_agent=probe_result.get("user_agent"),
            error_message=probe_result.get("error"),
            checked_at=now
        )
        db.add(health_log)

        carrier = await db.get(SIPCarrier, carrier_id)
        if carrier:
            carrier.last_health_status = probe_result.get("status", "UNKNOWN")
            carrier.last_health_latency_ms = probe_result.get("latency_ms")
            carrier.last_health_checked_at = now
            carrier.last_error_message = probe_result.get("error")
            db.add(carrier)

        await db.commit()
        return health_log

    @staticmethod
    async def resolve_outbound_carrier_trunk(
        db: AsyncSession,
        campaign_id: Optional[str] = None,
        tenant_id: Optional[str] = None
    ) -> Tuple[Optional[SIPCarrier], Optional[SIPTrunk], Optional[str]]:
        """
        Resolves the primary carrier, trunk, and caller ID DID for an outbound call.
        Falls back to secondary carrier if primary has recent failures.
        """
        if campaign_id:
            camp = await db.get(Campaign, campaign_id)
            if camp:
                carrier = await db.get(SIPCarrier, camp.carrier_id) if camp.carrier_id else None
                trunk = await db.get(SIPTrunk, camp.trunk_id) if camp.trunk_id else None
                did = await db.get(DIDNumber, camp.caller_did_id) if camp.caller_did_id else None
                
                caller_phone = did.phone_number if did else (camp.twilio_number or "")
                if carrier and carrier.status == "ACTIVE":
                    return carrier, trunk, caller_phone

        if tenant_id:
            stmt = select(CarrierRoutingPolicy).where(
                CarrierRoutingPolicy.business_id == tenant_id
            ).order_by(CarrierRoutingPolicy.created_at.desc())
            policy = (await db.scalars(stmt)).first()
            if policy:
                primary = await db.get(SIPCarrier, policy.primary_carrier_id)
                if primary and primary.status == "ACTIVE" and primary.last_health_status != "UNREACHABLE":
                    trunk_stmt = select(SIPTrunk).where(
                        SIPTrunk.carrier_id == primary.id,
                        SIPTrunk.status == "ACTIVE"
                    ).order_by(SIPTrunk.priority.asc())
                    trunk = (await db.scalars(trunk_stmt)).first()
                    return primary, trunk, None
                
                if policy.secondary_carrier_id:
                    secondary = await db.get(SIPCarrier, policy.secondary_carrier_id)
                    if secondary and secondary.status == "ACTIVE":
                        trunk_stmt = select(SIPTrunk).where(
                            SIPTrunk.carrier_id == secondary.id,
                            SIPTrunk.status == "ACTIVE"
                        ).order_by(SIPTrunk.priority.asc())
                        trunk = (await db.scalars(trunk_stmt)).first()
                        return secondary, trunk, None

        def_stmt = select(SIPCarrier).where(
            SIPCarrier.status == "ACTIVE",
            SIPCarrier.outbound_enabled == True
        ).order_by(SIPCarrier.created_at.asc()).limit(1)
        carrier = (await db.scalars(def_stmt)).first()
        if carrier:
            trunk_stmt = select(SIPTrunk).where(
                SIPTrunk.carrier_id == carrier.id,
                SIPTrunk.status == "ACTIVE"
            ).order_by(SIPTrunk.priority.asc())
            trunk = (await db.scalars(trunk_stmt)).first()
            return carrier, trunk, None

        return None, None, None


sip_service = SIPService()
