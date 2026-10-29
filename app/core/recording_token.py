"""
Call History v2 — Signed recording access tokens (ENABLE_CALL_HISTORY_V2).

Tokens are short-lived HMAC-SHA256 strings encoding:
  call_id | segment_index | user_id | expires_at (unix int)

Single-purpose: only stream/download endpoints accept them.
Provider credentials and storage paths never leave the server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Tuple

_SECRET = os.getenv("CALL_HISTORY_TOKEN_SECRET", "ch-v2-default-secret-change-in-prod").encode()
_TTL_SECONDS = int(os.getenv("CALL_HISTORY_TOKEN_TTL", "300"))  # 5 minutes default


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def generate_token(call_id: str, segment: int, user_id: str, ttl_seconds: int | None = None) -> str:
    """Return a signed, base64url-safe token."""
    ttl = ttl_seconds if ttl_seconds is not None else _TTL_SECONDS
    expires_at = int(time.time()) + ttl
    payload = f"{call_id}|{segment}|{user_id}|{expires_at}"
    sig = _sign(payload)
    raw = f"{payload}::{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_token(token: str) -> Tuple[str, int, str]:
    """
    Validate token. Returns (call_id, segment, user_id).
    Raises ValueError on tamper, expiry, or bad format.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode() + b"==").decode()
        payload_part, sig = raw.rsplit("::", 1)
    except Exception:
        raise ValueError("Malformed token")

    if not hmac.compare_digest(_sign(payload_part), sig):
        raise ValueError("Token signature invalid")

    parts = payload_part.split("|")
    if len(parts) != 4:
        raise ValueError("Token payload malformed")

    call_id, segment_str, user_id, expires_str = parts
    if int(time.time()) > int(expires_str):
        raise ValueError("Token expired")

    return call_id, int(segment_str), user_id
