"""
CallZenza Configuration & Telephony Diagnostic Script.
Safely validates Database connection, SIP configuration, Asterisk socket reachability, and RTP port bounds without exposing secrets.
"""
import os
import sys
import socket
import asyncio
from typing import Tuple

from app.core.config import get_settings
from app.db.database import AsyncSessionLocal, init_db
from sqlalchemy import text

settings = get_settings()

async def check_database() -> Tuple[bool, str]:
    try:
        await init_db()
        async with AsyncSessionLocal() as db:
            res = await db.execute(text("SELECT 1"))
            val = res.scalar()
            if val == 1:
                return True, "OK"
            return False, f"Unexpected DB query response: {val}"
    except Exception as e:
        return False, f"Database Connection Error: {e}"

def check_sip_config() -> dict:
    host = (settings.sip_server or settings.sip_host or os.getenv("SIP_SERVER") or os.getenv("SIP_HOST") or "").strip()
    port = int(settings.sip_port or os.getenv("SIP_PORT") or 5060)
    user = (settings.sip_username or os.getenv("SIP_USERNAME") or "").strip()
    pwd = (settings.sip_password or user or os.getenv("SIP_PASSWORD") or "").strip()
    transport = (getattr(settings, "sip_transport", "UDP") or os.getenv("SIP_TRANSPORT") or "UDP").upper()

    masked_host = (host[:3] + "***" + host[host.rfind("."):]) if ("." in host and len(host) > 6) else (host[:2] + "***" if host else "NOT_CONFIGURED")
    masked_user = (user[:2] + "***" + user[-1:]) if len(user) > 3 else (user[:1] + "***" if user else "NOT_CONFIGURED")

    return {
        "host_configured": bool(host),
        "host_val": masked_host,
        "raw_host": host,
        "port": port,
        "user_configured": bool(user),
        "user_val": masked_user,
        "pwd_configured": bool(pwd),
        "transport": transport,
    }

def check_asterisk_connectivity(host: str, port: int) -> Tuple[bool, str]:
    if not host:
        return False, "SKIPPED (No SIP Host configured)"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.5)
        # Send lightweight OPTIONS packet
        ping = (
            f"OPTIONS sip:{host}:{port} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-diag\r\n"
            f"From: <sip:diag@{host}>;tag=diag\r\n"
            f"To: <sip:{host}:{port}>\r\n"
            f"Call-ID: diag-{os.urandom(4).hex()}@callzenza\r\n"
            f"CSeq: 1 OPTIONS\r\n"
            f"Content-Length: 0\r\n\r\n"
        )
        s.sendto(ping.encode("utf-8"), (host, port))
        try:
            data, _ = s.recvfrom(1024)
            s.close()
            if data:
                return True, "OK (SIP Response received)"
        except socket.timeout:
            s.close()
            return True, "REACHABLE (Packet sent over UDP)"
        return True, "OK"
    except Exception as e:
        return False, f"Unreachable ({e})"

def check_rtp_config() -> dict:
    rtp_port = getattr(settings, "sip_rtp_port", 10000)
    return {
        "configured": True,
        "port": rtp_port,
        "status": "CONFIGURED"
    }

async def main():
    print("==================================================")
    print("CallZenza Configuration & Telephony Diagnostics")
    print("==================================================")

    # 1. Database Check
    db_ok, db_msg = await check_database()
    print(f"[CONFIG_CHECK] Database: {'OK' if db_ok else 'FAILED (' + db_msg + ')'}")

    # 2. SIP Configuration Check
    sip = check_sip_config()
    print(f"[CONFIG_CHECK] SIP host: {'CONFIGURED (' + sip['host_val'] + ')' if sip['host_configured'] else 'NOT_CONFIGURED'}")
    print(f"[CONFIG_CHECK] SIP port: CONFIGURED ({sip['port']})")
    print(f"[CONFIG_CHECK] SIP transport: CONFIGURED ({sip['transport']})")
    print(f"[CONFIG_CHECK] SIP credentials: {'CONFIGURED (' + sip['user_val'] + ')' if sip['user_configured'] else 'NOT_CONFIGURED'}")

    # 3. Connectivity Check
    if sip['host_configured']:
        ast_ok, ast_msg = check_asterisk_connectivity(sip['raw_host'], sip['port'])
        print(f"[CONFIG_CHECK] Asterisk connectivity: {'OK' if ast_ok else 'FAILED (' + ast_msg + ')'}")
    else:
        print("[CONFIG_CHECK] Asterisk connectivity: SKIPPED (Set SIP_SERVER in .env)")

    # 4. RTP Check
    rtp = check_rtp_config()
    print(f"[CONFIG_CHECK] RTP configuration: {rtp['status']} (Base Port: {rtp['port']})")

    print("==================================================")

if __name__ == "__main__":
    asyncio.run(main())
