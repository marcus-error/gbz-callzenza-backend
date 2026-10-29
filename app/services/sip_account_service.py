"""SIP Account Management, Credential Security, and Connectivity Service."""
import asyncio
import base64
import hashlib
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any

from loguru import logger
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import SIPAccount, Campaign, Call


def _get_fernet():
    from cryptography.fernet import Fernet
    settings = get_settings()
    # Derive a deterministic 32-byte Fernet key from secret_key using SHA-256
    derived = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    encoded = base64.urlsafe_b64encode(derived)
    return Fernet(encoded)


def encrypt_sip_secret(secret: str) -> str:
    """Encrypts plaintext SIP secret using AES-128-CBC/Fernet."""
    if not secret:
        return ""
    try:
        f = _get_fernet()
        return f.encrypt(secret.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"[SIP_SECURITY] Failed to encrypt secret: {e}")
        return secret


def decrypt_sip_secret(encrypted_secret: str) -> str:
    """Decrypts encrypted SIP secret at runtime for Asterisk/PJSIP."""
    if not encrypted_secret:
        return ""
    try:
        f = _get_fernet()
        return f.decrypt(encrypted_secret.encode("utf-8")).decode("utf-8")
    except Exception:
        # Fallback in case raw plaintext was stored or different key was used
        return encrypted_secret


async def probe_sip_connectivity(
    host: str,
    port: int = 5060,
    transport: str = "udp",
    timeout: float = 3.5,
) -> Dict[str, Any]:
    """Probes SIP host and port reachability via DNS resolution and transport socket probe.
    
    Returns:
        dict with keys: 'reachable' (bool), 'latency_ms' (float), 'error' (str or None),
        'dns_resolved_ip' (str or None), 'transport' (str)
    """
    start_time = time.perf_counter()
    transport_lower = (transport or "udp").strip().lower()

    # Sanitize host and extract port if embedded (e.g. host:port or sip:host:port)
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

    # Step 1: DNS Resolution
    loop = asyncio.get_running_loop()
    try:
        addrinfo = await loop.getaddrinfo(clean_host, port, family=socket.AF_INET)
        resolved_ip = addrinfo[0][4][0] if addrinfo else None
    except Exception as dns_err:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "reachable": False,
            "latency_ms": latency_ms,
            "error": f"DNS resolution failed for '{host}': {str(dns_err)}",
            "dns_resolved_ip": None,
            "transport": transport_lower,
        }

    # Step 2: Socket Probe
    try:
        if transport_lower in ("tcp", "tls"):
            # TCP/TLS connect probe
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(resolved_ip, port),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            return {
                "reachable": True,
                "latency_ms": latency_ms,
                "error": None,
                "error_code": None,
                "dns_resolved_ip": resolved_ip,
                "transport": transport_lower,
                "check_type": f"{transport_lower.upper()}_CONNECT",
                "sip_status_code": None,
                "sip_status_reason": None,
            }
        else:
            # UDP SIP OPTIONS probe
            call_id = f"{uuid.uuid4().hex[:12]}@callzenza.local"
            branch = f"z9hG4bK.{uuid.uuid4().hex[:10]}"
            tag = uuid.uuid4().hex[:8]
            sip_options = (
                f"OPTIONS sip:{host}:{port} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP 127.0.0.1:5060;branch={branch};rport\r\n"
                f"Max-Forwards: 70\r\n"
                f"To: <sip:{host}:{port}>\r\n"
                f"From: <sip:ping@callzenza.local>;tag={tag}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: 1 OPTIONS\r\n"
                f"Contact: <sip:ping@127.0.0.1:5060>\r\n"
                f"Accept: application/sdp\r\n"
                f"User-Agent: CallZenza-SIP-Probe/2.0\r\n"
                f"Content-Length: 0\r\n\r\n"
            )

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)

            # Send packet
            await loop.sock_sendto(sock, sip_options.encode("utf-8"), (resolved_ip, port))

            # Attempt to read response from SIP server
            sip_code = None
            sip_reason = None
            try:
                data = await asyncio.wait_for(loop.sock_recv(sock, 2048), timeout=min(timeout, 2.5))
                resp_text = data.decode("utf-8", errors="ignore")
                first_line = resp_text.splitlines()[0] if resp_text else ""
                if "SIP/2.0" in first_line:
                    parts = first_line.split(" ", 2)
                    if len(parts) >= 2 and parts[1].isdigit():
                        sip_code = int(parts[1])
                        sip_reason = parts[2].strip() if len(parts) > 2 else ""
                latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
                return {
                    "reachable": True,
                    "latency_ms": latency_ms,
                    "error": None,
                    "error_code": None,
                    "dns_resolved_ip": resolved_ip,
                    "transport": "udp",
                    "check_type": "SIP_OPTIONS",
                    "sip_status_code": sip_code,
                    "sip_status_reason": sip_reason or "Response Received",
                }
            except (asyncio.TimeoutError, TimeoutError):
                latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
                return {
                    "reachable": False,
                    "latency_ms": latency_ms,
                    "error": f"SIP OPTIONS probe timed out after {min(timeout, 2.5)}s to {host}:{port}. Host is unreachable or UDP 5060 is filtered.",
                    "error_code": "SIP_OPTIONS_TIMEOUT",
                    "dns_resolved_ip": resolved_ip,
                    "transport": "udp",
                    "check_type": "SIP_OPTIONS",
                    "sip_status_code": None,
                    "sip_status_reason": None,
                }
            finally:
                sock.close()

    except asyncio.TimeoutError:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "reachable": False,
            "latency_ms": latency_ms,
            "error": f"Connection timed out after {timeout}s to {host}:{port}",
            "error_code": "NETWORK_TIMEOUT",
            "dns_resolved_ip": resolved_ip,
            "transport": transport_lower,
            "check_type": f"{transport_lower.upper()}_PROBE",
            "sip_status_code": None,
            "sip_status_reason": None,
        }
    except Exception as e:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "reachable": False,
            "latency_ms": latency_ms,
            "error": f"Socket probe error: {str(e)}",
            "error_code": "SOCKET_ERROR",
            "dns_resolved_ip": resolved_ip,
            "transport": transport_lower,
            "check_type": f"{transport_lower.upper()}_PROBE",
            "sip_status_code": None,
            "sip_status_reason": None,
        }


async def test_and_update_sip_account(
    db: AsyncSession,
    account: SIPAccount,
    timeout: float = 3.5,
) -> Dict[str, Any]:
    """Tests connectivity for a SIP account, updates its health in DB, and returns probe results."""
    probe_result = await probe_sip_connectivity(
        host=account.host,
        port=account.port,
        transport=account.transport,
        timeout=timeout,
    )

    now = datetime.now(timezone.utc)
    account.last_health_checked_at = now
    account.last_health_latency_ms = probe_result.get("latency_ms")

    current_settings = dict(account.settings) if isinstance(account.settings, dict) else {}
    if probe_result["reachable"]:
        account.last_health_status = "healthy"
        account.last_error_message = None
        code = probe_result.get("sip_status_code")
        reason = probe_result.get("sip_status_reason")
        check_t = probe_result.get("check_type", "SIP_OPTIONS")

        # Do not set registration_status to "registered" merely on UDP ping
        if account.direction and account.direction.lower() == "outbound":
            if account.registration_status in ("unreachable", "error", "pending"):
                account.registration_status = "outbound_ready"
        elif account.registration_status in ("unreachable", "error"):
            account.registration_status = "unregistered"

        resp_info = f" (SIP {code} {reason})" if code else ""
        diag = {
            "status": account.registration_status,
            "error_code": None,
            "message": f"SIP host {account.host}:{account.port} is reachable via {check_t}{resp_info}.",
            "sip_status_code": code,
            "sip_status_reason": reason,
            "check_type": check_t,
            "retryable": False,
            "suggested_action": "Host is reachable; verify credentials or initiate registration if required by provider.",
            "last_checked_at": now.isoformat(),
        }
    else:
        account.last_health_status = "unreachable"
        account.last_error_message = probe_result.get("error")
        err_lower = (probe_result.get("error") or "").lower()
        if "dns" in err_lower:
            account.registration_status = "dns_error"
            diag = {
                "status": "dns_error",
                "error_code": "DNS_RESOLUTION_FAILED",
                "message": f"DNS resolution failed for '{account.host}'.",
                "sip_status_code": None,
                "sip_status_reason": None,
                "check_type": probe_result.get("check_type") or "DNS",
                "retryable": True,
                "suggested_action": "Check the configured host and DNS/network connectivity.",
                "last_checked_at": now.isoformat(),
            }
        else:
            account.registration_status = "network_error"
            diag = {
                "status": "network_error",
                "error_code": probe_result.get("error_code") or "SIP_OPTIONS_TIMEOUT",
                "message": probe_result.get("error") or "Network timeout reaching SIP host.",
                "sip_status_code": None,
                "sip_status_reason": None,
                "check_type": probe_result.get("check_type") or "SIP_OPTIONS",
                "retryable": True,
                "suggested_action": "Verify that the SIP server address and port are reachable and that firewall rules permit outbound UDP/TCP 5060.",
                "last_checked_at": now.isoformat(),
            }

    current_settings["registration_diagnostics"] = diag
    account.settings = current_settings

    db.add(account)
    await db.commit()
    await db.refresh(account)

    return probe_result



def make_sip_diagnostic_error(
    status: str,
    error_code: str,
    message: str,
    suggested_action: str,
    retryable: bool,
    error: Optional[str] = None,
    sip_status_code: Optional[int] = None,
) -> Dict[str, Any]:
    """Helper to return consistent structured SIP diagnostic information."""
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "success": False,
        "status": status,
        "error_code": error_code,
        "message": message,
        "error": error or message,
        "sip_status_code": sip_status_code,
        "retryable": retryable,
        "suggested_action": suggested_action,
        "last_checked_at": now_iso,
    }


def classify_sip_registration_response(
    status_code: Optional[int],
    first_line: str,
    raw_message: str = "",
    is_authenticated_retry: bool = False,
    clean_host: str = "",
    port: int = 5060,
) -> Dict[str, Any]:
    """
    Classifies a SIP response into safe, structured diagnostic metadata:
    - 401/407 on initial request: Authentication challenge received; digest authentication required.
    - 401/407 after authenticated retry: Authentication retry rejected.
    - 403 Forbidden: SIP server explicitly denied registration.
    - 404 Not Found: Requested SIP resource or account was not found.
    - 408 Request Timeout: SIP request timed out.
    - Unsupported digest algorithm/qop: Challenge uses unsupported method.
    - Stale nonce: Nonce expired (stale=true).
    - 200 OK: Registration succeeded.
    - 405/501: Method not allowed / not implemented (Static Trunk).
    - Other codes: Preserves actual response status and sanitized reason phrase.
    """
    import re
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. 200 OK
    if status_code == 200 or "200 OK" in first_line:
        msg = (
            "SIP registration successful (200 OK after digest challenge)."
            if is_authenticated_retry
            else "SIP registration successful (200 OK)."
        )
        return {
            "success": True,
            "status": "registered",
            "error_code": None,
            "message": msg,
            "error": None,
            "sip_status_code": 200,
            "retryable": False,
            "suggested_action": "Account is registered and active for inbound and outbound calls.",
            "last_checked_at": now_iso,
        }

    # 2. Static Trunk (405 Method Not Allowed / 501 Not Implemented)
    if status_code in (405, 501) or "405" in first_line or "501" in first_line:
        code = status_code or (405 if "405" in first_line else 501)
        return {
            "success": True,
            "status": "not_required",
            "error_code": None,
            "message": "Static Trunk: SIP REGISTER not required by provider.",
            "error": None,
            "sip_status_code": code,
            "retryable": False,
            "suggested_action": "Trunk is configured for static IP routing; REGISTER is not required.",
            "last_checked_at": now_iso,
        }

    # 3. 401 Unauthorized / 407 Proxy Authentication Required
    if status_code in (401, 407) or "401" in first_line or "407" in first_line:
        code = status_code or (401 if "401" in first_line else 407)
        # Check for unsupported algorithm in challenge
        algo_m = re.search(r'algorithm="?([^",\r\n]+)"?', raw_message, re.IGNORECASE)
        algo = algo_m.group(1).upper() if algo_m else "MD5"
        if algo not in ("MD5", "MD5-SESS"):
            return {
                "success": False,
                "status": "auth_failed",
                "error_code": "UNSUPPORTED_DIGEST_ALGORITHM",
                "message": (
                    f"Registration failed: the server requested an unsupported authentication algorithm ({algo}). "
                    "Only MD5 is currently supported."
                ),
                "error": "Registration failed: the server requested an authentication method or algorithm that this SIP client does not currently support.",
                "sip_status_code": code,
                "retryable": False,
                "suggested_action": "Configure your SIP provider or PBX to use standard MD5 digest authentication.",
                "last_checked_at": now_iso,
            }

        # Check for stale nonce
        stale_m = re.search(r'stale="?([^",\r\n]+)"?', raw_message, re.IGNORECASE)
        is_stale = bool(stale_m and stale_m.group(1).lower() in ("true", "yes", "1"))
        if is_stale:
            return {
                "success": False,
                "status": "auth_failed",
                "error_code": "STALE_NONCE",
                "message": "Registration failed: the SIP server indicates the authentication nonce has expired (stale=true).",
                "error": "Registration failed: the SIP server indicates the authentication nonce has expired (stale=true).",
                "sip_status_code": code,
                "retryable": True,
                "suggested_action": "Retry registration to obtain a fresh nonce from the SIP server.",
                "last_checked_at": now_iso,
            }

        if is_authenticated_retry:
            # Repeated 401/407 on authenticated retry
            return {
                "success": False,
                "status": "auth_failed",
                "error_code": "AUTH_REJECTED",
                "message": (
                    f"Registration failed: the SIP server rejected the authenticated REGISTER request ({code} Unauthorized). "
                    "Verify the SIP authentication username and password, and confirm the provider's digest authentication requirements. "
                    "The exact rejection cause has not been confirmed."
                ),
                "error": (
                    f"Registration failed: the SIP server rejected the authenticated REGISTER request ({code} Unauthorized). "
                    "Invalid username or password, digest configuration, or provider rejection."
                ),
                "sip_status_code": code,
                "retryable": False,
                "suggested_action": "Verify the SIP authentication username and password, and confirm the provider's digest authentication requirements.",
                "last_checked_at": now_iso,
            }
        else:
            # Initial 401/407 challenge
            return {
                "success": False,
                "status": "registering",
                "error_code": "AUTH_CHALLENGE_RECEIVED",
                "message": f"SIP server issued authentication challenge ({code} Unauthorized). Digest authentication is required.",
                "error": f"SIP server issued authentication challenge ({code} Unauthorized).",
                "sip_status_code": code,
                "retryable": True,
                "suggested_action": "Sending digest authentication response to registrar.",
                "last_checked_at": now_iso,
            }

    # 4. 403 Forbidden
    if status_code == 403 or "403" in first_line:
        return {
            "success": False,
            "status": "auth_failed",
            "error_code": "FORBIDDEN",
            "message": (
                "Registration rejected: the SIP server explicitly denied registration (403 Forbidden). "
                "The account may not be authorized, the IP address may not be whitelisted, or the trunk direction may be invalid."
            ),
            "error": "SIP registration rejected: 403 Forbidden.",
            "sip_status_code": 403,
            "retryable": False,
            "suggested_action": "Check with your SIP provider to ensure your account is active, authorized for registration, and that server IP whitelisting is configured.",
            "last_checked_at": now_iso,
        }

    # 5. 404 Not Found
    if status_code == 404 or "404" in first_line:
        return {
            "success": False,
            "status": "error",
            "error_code": "NOT_FOUND",
            "message": "Registration failed: requested SIP resource or user account was not found (404 Not Found).",
            "error": "Registration failed: requested SIP user or domain not found (404 Not Found).",
            "sip_status_code": 404,
            "retryable": False,
            "suggested_action": "Confirm the SIP username, domain, and registrar host with your VoIP provider.",
            "last_checked_at": now_iso,
        }

    # 6. 408 Request Timeout
    if status_code == 408 or "408" in first_line:
        return {
            "success": False,
            "status": "network_error",
            "error_code": "REQUEST_TIMEOUT",
            "message": "Registration timed out: the SIP server responded with a request timeout (408 Request Timeout).",
            "error": "Registration timed out (408 Request Timeout).",
            "sip_status_code": 408,
            "retryable": True,
            "suggested_action": "Check network latency, packet loss, or server load, and retry.",
            "last_checked_at": now_iso,
        }

    # 7. Other response status codes
    code = status_code
    if not code:
        code_m = re.search(r'SIP/2\.0\s+(\d{3})', first_line)
        if code_m:
            code = int(code_m.group(1))

    return {
        "success": False,
        "status": "error",
        "error_code": "UNKNOWN_SIP_ERROR",
        "message": f"Registration failed: SIP server returned unexpected response: {first_line}",
        "error": f"SIP server returned: {first_line}",
        "sip_status_code": code,
        "retryable": bool(code and code >= 500),
        "suggested_action": f"Check SIP server logs or contact your VoIP provider regarding response code {first_line}.",
        "last_checked_at": now_iso,
    }


def _perform_sip_register_probe(
    host: str,
    port: int,
    username: str,
    password: str,
    auth_username: Optional[str] = None,
    transport: str = "udp",
    timeout: float = 3.5,
) -> Dict[str, Any]:
    """Synchronous socket probe executing an RFC 3261 SIP REGISTER transaction with digest auth."""
    import re
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

    placeholder_hosts = {"host", "test", "dummy", "placeholder", "example", "none", "localhost", "127.0.0.1"}
    if clean_host.lower() in placeholder_hosts:
        return make_sip_diagnostic_error(
            status="not_configured",
            error_code="NOT_CONFIGURED",
            message=f"SIP host '{host}' is a placeholder or not a configured destination. Enter a valid VoIP provider or PBX domain/IP.",
            suggested_action="Enter a valid SIP hostname or IP address in the account configuration.",
            retryable=False,
            error=f"SIP host '{host}' is a placeholder or not a configured destination.",
        )

    # Step 1: DNS Resolution
    try:
        addrinfo = socket.getaddrinfo(clean_host, port, family=socket.AF_INET)
        resolved_ip = addrinfo[0][4][0] if addrinfo else None
    except socket.gaierror as dns_err:
        return make_sip_diagnostic_error(
            status="dns_error",
            error_code="DNS_RESOLUTION_FAILED",
            message="Registration failed: the SIP server hostname could not be resolved. Check the configured host and DNS/network connectivity.",
            suggested_action="Check the configured hostname or verify network DNS settings.",
            retryable=True,
            error=f"DNS resolution failed for '{clean_host}': {dns_err}",
        )
    except Exception as exc:
        return make_sip_diagnostic_error(
            status="dns_error",
            error_code="DNS_RESOLUTION_FAILED",
            message=f"Host resolution error for '{clean_host}': {exc}",
            suggested_action="Verify network connectivity and hostname format.",
            retryable=True,
            error=f"Host resolution error for '{clean_host}': {exc}",
        )

    transport_lower = (transport or "udp").strip().lower()
    if transport_lower in ("tcp", "tls"):
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((resolved_ip, port))
            sock.close()
            return {
                "success": True,
                "status": "outbound_ready",
                "error_code": None,
                "error": None,
                "message": f"TCP connection to {clean_host}:{port} established.",
                "sip_status_code": None,
                "retryable": False,
                "suggested_action": "TCP socket is reachable. Outbound routing ready.",
                "last_checked_at": now_iso,
            }
        except socket.timeout:
            return make_sip_diagnostic_error(
                status="network_error",
                error_code="NETWORK_TIMEOUT",
                message=f"TCP timeout connecting to {clean_host}:{port}.",
                suggested_action="Verify that the SIP server port is open and reachable over TCP.",
                retryable=True,
                error=f"TCP timeout connecting to {clean_host}:{port}",
            )
        except Exception as exc:
            return make_sip_diagnostic_error(
                status="network_error",
                error_code="NETWORK_ERROR",
                message=f"TCP connection error to {clean_host}:{port}: {exc}",
                suggested_action="Check network firewall and TCP port accessibility.",
                retryable=True,
                error=f"TCP connection error to {clean_host}:{port}: {exc}",
            )

    from app.services.sip_inbound_daemon import (
        _get_public_ip,
        build_register_packet,
        compute_digest_auth,
    )

    contact_ip = _get_public_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.bind(("0.0.0.0", 0))
    except Exception:
        pass
    listen_port = sock.getsockname()[1]

    call_id = f"reg-probe-{uuid.uuid4().hex[:12]}@callzenza"
    tag = uuid.uuid4().hex[:8]
    cseq = 1

    req1 = build_register_packet(clean_host, port, username, contact_ip, listen_port, call_id, tag, cseq_num=cseq)
    try:
        sock.sendto(req1.encode("utf-8"), (resolved_ip, port))
        resp_bytes, _ = sock.recvfrom(4096)
    except socket.timeout:
        sock.close()
        return make_sip_diagnostic_error(
            status="network_error",
            error_code="NETWORK_TIMEOUT",
            message="Registration timed out. Verify that the SIP server address and port are reachable and that the configured transport is supported.",
            suggested_action="Verify that the SIP server address and port are reachable and that the configured transport is supported.",
            retryable=True,
            error=f"Network timeout: No response received from {clean_host}:{port} after {timeout}s.",
        )
    except Exception as exc:
        sock.close()
        return make_sip_diagnostic_error(
            status="network_error",
            error_code="NETWORK_ERROR",
            message=f"UDP socket error transmitting REGISTER: {exc}",
            suggested_action="Check network adapter, firewall rules, and UDP socket permissions.",
            retryable=True,
            error=f"UDP transmission error: {exc}",
        )

    msg1 = resp_bytes.decode("utf-8", errors="ignore")
    first_line1 = msg1.splitlines()[0].strip() if msg1 else ""

    if "200 OK" in first_line1:
        sock.close()
        return classify_sip_registration_response(200, first_line1, msg1, is_authenticated_retry=False)

    if "401" in first_line1 or "407" in first_line1:
        # Check for unsupported algorithm in initial challenge
        algo_m = re.search(r'algorithm="?([^",\r\n]+)"?', msg1, re.IGNORECASE)
        algo = algo_m.group(1).upper() if algo_m else "MD5"
        if algo not in ("MD5", "MD5-SESS"):
            sock.close()
            return classify_sip_registration_response(401 if "401" in first_line1 else 407, first_line1, msg1, is_authenticated_retry=False)

        realm_m = re.search(r'realm="([^"]+)"', msg1)
        nonce_m = re.search(r'nonce="([^"]+)"', msg1)
        qop_m = re.search(r'qop="?([^,\r\n"]+)"?', msg1)
        opaque_m = re.search(r'opaque="([^"]+)"', msg1)

        if not realm_m or not nonce_m:
            sock.close()
            return make_sip_diagnostic_error(
                status="auth_failed",
                error_code="AUTH_CHALLENGE_INCOMPLETE",
                message="SIP server authentication challenge incomplete (missing realm or nonce).",
                suggested_action="Check SIP server configuration for compliant digest authentication challenge headers.",
                retryable=False,
                error=f"SIP server challenge missing realm/nonce: {first_line1}",
                sip_status_code=401 if "401" in first_line1 else 407,
            )

        realm = realm_m.group(1)
        nonce = nonce_m.group(1)
        qop = qop_m.group(1) if qop_m else None
        opaque = opaque_m.group(1) if opaque_m else None
        uri = f"sip:{clean_host}:{port}"

        auth_header = compute_digest_auth(
            username, password, realm, nonce, uri, qop, opaque, auth_user=auth_username
        )
        cseq += 1
        req2 = build_register_packet(
            clean_host, port, username, contact_ip, listen_port, call_id, tag, cseq_num=cseq, auth_header=auth_header
        )
        try:
            sock.sendto(req2.encode("utf-8"), (resolved_ip, port))
            resp_bytes2, _ = sock.recvfrom(4096)
        except socket.timeout:
            sock.close()
            return make_sip_diagnostic_error(
                status="network_error",
                error_code="REQUEST_TIMEOUT",
                message="Timeout waiting for authenticated registration response from registrar.",
                suggested_action="Check network latency and ensure the SIP registrar is responsive.",
                retryable=True,
                error=f"Timeout waiting for authenticated registration response from {clean_host}:{port}",
            )
        except Exception as exc:
            sock.close()
            return make_sip_diagnostic_error(
                status="network_error",
                error_code="NETWORK_ERROR",
                message=f"Socket error on authenticated registration retry: {exc}",
                suggested_action="Check network adapter and firewall rules.",
                retryable=True,
                error=f"Socket error on authenticated registration: {exc}",
            )

        msg2 = resp_bytes2.decode("utf-8", errors="ignore")
        first_line2 = msg2.splitlines()[0].strip() if msg2 else ""

        # RFC 3261 Section 22.4: If response contains stale=true, retry once with the new nonce
        stale_m2 = re.search(r'stale="?([^",\r\n]+)"?', msg2, re.IGNORECASE)
        is_stale2 = bool(stale_m2 and stale_m2.group(1).lower() in ("true", "yes", "1"))
        if is_stale2 and ("401" in first_line2 or "407" in first_line2):
            realm_m2 = re.search(r'realm="([^"]+)"', msg2)
            nonce_m2 = re.search(r'nonce="([^"]+)"', msg2)
            qop_m2 = re.search(r'qop="?([^,\r\n"]+)"?', msg2)
            opaque_m2 = re.search(r'opaque="([^"]+)"', msg2)
            if realm_m2 and nonce_m2:
                realm2 = realm_m2.group(1)
                nonce2 = nonce_m2.group(1)
                qop2 = qop_m2.group(1) if qop_m2 else None
                opaque2 = opaque_m2.group(1) if opaque_m2 else None
                auth_header2 = compute_digest_auth(
                    username, password, realm2, nonce2, uri, qop2, opaque2, auth_user=auth_username
                )
                cseq += 1
                req3 = build_register_packet(
                    clean_host, port, username, contact_ip, listen_port, call_id, tag, cseq_num=cseq, auth_header=auth_header2
                )
                try:
                    sock.sendto(req3.encode("utf-8"), (resolved_ip, port))
                    resp_bytes3, _ = sock.recvfrom(4096)
                    sock.close()
                    msg3 = resp_bytes3.decode("utf-8", errors="ignore")
                    first_line3 = msg3.splitlines()[0].strip() if msg3 else ""
                    return classify_sip_registration_response(
                        None,
                        first_line3,
                        msg3,
                        is_authenticated_retry=True,
                        clean_host=clean_host,
                        port=port,
                    )
                except Exception:
                    pass

        sock.close()
        return classify_sip_registration_response(
            None,
            first_line2,
            msg2,
            is_authenticated_retry=True,
            clean_host=clean_host,
            port=port,
        )

    sock.close()
    return classify_sip_registration_response(
        None,
        first_line1,
        msg1,
        is_authenticated_retry=False,
        clean_host=clean_host,
        port=port,
    )


async def register_and_update_sip_account(
    db: AsyncSession,
    account: SIPAccount,
    timeout: float = 4.0,
) -> Dict[str, Any]:
    """Initiates an active SIP REGISTER transaction, verifies digest challenge, and updates DB status with structured diagnostics."""
    now = datetime.now(timezone.utc)

    if not account.enabled:
        account.registration_status = "disabled"
        account.last_error_message = f"SIP account '{account.name}' is disabled."
        account.last_health_checked_at = now
        diag = {
            "status": "disabled",
            "error_code": "ACCOUNT_DISABLED",
            "message": account.last_error_message,
            "sip_status_code": None,
            "retryable": False,
            "suggested_action": "Enable the account in configuration to permit registration and call routing.",
            "last_checked_at": now.isoformat(),
        }
        current_settings = dict(account.settings) if isinstance(account.settings, dict) else {}
        current_settings["registration_diagnostics"] = diag
        account.settings = current_settings
        db.add(account)
        await db.commit()
        await db.refresh(account)
        return {
            "account_id": account.id,
            "status": "disabled",
            "message": account.last_error_message,
            "is_registered": False,
            "last_error": account.last_error_message,
            "error_code": "ACCOUNT_DISABLED",
            "sip_status_code": None,
            "retryable": False,
            "suggested_action": diag["suggested_action"],
            "last_checked_at": diag["last_checked_at"],
            "timestamp": now,
        }

    raw_password = decrypt_sip_secret(account.password_hash) if account.password_hash else ""

    # Check if outbound-only static trunk that doesn't need REGISTER
    if account.direction and account.direction.lower() == "outbound" and not raw_password:
        probe_res = await probe_sip_connectivity(account.host, account.port, account.transport, timeout=2.5)
        account.registration_status = "not_required" if probe_res["reachable"] else "unreachable"
        account.last_health_status = "healthy" if probe_res["reachable"] else "unreachable"
        account.last_error_message = None if probe_res["reachable"] else probe_res.get("error")
        account.last_health_checked_at = now
        diag = {
            "status": account.registration_status,
            "error_code": None if probe_res["reachable"] else "NETWORK_TIMEOUT",
            "message": "Static outbound trunk (REGISTER not required)." if probe_res["reachable"] else "Host unreachable",
            "sip_status_code": None,
            "retryable": not probe_res["reachable"],
            "suggested_action": "Trunk is configured for static IP routing; REGISTER is not required." if probe_res["reachable"] else "Verify server IP/port reachability.",
            "last_checked_at": now.isoformat(),
        }
        current_settings = dict(account.settings) if isinstance(account.settings, dict) else {}
        current_settings["registration_diagnostics"] = diag
        account.settings = current_settings
        db.add(account)
        await db.commit()
        await db.refresh(account)
        return {
            "account_id": account.id,
            "status": account.registration_status,
            "message": diag["message"],
            "is_registered": probe_res["reachable"],
            "last_error": account.last_error_message,
            "error_code": diag["error_code"],
            "sip_status_code": None,
            "retryable": diag["retryable"],
            "suggested_action": diag["suggested_action"],
            "last_checked_at": diag["last_checked_at"],
            "timestamp": now,
        }

    probe_result = await asyncio.to_thread(
        _perform_sip_register_probe,
        host=account.host,
        port=account.port,
        username=account.username,
        password=raw_password,
        auth_username=account.auth_username or account.username,
        transport=account.transport,
        timeout=timeout,
    )

    account.registration_status = probe_result["status"]
    account.last_error_message = probe_result.get("message") or probe_result.get("error")
    if probe_result["success"]:
        account.last_health_status = "healthy"
    elif probe_result["status"] in ("network_error", "dns_error"):
        account.last_health_status = "unreachable"
    else:
        account.last_health_status = "degraded"
    account.last_health_checked_at = now

    current_settings = dict(account.settings) if isinstance(account.settings, dict) else {}
    current_settings["registration_diagnostics"] = {
        "status": probe_result["status"],
        "error_code": probe_result.get("error_code"),
        "message": probe_result.get("message"),
        "sip_status_code": probe_result.get("sip_status_code"),
        "retryable": probe_result.get("retryable", False),
        "suggested_action": probe_result.get("suggested_action"),
        "last_checked_at": probe_result.get("last_checked_at") or now.isoformat(),
    }
    account.settings = current_settings

    db.add(account)
    await db.commit()
    await db.refresh(account)

    try:
        from app.services.sip_inbound_daemon import notify_sip_account_changed
        notify_sip_account_changed()
    except Exception:
        pass

    return {
        "account_id": account.id,
        "status": account.registration_status,
        "message": probe_result.get("message") or "Registration probe completed.",
        "is_registered": bool(probe_result.get("success")),
        "last_error": account.last_error_message,
        "error_code": probe_result.get("error_code"),
        "sip_status_code": probe_result.get("sip_status_code"),
        "retryable": probe_result.get("retryable", False),
        "suggested_action": probe_result.get("suggested_action"),
        "last_checked_at": probe_result.get("last_checked_at") or now.isoformat(),
        "timestamp": now,
    }



async def validate_sip_account_for_campaign(
    db: AsyncSession,
    sip_account_id: str,
    organization_id: str,
    call_direction: str = "outbound",
) -> Tuple[bool, Optional[str], Optional[SIPAccount]]:
    """Validates that a SIP account exists, belongs to the organization, is enabled, and supports direction.
    
    Returns:
        (is_valid: bool, error_message: str or None, account: SIPAccount or None)
    """
    if not sip_account_id:
        return False, "SIP Account ID is required", None

    stmt = select(SIPAccount).where(
        SIPAccount.id == sip_account_id,
        SIPAccount.organization_id == organization_id,
    )
    result = await db.execute(stmt)
    account = result.scalar_one_or_none()

    raw_acc = await db.get(SIPAccount, sip_account_id)
    if raw_acc and raw_acc.organization_id != organization_id:
        return False, f"Tenant isolation violation: SIP account '{sip_account_id}' belongs to another organization.", None

    if not account:
        return False, "Selected SIP account does not exist or does not belong to your organization", None

    if not account.enabled:
        return False, f"SIP account '{account.name}' is currently disabled. Please enable it before using.", account

    dir_clean = (call_direction or "outbound").strip().lower()
    acc_dir = (account.direction or "both").strip().lower()
    if dir_clean == "outbound" and acc_dir not in ("outbound", "both", "bidirectional"):
        return False, f"SIP account '{account.name}' is configured for {account.direction} calls only and cannot be used for outbound campaigns.", account

    if dir_clean == "inbound" and acc_dir not in ("inbound", "both", "bidirectional"):
        return False, f"SIP account '{account.name}' is configured for {account.direction} calls only and cannot be used for inbound campaigns.", account

    return True, None, account


async def validate_sip_did_for_campaign(
    db: AsyncSession,
    sip_account_id: str,
    did_id_or_number: str,
    organization_id: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[Any]]:
    """Validates that a DID exists, belongs to the organization (if specified),
    is active, and is explicitly associated with the specified SIP account.

    Returns:
        (is_valid: bool, error_message: str or None, did: DIDNumber or None)
    """
    if not did_id_or_number:
        return False, "DID identifier or phone number is required", None

    from app.db.models import DIDNumber

    # Query by ID first, then by phone_number
    did = await db.get(DIDNumber, did_id_or_number)
    if not did:
        stmt = select(DIDNumber).where(DIDNumber.phone_number == did_id_or_number.strip())
        if organization_id:
            stmt = stmt.where(DIDNumber.business_id == organization_id)
        did = (await db.execute(stmt)).scalars().first()

    if not did:
        return False, f"Selected DID '{did_id_or_number}' does not exist.", None

    if organization_id and did.business_id and did.business_id != organization_id:
        return False, f"Tenant isolation violation: DID '{did.phone_number}' belongs to another organization.", did

    if did.sip_account_id and did.sip_account_id != sip_account_id:
        return False, f"DID '{did.phone_number}' is assigned to SIP account '{did.sip_account_id}', not '{sip_account_id}'.", did

    if hasattr(did, "status") and did.status and did.status.upper() not in ("ACTIVE", "CONFIGURED"):
        return False, f"DID '{did.phone_number}' is inactive (status: {did.status}).", did

    return True, None, did



async def get_organization_sip_account(
    db: AsyncSession,
    organization_id: str,
    sip_account_id: Optional[str] = None,
    call_direction: str = "outbound",
    raise_on_explicit_failure: bool = False,
) -> Optional[SIPAccount]:
    """Resolves the authoritative SIPAccount for an organization:
    1. If sip_account_id is provided, loads and validates that specific account.
    2. If sip_account_id is None, finds the default enabled SIPAccount for the organization.
    3. If no default is marked, returns the earliest active enabled SIPAccount for the organization.
    4. Returns None (or raises ValueError if raise_on_explicit_failure=True) if no valid SIPAccount is available.
    """
    if not organization_id:
        if raise_on_explicit_failure:
            raise ValueError("Organization ID must be provided to resolve SIP account.")
        return None

    if sip_account_id:
        is_val, err, acc = await validate_sip_account_for_campaign(
            db, sip_account_id, organization_id, call_direction=call_direction
        )
        if not is_val:
            if raise_on_explicit_failure:
                raise ValueError(err or f"SIP account '{sip_account_id}' is invalid, disabled, or unauthorized.")
            return None
        return acc

    # Look for organization enabled accounts matching direction
    stmt = select(SIPAccount).where(
        SIPAccount.organization_id == organization_id,
        SIPAccount.enabled == True,
    )
    dir_clean = (call_direction or "outbound").strip().lower()
    if dir_clean == "outbound":
        allowed_dirs = ["outbound", "both", "bidirectional", "OUTBOUND", "BOTH", "BIDIRECTIONAL"]
        stmt = stmt.where(SIPAccount.direction.in_(allowed_dirs))
    elif dir_clean == "inbound":
        allowed_dirs = ["inbound", "both", "bidirectional", "INBOUND", "BOTH", "BIDIRECTIONAL"]
        stmt = stmt.where(SIPAccount.direction.in_(allowed_dirs))

    accounts = (await db.scalars(stmt.order_by(SIPAccount.created_at.asc()))).all()
    if not accounts:
        if raise_on_explicit_failure:
            raise ValueError(f"No enabled SIP accounts found for organization '{organization_id}'.")
        return None

    # Check for explicitly marked default
    for acc in accounts:
        if getattr(acc, "is_default", False) or (isinstance(getattr(acc, "settings", None), dict) and acc.settings.get("is_default")):
            return acc

    return accounts[0]


async def sync_carrier_trunks_to_sip_accounts(
    db: AsyncSession,
    organization_id: Optional[str] = None,
) -> list[SIPAccount]:
    """Idempotently syncs existing SIPCarrier / SIPTrunk records with multi-tenant SIPAccount records.
    - Preserves existing credentials, endpoints, and trunk configurations.
    - Ensures that active carriers and trunks are available under Admin Telephony SIP Accounts.
    - Reversibly links campaigns using the carrier/trunk to the corresponding SIPAccount.
    """
    from app.db.models import SIPCarrier, SIPTrunk, DIDNumber, Business, Campaign

    carrier_stmt = select(SIPCarrier)
    if organization_id:
        carrier_stmt = carrier_stmt.where(SIPCarrier.business_id == organization_id)
    carriers = (await db.scalars(carrier_stmt)).all()
    if not carriers:
        return []

    businesses = (await db.scalars(select(Business))).all()
    biz_map = {b.id: b for b in businesses}
    if not businesses:
        return []

    synced_accounts: list[SIPAccount] = []

    for carrier in carriers:
        trunks = (await db.scalars(select(SIPTrunk).where(SIPTrunk.carrier_id == carrier.id))).all()
        if not trunks:
            trunks = [
                SIPTrunk(
                    id=str(uuid.uuid4()),
                    carrier_id=carrier.id,
                    name=f"Primary Route ({carrier.sip_host})",
                    direction="OUTBOUND" if carrier.outbound_enabled else "INBOUND",
                    max_concurrency=carrier.max_channels or 30,
                    status=carrier.status,
                )
            ]
            db.add(trunks[0])
            await db.flush()

        # Determine target organizations for this carrier strictly by carrier.business_id
        target_org_ids = set()
        if carrier.business_id and carrier.business_id in biz_map:
            target_org_ids.add(carrier.business_id)
        elif not carrier.business_id:
            # Unassigned legacy carrier maps to primary business only
            primary_id = "fa5ffab7-4039-47e3-9c05-3546b2109a12"
            if primary_id in biz_map:
                target_org_ids.add(primary_id)

        for org_id in target_org_ids:
            # Find default outbound DID for this org & carrier
            settings = get_settings()
            outbound_did = (await db.scalars(
                select(DIDNumber).where(
                    DIDNumber.business_id == org_id,
                    DIDNumber.status == "ACTIVE",
                    DIDNumber.is_outbound_caller_id.is_(True),
                    or_(DIDNumber.carrier_id == carrier.id, DIDNumber.carrier_id.is_(None))
                ).order_by(DIDNumber.created_at.desc())
            )).first()
            if not outbound_did:
                outbound_did = (await db.scalars(
                    select(DIDNumber).where(
                        DIDNumber.business_id == org_id,
                        DIDNumber.status == "ACTIVE",
                        or_(DIDNumber.carrier_id == carrier.id, DIDNumber.carrier_id.is_(None))
                    ).order_by(DIDNumber.created_at.desc())
                )).first()

            caller_did = outbound_did.phone_number if outbound_did else None
            caller_did_id = outbound_did.id if outbound_did else None

            # Clean host and port
            raw_host = (carrier.sip_host or "").strip()
            if not raw_host:
                continue
            if ":" in raw_host:
                host_part, port_str = raw_host.split(":", 1)
                port_val = int(port_str) if port_str.isdigit() else (carrier.sip_port or 5060)
            else:
                host_part = raw_host
                port_val = carrier.sip_port or 5060

            for trunk in trunks:
                existing_accounts = (await db.scalars(
                    select(SIPAccount).where(SIPAccount.organization_id == org_id)
                )).all()

                matched_acc = None
                for acc in existing_accounts:
                    settings_dict = acc.settings or {}
                    if settings_dict.get("carrier_id") == carrier.id and settings_dict.get("trunk_id") == trunk.id:
                        matched_acc = acc
                        break
                    if acc.host == host_part and acc.username == (carrier.username or carrier.name):
                        matched_acc = acc
                        break

                acc_name = f"{carrier.name}" if trunk.name.startswith(carrier.name) else f"{carrier.name} ({trunk.name})"
                if len(acc_name) > 140:
                    acc_name = acc_name[:140]

                direction_val = (trunk.direction or "BIDIRECTIONAL").lower()
                if direction_val == "bidirectional":
                    direction_val = "both"
                elif direction_val not in ("outbound", "inbound", "both"):
                    direction_val = "both"

                raw_pwd = carrier.password_hash or carrier.username or ""
                encrypted_pw = encrypt_sip_secret(raw_pwd) if raw_pwd else None

                if not matched_acc:
                    new_acc = SIPAccount(
                        id=str(uuid.uuid4()),
                        organization_id=org_id,
                        name=acc_name,
                        username=carrier.username or carrier.name,
                        auth_username=carrier.username or carrier.name,
                        password_hash=encrypted_pw,
                        domain=host_part,
                        host=host_part,
                        port=port_val,
                        transport=(carrier.transport or "udp").lower(),
                        outbound_proxy=trunk.proxy_server,
                        caller_id=caller_did,
                        direction=direction_val,
                        enabled=(carrier.status == "ACTIVE" and trunk.status == "ACTIVE"),
                        registration_status="pending",
                        last_health_status="healthy" if (carrier.last_health_status == "HEALTHY" or carrier.status == "ACTIVE") else "unknown",
                        last_health_latency_ms=carrier.last_health_latency_ms or 24.5,
                        last_health_checked_at=carrier.last_health_checked_at or datetime.now(timezone.utc).replace(tzinfo=None),
                        last_error_message=carrier.last_error_message,
                        max_channels=min(carrier.max_channels or 30, trunk.max_concurrency or 30),
                        settings={
                            "carrier_id": carrier.id,
                            "trunk_id": trunk.id,
                            "caller_did_id": caller_did_id,
                            "synced_from_carrier": True,
                        },
                    )
                    db.add(new_acc)
                    await db.flush()
                    matched_acc = new_acc
                    logger.info(f"[SIP_SYNC] Created SIPAccount '{new_acc.name}' ({new_acc.id}) for org={org_id}")
                else:
                    curr_settings = dict(matched_acc.settings or {})
                    curr_settings["carrier_id"] = carrier.id
                    curr_settings["trunk_id"] = trunk.id
                    if caller_did_id:
                        curr_settings["caller_did_id"] = caller_did_id
                    curr_settings["synced_from_carrier"] = True
                    matched_acc.settings = curr_settings
                    if not matched_acc.caller_id and caller_did:
                        matched_acc.caller_id = caller_did
                    if not matched_acc.password_hash and encrypted_pw:
                        matched_acc.password_hash = encrypted_pw
                    db.add(matched_acc)

                synced_accounts.append(matched_acc)

                # Link campaigns belonging to this org that reference this carrier or trunk
                camps_to_link = (await db.scalars(
                    select(Campaign).where(
                        Campaign.business_id == org_id,
                        or_(Campaign.carrier_id == carrier.id, Campaign.trunk_id == trunk.id),
                        Campaign.sip_account_id.is_(None),
                    )
                )).all()

                for camp in camps_to_link:
                    camp.sip_account_id = matched_acc.id
                    if not camp.carrier_id:
                        camp.carrier_id = carrier.id
                    if not camp.trunk_id:
                        camp.trunk_id = trunk.id
                    if not camp.caller_did_id and caller_did_id:
                        camp.caller_did_id = caller_did_id
                    if not camp.twilio_number and caller_did:
                        camp.twilio_number = caller_did
                    db.add(camp)
                    logger.info(f"[SIP_SYNC] Linked campaign '{camp.name}' ({camp.id}) to SIPAccount {matched_acc.id}")

                # Link DIDs belonging to this carrier or trunk to matched_acc if unlinked
                dids_to_link = (await db.scalars(
                    select(DIDNumber).where(
                        DIDNumber.business_id == org_id,
                        or_(DIDNumber.carrier_id == carrier.id, DIDNumber.trunk_id == trunk.id),
                        DIDNumber.sip_account_id.is_(None),
                    )
                )).all()
                for did_item in dids_to_link:
                    did_item.sip_account_id = matched_acc.id
                    db.add(did_item)
                    logger.info(f"[SIP_SYNC] Linked DID '{did_item.phone_number}' ({did_item.id}) to SIPAccount {matched_acc.id}")

    await db.commit()
    return synced_accounts


async def sync_sip_account_to_carrier_trunk(
    db: AsyncSession,
    account: SIPAccount,
    raw_password: Optional[str] = None,
) -> Tuple[Any, Any]:
    """Propagates changes or creations of a SIPAccount back to SIPCarrier and SIPTrunk
    to guarantee consistency with legacy carrier/trunk modules.
    """
    from app.db.models import SIPCarrier, SIPTrunk

    settings_dict = dict(account.settings or {})
    carrier_id = settings_dict.get("carrier_id")
    trunk_id = settings_dict.get("trunk_id")

    carrier = None
    if carrier_id:
        carrier = await db.get(SIPCarrier, carrier_id)

    if not carrier:
        carrier = (await db.scalars(
            select(SIPCarrier).where(
                SIPCarrier.sip_host == account.host,
                SIPCarrier.username == account.username,
            )
        )).first()

    pw_to_save = raw_password or (decrypt_sip_secret(account.password_hash) if account.password_hash else account.username)

    if not carrier:
        carrier = SIPCarrier(
            id=str(uuid.uuid4()),
            business_id=account.organization_id,
            name=account.username or account.name,
            provider_type="ASTERISK",
            sip_host=account.host,
            sip_port=account.port,
            transport=(account.transport or "UDP").upper(),
            auth_type="CREDENTIAL",
            username=account.username,
            password_hash=pw_to_save,
            inbound_enabled=(account.direction in ("inbound", "both", "BIDIRECTIONAL")),
            outbound_enabled=(account.direction in ("outbound", "both", "BIDIRECTIONAL")),
            max_channels=account.max_channels,
            status="ACTIVE" if account.enabled else "INACTIVE",
            last_health_status="HEALTHY" if account.last_health_status == "healthy" else "UNKNOWN",
        )
        db.add(carrier)
        await db.flush()
    else:
        carrier.sip_host = account.host
        carrier.sip_port = account.port
        carrier.transport = (account.transport or "UDP").upper()
        carrier.username = account.username
        if pw_to_save:
            carrier.password_hash = pw_to_save
        carrier.status = "ACTIVE" if account.enabled else "INACTIVE"
        carrier.max_channels = account.max_channels
        db.add(carrier)

    trunk = None
    if trunk_id:
        trunk = await db.get(SIPTrunk, trunk_id)

    if not trunk:
        trunk = (await db.scalars(
            select(SIPTrunk).where(SIPTrunk.carrier_id == carrier.id)
        )).first()

    direction_val = "OUTBOUND" if account.direction == "outbound" else ("INBOUND" if account.direction == "inbound" else "BIDIRECTIONAL")

    if not trunk:
        trunk = SIPTrunk(
            id=str(uuid.uuid4()),
            carrier_id=carrier.id,
            business_id=account.organization_id,
            name=f"Primary Outbound ({account.host})",
            direction=direction_val,
            max_concurrency=account.max_channels,
            priority=1,
            proxy_server=account.outbound_proxy,
            register_enabled=True,
            registration_status="PEER_ACTIVE" if account.enabled else "UNREGISTERED",
            status="ACTIVE" if account.enabled else "DISABLED",
        )
        db.add(trunk)
        await db.flush()
    else:
        trunk.direction = direction_val
        trunk.max_concurrency = account.max_channels
        trunk.proxy_server = account.outbound_proxy
        trunk.status = "ACTIVE" if account.enabled else "DISABLED"
        db.add(trunk)

    settings_dict["carrier_id"] = carrier.id
    settings_dict["trunk_id"] = trunk.id
    account.settings = settings_dict
    db.add(account)
    await db.commit()

