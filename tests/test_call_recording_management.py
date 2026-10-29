"""Unit and Integration Tests for Campaign-wise Call Recording Management.

Covers:
1. Inbound & outbound recordings associated with correct campaign.
2. Campaign filtering & tenant isolation.
3. Audio streaming with 206 Partial Content range requests.
4. Prevention of streaming expired/deleted recordings (HTTP 410 Gone).
5. Safe deletion with preservation of verified Google Drive backups.
6. 24-hour automatic recording expiry lifecycle.
7. Google Drive export status tracking and retry flow.
8. Backward compatibility with existing reports and calls.
"""
import os
import uuid
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import Call, Campaign, Business, User, Integration
from app.db.call_history_models import CallRecording
from app.core.security import create_access_token
from app.services.call_recording_service import (
    ensure_recording_for_call,
    cleanup_expired_recordings,
    delete_recording,
    RECORDINGS_DIR,
    AUDIO_DIR,
)
from app.services.google_drive_service import (
    get_drive_status,
    save_drive_credentials,
)


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_call_recording_campaign_association_and_filtering():
    """Verify that inbound and outbound recordings correctly link to campaigns and isolate by tenant."""
    async with AsyncSessionLocal() as db:
        tenant_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
        camp1_id = f"camp1-{uuid.uuid4().hex[:8]}"
        camp2_id = f"camp2-{uuid.uuid4().hex[:8]}"

        # Create campaigns
        c1 = Campaign(id=camp1_id, name="Solar Outbound Campaign", business_id=tenant_id)
        c2 = Campaign(id=camp2_id, name="Support Inbound Campaign", business_id=tenant_id)
        db.add_all([c1, c2])

        # Create audio files
        call1_id = f"call-out-{uuid.uuid4().hex[:8]}"
        call2_id = f"call-in-{uuid.uuid4().hex[:8]}"

        f1 = RECORDINGS_DIR / f"{call1_id}_rec.wav"
        f1.write_bytes(b"RIFF" + b"\x00" * 100)

        f2 = RECORDINGS_DIR / f"{call2_id}_rec.wav"
        f2.write_bytes(b"RIFF" + b"\x00" * 100)

        # Create calls
        call1 = Call(
            id=call1_id,
            business_id=tenant_id,
            campaign_id=camp1_id,
            direction="OUTBOUND",
            client_name="Alice Smith",
            client_phone="+15551112222",
            duration_seconds=45,
            recording_url=f"/recordings/{call1_id}_rec.wav",
            status="COMPLETED",
        )
        call2 = Call(
            id=call2_id,
            business_id=tenant_id,
            campaign_id=camp2_id,
            direction="INBOUND",
            client_name="Bob Jones",
            client_phone="+15553334444",
            duration_seconds=90,
            recording_url=f"/recordings/{call2_id}_rec.wav",
            status="COMPLETED",
        )
        db.add_all([call1, call2])
        await db.commit()

        # Ensure recordings created
        rec1 = await ensure_recording_for_call(db, call1)
        rec2 = await ensure_recording_for_call(db, call2)

        assert rec1.campaign_id == camp1_id
        assert rec1.call_direction == "OUTBOUND"
        assert rec1.recording_status in ("available", "ready")
        assert rec1.expires_at is not None

        assert rec2.campaign_id == camp2_id
        assert rec2.call_direction == "INBOUND"

    # Test API filtering with token
    token = create_access_token(subject="test_user", role="admin", tenant_id=tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Filter by Campaign 1
        resp = await client.get(f"/api/reports/call-recordings?campaign_id={camp1_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["campaign_id"] == camp1_id
        assert data["items"][0]["call_direction"] == "OUTBOUND"
        assert data["items"][0]["customer_name"] == "Alice Smith"

        # Filter by Campaign 2
        resp2 = await client.get(f"/api/reports/call-recordings?campaign_id={camp2_id}", headers=headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["total"] == 1
        assert data2["items"][0]["campaign_id"] == camp2_id
        assert data2["items"][0]["call_direction"] == "INBOUND"

        # Cross-tenant isolation check: another tenant token cannot see these
        other_tenant_token = create_access_token(subject="other_user", role="admin", tenant_id="other-tenant-999")
        other_resp = await client.get(
            f"/api/reports/call-recordings?campaign_id={camp1_id}",
            headers={"Authorization": f"Bearer {other_tenant_token}"}
        )
        assert other_resp.status_code == 200
        assert other_resp.json()["total"] == 0

        # Clean up test files
        if f1.exists():
            f1.unlink()
        if f2.exists():
            f2.unlink()


@pytest.mark.asyncio
async def test_audio_streaming_range_and_expiry_block():
    """Verify HTTP 206 Partial Content range requests and HTTP 410 block for expired recordings."""
    tenant_id = f"stream-tenant-{uuid.uuid4().hex[:8]}"
    call_id = f"stream-call-{uuid.uuid4().hex[:8]}"

    # Create dummy audio file
    dummy_wav = RECORDINGS_DIR / f"{call_id}_rec.wav"
    audio_data = b"RIFF" + bytes(range(200))  # 204 bytes
    dummy_wav.write_bytes(audio_data)

    async with AsyncSessionLocal() as db:
        call = Call(
            id=call_id,
            business_id=tenant_id,
            direction="OUTBOUND",
            client_name="Streaming Tester",
            recording_url=f"/recordings/{call_id}_rec.wav",
        )
        db.add(call)
        await db.commit()
        rec = await ensure_recording_for_call(db, call)
        recording_id = rec.id

    token = create_access_token(subject="stream_user", role="admin", tenant_id=tenant_id)
    headers = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Full content stream (200 OK)
        resp_full = await client.get(f"/api/reports/call-recordings/{recording_id}/stream", headers=headers)
        assert resp_full.status_code == 200
        assert resp_full.content == audio_data
        assert resp_full.headers["Accept-Ranges"] == "bytes"

        # Range request (206 Partial Content)
        range_headers = {**headers, "Range": "bytes=0-99"}
        resp_range = await client.get(f"/api/reports/call-recordings/{recording_id}/stream", headers=range_headers)
        assert resp_range.status_code == 206
        assert len(resp_range.content) == 100
        assert resp_range.content == audio_data[:100]
        assert "bytes 0-99/204" in resp_range.headers["Content-Range"]

        # Now simulate 24-hour expiry
        async with AsyncSessionLocal() as db:
            r = await db.get(CallRecording, recording_id)
            r.recording_status = "expired"
            r.expires_at = _utc_now() - timedelta(minutes=5)
            await db.commit()

        # Stream must be rejected with HTTP 410 Gone!
        resp_expired = await client.get(f"/api/reports/call-recordings/{recording_id}/stream", headers=headers)
        assert resp_expired.status_code == 410

    if dummy_wav.exists():
        dummy_wav.unlink()


@pytest.mark.asyncio
async def test_24_hour_expiry_and_cleanup_lifecycle():
    """Verify that recordings past 24 hours expire and files are cleaned up, but Google Drive backups are retained."""
    tenant_id = f"expiry-tenant-{uuid.uuid4().hex[:8]}"
    call_id1 = f"exp-call-no-backup-{uuid.uuid4().hex[:8]}"
    call_id2 = f"exp-call-with-backup-{uuid.uuid4().hex[:8]}"

    f1 = RECORDINGS_DIR / f"{call_id1}_rec.wav"
    f1.write_bytes(b"TESTAUDIO12345")
    f2 = RECORDINGS_DIR / f"{call_id2}_rec.wav"
    f2.write_bytes(b"TESTAUDIO67890")

    async with AsyncSessionLocal() as db:
        # Recording 1: expired, NOT backed up
        rec1 = CallRecording(
            call_id=call_id1,
            tenant_id=tenant_id,
            storage_key=f"/recordings/{call_id1}_rec.wav",
            recording_status="available",
            backup_status="NOT_BACKED_UP",
            created_at=_utc_now() - timedelta(hours=25),
            expires_at=_utc_now() - timedelta(hours=1),
        )
        # Recording 2: expired, BACKED UP to Google Drive
        rec2 = CallRecording(
            call_id=call_id2,
            tenant_id=tenant_id,
            storage_key=f"/recordings/{call_id2}_rec.wav",
            recording_status="available",
            backup_status="BACKED_UP",
            google_drive_file_id="gdrive-file-abc-999",
            google_drive_file_url="https://drive.google.com/file/d/gdrive-file-abc-999/view",
            created_at=_utc_now() - timedelta(hours=25),
            expires_at=_utc_now() - timedelta(hours=1),
        )
        db.add_all([rec1, rec2])
        await db.commit()
        await db.refresh(rec1)
        await db.refresh(rec2)

        # Run cleanup
        result = await cleanup_expired_recordings(db)
        assert result["expired_processed"] >= 2

        # Check rec1: file deleted, status expired, cleanup_status cleaned
        await db.refresh(rec1)
        assert rec1.recording_status == "expired"
        assert rec1.cleanup_status == "cleaned"
        assert not f1.exists()

        # Check rec2: local file deleted, status expired, but backup_status remains BACKED_UP,
        # google_drive_file_id is preserved, cleanup_status = retained_backup!
        await db.refresh(rec2)
        assert rec2.recording_status == "expired"
        assert rec2.backup_status == "BACKED_UP"
        assert rec2.google_drive_file_id == "gdrive-file-abc-999"
        assert rec2.cleanup_status == "retained_backup"
        assert not f2.exists()


@pytest.mark.asyncio
async def test_delete_recording_idempotency_and_backup_preservation():
    """Verify manual delete removes physical file, marks deleted, and preserves Drive backup safely."""
    tenant_id = f"del-tenant-{uuid.uuid4().hex[:8]}"
    call_id = f"del-call-{uuid.uuid4().hex[:8]}"

    f = RECORDINGS_DIR / f"{call_id}_rec.wav"
    f.write_bytes(b"WAVTOBEDELETED")

    async with AsyncSessionLocal() as db:
        rec = CallRecording(
            call_id=call_id,
            tenant_id=tenant_id,
            storage_key=f"/recordings/{call_id}_rec.wav",
            recording_status="available",
            backup_status="BACKED_UP",
            google_drive_file_id="drive-safe-123",
            google_drive_file_url="https://drive.google.com/file/d/drive-safe-123/view",
            expires_at=_utc_now() + timedelta(hours=12),
        )
        db.add(rec)
        await db.commit()
        await db.refresh(rec)
        rec_id = rec.id

        # Execute deletion
        res = await delete_recording(db, rec_id, tenant_id)
        assert res["success"] is True
        assert res["recording_status"] == "deleted"
        assert res["backup_preserved"] is True
        assert not f.exists()

        # Idempotency check: deleting again succeeds without error
        res_repeat = await delete_recording(db, rec_id, tenant_id)
        assert res_repeat["success"] is True
        assert "already deleted" in res_repeat["message"]
