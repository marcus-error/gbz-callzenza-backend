"""
Tests for Call History v2 (additive endpoints, HMAC tokens, Range streaming, filters, export).
"""
import io
import os
import time
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta
from app.core.recording_token import generate_token, verify_token
from app.repositories.call_history_repository import (
    _encode_cursor,
    _decode_cursor,
    _mask_phone,
    _recording_info,
)
from app.api.v1.call_history import _parse_range
from app.db.models import Call, Business, User, Campaign
from app.db.database import AsyncSessionLocal
from app.main import app


def test_recording_token_lifecycle():
    # 1. Valid token
    call_id = "test-call-1234"
    seg = 0
    uid = "user-5678"
    token = generate_token(call_id, seg, uid, ttl_seconds=10)
    assert token is not None

    cid_out, seg_out, uid_out = verify_token(token)
    assert cid_out == call_id
    assert seg_out == seg
    assert uid_out == uid

    # 2. Tampered token fails
    tampered = token[:-4] + "abcd"
    with pytest.raises(ValueError):
        verify_token(tampered)

    # 3. Expired token fails
    expired_token = generate_token(call_id, seg, uid, ttl_seconds=-1)
    with pytest.raises(ValueError, match="expired"):
        verify_token(expired_token)


def test_keyset_cursor_codec():
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)
    call_id = "call-abc-999"
    cursor = _encode_cursor(now, call_id)
    assert isinstance(cursor, str)

    ts_out, id_out = _decode_cursor(cursor)
    assert id_out == call_id
    assert ts_out is not None
    assert ts_out.year == 2026


def test_phone_masking():
    phone = "+15551234567"
    # Admins see full phone
    assert _mask_phone(phone, "SUPER_ADMIN") == phone
    assert _mask_phone(phone, "ADMIN") == phone
    assert _mask_phone(phone, "SUPERVISOR") == phone

    # Other roles see masked
    masked = _mask_phone(phone, "VIEWER")
    assert "****4567" in masked
    assert "1555" not in masked


def test_http_range_parser():
    # Normal bytes range
    start, end = _parse_range("bytes=0-499")
    assert start == 0
    assert end == 499

    # Open-ended range
    start2, end2 = _parse_range("bytes=1000-")
    assert start2 == 1000
    assert end2 is None

    # No header
    start3, end3 = _parse_range(None)
    assert start3 == 0
    assert end3 is None


def test_no_recording_call_info():
    c = Call(
        id="c-no-rec",
        business_id="b1",
        lead_id="l1",
        direction="OUTBOUND",
        status="COMPLETED",
        duration_seconds=30,
        recording_url=None,
    )
    info = _recording_info(c)
    assert info.status == "not_recorded"
    assert len(info.segments) == 0


@pytest.mark.asyncio
async def test_filter_options_endpoint_flag_check():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # If no auth, returns 401/403 or 404 (when flag off)
        resp = await ac.get("/api/v1/call-history/filter-options")
        assert resp.status_code in (401, 403, 404)
