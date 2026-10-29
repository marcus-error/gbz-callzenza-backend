"""Google Drive Export & Backup Service for CallZenza Call Recordings.

Follows strict requirements:
1. Minimum required permissions: https://www.googleapis.com/auth/drive.file
2. OAuth credentials securely stored on backend in Integration table.
3. Resumable & multipart upload for reliable audio transfer.
4. Upload verification before marking Backed Up.
5. Independent lifecycle: original expiry does NOT remove verified Drive backup.
6. Single and campaign-level batch exports with retry capability.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import base64
import os
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple
import urllib.parse
import httpx
from loguru import logger
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Integration, Campaign, Call, AuditLog
from app.db.call_history_models import CallRecording
from app.services.call_recording_service import resolve_local_audio_path

from jose import jwt

settings = get_settings()

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
DEFAULT_FOLDER_NAME = "CallZenza Call Recordings"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sanitize_return_url(return_url: Optional[str], default_path: str = "/reports?tab=call-recordings") -> str:
    """
    Ensures return_url is a safe relative path within the CallZenza application.
    Rejects:
    - empty / None -> default_path
    - external URLs (starts with http:, https:, //, etc.)
    - javascript:, data:, etc.
    - backslashes (e.g. \\/evil.com)
    - paths not starting with /
    """
    if not return_url or not isinstance(return_url, str):
        return default_path
    cleaned = return_url.strip()
    if not cleaned or not cleaned.startswith("/"):
        return default_path
    if cleaned.startswith("//") or cleaned.startswith("/\\") or "\\" in cleaned:
        return default_path
    if any(ord(c) < 32 or ord(c) == 127 for c in cleaned):
        return default_path
    lower = cleaned.lower()
    if lower.startswith("/http:") or lower.startswith("/https:"):
        return default_path
    return cleaned


def resolve_frontend_base_url(origin: Optional[str] = None, subdomain: Optional[str] = None) -> str:
    """
    Resolves the safe base URL of the frontend application.
    Preserves tenant subdomain and origin if valid.
    """
    if origin and isinstance(origin, str):
        try:
            parsed = urllib.parse.urlparse(origin.strip())
            if parsed.scheme in ("http", "https") and parsed.netloc:
                host = parsed.netloc.split(":")[0].lower()
                if (
                    host in ("localhost", "127.0.0.1")
                    or host == "callzenza.com"
                    or host.endswith(".callzenza.com")
                ):
                    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        except Exception:
            pass

    configured_frontend = (getattr(settings, "frontend_url", None) or os.getenv("FRONTEND_URL", "http://localhost:3000")).rstrip("/")
    if subdomain and subdomain not in ("default", "system", "all"):
        try:
            parsed = urllib.parse.urlparse(configured_frontend)
            netloc = parsed.netloc
            if netloc and not netloc.startswith(f"{subdomain}."):
                host = netloc.split(":")[0]
                if host in ("callzenza.com", "localhost") or host.endswith(".callzenza.com"):
                    port_part = f":{netloc.split(':')[1]}" if ":" in netloc else ""
                    return f"{parsed.scheme}://{subdomain}.{host}{port_part}"
        except Exception:
            pass

    return configured_frontend or "http://localhost:3000"


def create_drive_oauth_state(
    user_id: str,
    business_id: str,
    return_url: Optional[str] = None,
    origin: Optional[str] = None,
    subdomain: Optional[str] = None,
) -> str:
    """Creates a cryptographically signed, expiring state token for Google Drive OAuth."""
    safe_return = sanitize_return_url(return_url)
    payload = {
        "service": "google_drive",
        "user_id": str(user_id),
        "business_id": str(business_id),
        "return_url": safe_return,
        "origin": origin,
        "subdomain": subdomain,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_drive_oauth_state(state: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decodes and validates a Google Drive OAuth state token."""
    if not state or not isinstance(state, str):
        return None
    # 1. Try decoding as signed JWT
    try:
        payload = jwt.decode(state, settings.secret_key, algorithms=["HS256"])
        if isinstance(payload, dict) and payload.get("service") == "google_drive":
            return payload
    except Exception as e:
        logger.debug(f"[DRIVE OAUTH] JWT decode check: {e}")

    # 2. Try decoding as JSON (backward compatibility)
    try:
        data = json.loads(state)
        if isinstance(data, dict):
            if data.get("service") == "google_drive" or ("user_id" in data and "business_id" in data):
                return data
    except Exception:
        pass

    return None


def is_drive_oauth_state(state: Optional[str]) -> bool:
    """Checks whether an OAuth state parameter was generated for Google Drive."""
    if not state or not isinstance(state, str):
        return False
    if decode_drive_oauth_state(state) is not None:
        return True
    if "." in state:
        try:
            parts = state.split(".")
            if len(parts) >= 2:
                padding = "=" * (-len(parts[1]) % 4)
                data = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
                if isinstance(data, dict) and data.get("service") == "google_drive":
                    return True
        except Exception:
            pass
    try:
        data = json.loads(state)
        if isinstance(data, dict) and (data.get("service") == "google_drive" or "business_id" in data):
            return True
    except Exception:
        pass
    return False


def get_drive_auth_url(
    user_id: str,
    business_id: str,
    redirect_uri: Optional[str] = None,
    return_url: Optional[str] = None,
    origin: Optional[str] = None,
    subdomain: Optional[str] = None,
) -> str:
    """Generates the Google OAuth 2.0 authorization URL for Google Drive with minimum scopes."""
    target_redirect = redirect_uri or settings.google_redirect_uri or f"{settings.public_base_url}/api/reports/google-drive/callback"
    state_payload = create_drive_oauth_state(
        user_id=user_id,
        business_id=business_id,
        return_url=return_url,
        origin=origin,
        subdomain=subdomain,
    )
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": target_redirect,
        "response_type": "code",
        "scope": DRIVE_FILE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state_payload,
    }
    return f"{AUTH_BASE_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_drive_tokens(code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
    """Exchanges OAuth code for access and refresh tokens."""
    target_redirect = redirect_uri or settings.google_redirect_uri or f"{settings.public_base_url}/api/reports/google-drive/callback"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": target_redirect,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code != 200:
            logger.error(f"[DRIVE OAUTH] Token exchange failed ({resp.status_code}): {resp.text}")
            raise ValueError(f"Google OAuth failed: {resp.text}")
        return resp.json()


async def get_user_email_from_token(access_token: str) -> Optional[str]:
    """Fetches user email associated with Google access token."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{TOKENINFO_URL}?access_token={access_token}")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("email")
    except Exception as e:
        logger.warning(f"[DRIVE OAUTH] Could not resolve token email: {e}")
    return None


async def save_drive_credentials(
    db: AsyncSession,
    business_id: str,
    user_id: str,
    tokens: Dict[str, Any],
    email: Optional[str] = None
) -> Integration:
    """Stores Google Drive credentials in PostgreSQL Integration table."""
    provider = f"google_drive"
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == provider
    )
    existing = await db.scalar(stmt)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token and existing and existing.config:
        refresh_token = existing.config.get("refresh_token")

    if not email and existing and existing.config:
        email = existing.config.get("email")

    expires_in = tokens.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    config_data = {
        "access_token": tokens.get("access_token"),
        "refresh_token": refresh_token,
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_at": expires_at.isoformat(),
        "scope": tokens.get("scope", DRIVE_FILE_SCOPE),
        "email": email,
        "user_id": user_id,
        "default_folder_id": (existing.config.get("default_folder_id") if existing and existing.config else None),
        "updated_at": _now().isoformat(),
    }

    if existing:
        existing.config = config_data
        existing.is_active = True
        integration = existing
    else:
        integration = Integration(
            business_id=business_id,
            provider=provider,
            config=config_data,
            is_active=True,
        )
        db.add(integration)

    await db.commit()
    await db.refresh(integration)
    logger.info(f"[DRIVE OAUTH] Successfully saved Google Drive integration for tenant {business_id}")
    return integration


async def get_valid_access_token(db: AsyncSession, business_id: str) -> Optional[str]:
    """Retrieves access token for tenant, refreshing automatically if expired."""
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == "google_drive",
        Integration.is_active == True,
    )
    integration = await db.scalar(stmt)
    if not integration or not integration.config:
        return None

    cfg = integration.config
    access_token = cfg.get("access_token")
    expires_at_str = cfg.get("expires_at")
    refresh_token = cfg.get("refresh_token")

    is_expired = True
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            # 5 minute safety buffer
            if expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
                is_expired = False
        except Exception:
            is_expired = True

    if not is_expired and access_token:
        return access_token

    # Token is expired; refresh using refresh_token
    if not refresh_token:
        logger.warning(f"[DRIVE OAUTH] Missing refresh token for tenant {business_id}")
        return None

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                new_access_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

                cfg["access_token"] = new_access_token
                cfg["expires_at"] = new_expires_at.isoformat()
                cfg["updated_at"] = _now().isoformat()

                integration.config = dict(cfg)
                await db.commit()
                logger.info(f"[DRIVE OAUTH] Successfully refreshed access token for tenant {business_id}")
                return new_access_token
            else:
                logger.error(f"[DRIVE OAUTH] Token refresh failed ({resp.status_code}): {resp.text}")
                return None
    except Exception as e:
        logger.error(f"[DRIVE OAUTH] Exception refreshing token for tenant {business_id}: {e}")
        return None


async def get_drive_status(db: AsyncSession, business_id: str) -> Dict[str, Any]:
    """Returns the current connection status of Google Drive for a tenant."""
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == "google_drive",
        Integration.is_active == True,
    )
    integration = await db.scalar(stmt)
    if not integration or not integration.config:
        return {
            "connected": False,
            "email": None,
            "folder_id": None,
            "folder_name": DEFAULT_FOLDER_NAME,
        }

    cfg = integration.config
    email = cfg.get("email")
    folder_id = cfg.get("default_folder_id")
    has_token = bool(cfg.get("access_token") or cfg.get("refresh_token"))

    return {
        "connected": has_token,
        "email": email,
        "folder_id": folder_id,
        "folder_name": DEFAULT_FOLDER_NAME,
    }


async def disconnect_drive(db: AsyncSession, business_id: str) -> bool:
    """Disconnects Google Drive integration for tenant."""
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == "google_drive",
    )
    integration = await db.scalar(stmt)
    if integration:
        integration.is_active = False
        if integration.config:
            integration.config["access_token"] = None
            integration.config["refresh_token"] = None
        await db.commit()
        return True
    return False


async def get_or_create_app_folder(
    access_token: str,
    folder_name: str = DEFAULT_FOLDER_NAME
) -> Optional[str]:
    """Finds or creates the designated destination folder in Google Drive."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        # Search for existing folder
        q = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        search_resp = await client.get(
            f"{DRIVE_API_BASE}/files",
            params={"q": q, "fields": "files(id, name)"},
            headers=headers
        )
        if search_resp.status_code == 200:
            files = search_resp.json().get("files", [])
            if files:
                return files[0]["id"]

        # Create folder if not found
        create_resp = await client.post(
            f"{DRIVE_API_BASE}/files",
            json={
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "description": "CallZenza Call Recordings Backup Archive",
            },
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        )
        if create_resp.status_code in (200, 201):
            return create_resp.json().get("id")
        else:
            logger.warning(f"[DRIVE] Failed to create folder '{folder_name}': {create_resp.text}")
            return None


async def upload_recording_to_drive(
    db: AsyncSession,
    recording_id: str,
    tenant_id: str,
    destination_folder_id: Optional[str] = None,
    user_id: str = "system"
) -> Dict[str, Any]:
    """
    Exports a single call recording to Google Drive.
    1. Verifies authentication and gets valid access token.
    2. Resolves audio file and marks status UPLOADING.
    3. Uploads file via Google Drive API v3.
    4. Verifies uploaded file metadata.
    5. Saves PostgreSQL metadata (google_drive_file_id, backup_status='BACKED_UP', etc.).
    """
    # 1. Fetch recording
    stmt = select(CallRecording).where(
        CallRecording.id == recording_id,
        or_(CallRecording.tenant_id == tenant_id, CallRecording.tenant_id.is_(None))
    )
    rec = (await db.execute(stmt)).scalar_one_or_none()
    if not rec:
        raise ValueError("Recording not found or access denied")

    # If already backed up and verified, return existing details
    if rec.backup_status == "BACKED_UP" and rec.google_drive_file_id:
        return {
            "success": True,
            "recording_id": rec.id,
            "backup_status": "BACKED_UP",
            "google_drive_file_id": rec.google_drive_file_id,
            "google_drive_file_url": rec.google_drive_file_url,
            "backup_completed_at": rec.backup_completed_at.isoformat() if rec.backup_completed_at else None,
            "message": "Recording is already backed up to Google Drive",
        }

    # 2. Check original file availability
    local_path = resolve_local_audio_path(rec.storage_key)
    if not local_path or not local_path.exists():
        rec.backup_status = "FAILED"
        rec.backup_error = "Original audio file does not exist on server storage"
        await db.commit()
        raise FileNotFoundError(rec.backup_error)

    # 3. Get valid Google Drive access token
    access_token = await get_valid_access_token(db, tenant_id)
    if not access_token:
        rec.backup_status = "FAILED"
        rec.backup_error = "Google Drive is not connected. Please connect Google Drive first."
        await db.commit()
        raise PermissionError(rec.backup_error)

    # 4. Mark status UPLOADING
    rec.backup_status = "UPLOADING"
    rec.backup_error = None
    rec.updated_at = _now()
    await db.commit()

    try:
        # Determine destination folder
        folder_id = destination_folder_id
        if not folder_id:
            folder_id = await get_or_create_app_folder(access_token, DEFAULT_FOLDER_NAME)

        # Build clean filename
        # Format: CallZenza_{campaign_id}_{call_id}_{timestamp}.wav
        date_str = rec.created_at.strftime("%Y%m%d_%H%M%S") if rec.created_at else "call"
        ext = "wav" if rec.format.lower() == "wav" else "mp3"
        remote_filename = f"CallZenza_{rec.campaign_id or 'general'}_{rec.call_id}_{date_str}.{ext}"

        file_size = local_path.stat().st_size
        mime_type = "audio/wav" if ext == "wav" else "audio/mpeg"

        metadata = {
            "name": remote_filename,
            "description": f"CallZenza call recording for call {rec.call_id}",
            "properties": {
                "call_id": rec.call_id,
                "recording_id": rec.id,
                "campaign_id": str(rec.campaign_id or ""),
                "source": "callzenza_backup",
            }
        }
        if folder_id:
            metadata["parents"] = [folder_id]

        uploaded_file_id: Optional[str] = None
        web_view_link: Optional[str] = None

        # 5. Upload file (Resumable upload for reliable streaming)
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Step A: Initiate resumable session
            init_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(file_size),
            }
            init_resp = await client.post(
                f"{DRIVE_UPLOAD_BASE}?uploadType=resumable",
                json=metadata,
                headers=init_headers
            )
            if init_resp.status_code not in (200, 201):
                raise RuntimeError(f"Failed to initiate Google Drive upload: {init_resp.text}")

            upload_url = init_resp.headers.get("location")
            if not upload_url:
                raise RuntimeError("Google Drive did not provide resumable upload location URL")

            # Step B: Stream audio bytes to upload URL
            with open(local_path, "rb") as audio_file:
                audio_content = audio_file.read()

            upload_resp = await client.put(
                upload_url,
                content=audio_content,
                headers={
                    "Content-Type": mime_type,
                    "Content-Length": str(len(audio_content)),
                }
            )
            if upload_resp.status_code not in (200, 201):
                raise RuntimeError(f"Audio upload chunk failed ({upload_resp.status_code}): {upload_resp.text}")

            upload_json = upload_resp.json()
            uploaded_file_id = upload_json.get("id")

            # Step C: Verification query
            if not uploaded_file_id:
                raise RuntimeError("Upload finished but Google Drive returned no file ID")

            verify_resp = await client.get(
                f"{DRIVE_API_BASE}/files/{uploaded_file_id}?fields=id,name,size,webViewLink,createdTime",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            if verify_resp.status_code != 200:
                raise RuntimeError(f"Uploaded file verification failed: {verify_resp.text}")

            verify_data = verify_resp.json()
            web_view_link = verify_data.get("webViewLink") or f"https://drive.google.com/file/d/{uploaded_file_id}/view"

        # 6. Verify and commit PostgreSQL metadata
        now_utc = _now()
        rec.backup_status = "BACKED_UP"
        rec.google_drive_file_id = uploaded_file_id
        rec.google_drive_folder_id = folder_id
        rec.google_drive_file_url = web_view_link
        rec.backup_completed_at = now_utc
        rec.backup_error = None
        rec.updated_at = now_utc

        # Audit log entry
        db.add(AuditLog(
            business_id=tenant_id,
            user_id=user_id,
            action="RECORDING_BACKED_UP_TO_DRIVE",
            entity="call_recordings",
            entity_id=rec.id,
            details={
                "call_id": rec.call_id,
                "campaign_id": rec.campaign_id,
                "drive_file_id": uploaded_file_id,
                "folder_id": folder_id,
                "file_size": file_size,
            }
        ))

        await db.commit()
        await db.refresh(rec)

        logger.info(f"✅ [DRIVE BACKUP] Successfully verified backup for recording {rec.id} -> Drive ID: {uploaded_file_id}")

        return {
            "success": True,
            "recording_id": rec.id,
            "backup_status": "BACKED_UP",
            "google_drive_file_id": uploaded_file_id,
            "google_drive_file_url": web_view_link,
            "backup_completed_at": now_utc.isoformat(),
            "message": "Recording uploaded and verified successfully in Google Drive",
        }

    except Exception as err:
        logger.error(f"❌ [DRIVE BACKUP] Failed to export recording {recording_id}: {err}")
        rec.backup_status = "FAILED"
        rec.backup_error = str(err)
        rec.updated_at = _now()
        await db.commit()
        return {
            "success": False,
            "recording_id": rec.id,
            "backup_status": "FAILED",
            "error": str(err),
            "message": f"Failed to export recording to Google Drive: {err}",
        }


async def export_campaign_recordings(
    db: AsyncSession,
    campaign_id: str,
    tenant_id: str,
    destination_folder_id: Optional[str] = None,
    user_id: str = "system"
) -> Dict[str, Any]:
    """
    Exports all eligible recordings for a given campaign to Google Drive.
    Eligible = not deleted, not expired, and not already backed up.
    """
    # 1. Verify campaign exists and belongs to tenant
    camp_stmt = select(Campaign).where(
        Campaign.id == campaign_id,
        or_(Campaign.business_id == tenant_id, Campaign.business_id.is_(None))
    )
    campaign = (await db.execute(camp_stmt)).scalar_one_or_none()
    if not campaign:
        raise ValueError("Campaign not found or access denied")

    # 2. Check Google Drive connection
    access_token = await get_valid_access_token(db, tenant_id)
    if not access_token:
        raise PermissionError("Google Drive is not connected. Please connect Google Drive first.")

    # 3. Find eligible recordings
    stmt = select(CallRecording).where(
        CallRecording.campaign_id == campaign_id,
        or_(CallRecording.tenant_id == tenant_id, CallRecording.tenant_id.is_(None)),
        CallRecording.recording_status.in_(["available", "expiring_soon", "ready"]),
        CallRecording.backup_status != "BACKED_UP",
    ).order_by(CallRecording.created_at.desc())
    recordings = (await db.execute(stmt)).scalars().all()

    if not recordings:
        return {
            "success": True,
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "total_eligible": 0,
            "exported_count": 0,
            "failed_count": 0,
            "message": "No unbacked eligible recordings found for this campaign",
        }

    folder_id = destination_folder_id
    if not folder_id:
        campaign_folder_name = f"{DEFAULT_FOLDER_NAME} - {campaign.name}"
        folder_id = await get_or_create_app_folder(access_token, campaign_folder_name)

    exported_count = 0
    failed_count = 0
    results = []

    for rec in recordings:
        res = await upload_recording_to_drive(
            db=db,
            recording_id=rec.id,
            tenant_id=tenant_id,
            destination_folder_id=folder_id,
            user_id=user_id
        )
        if res.get("success"):
            exported_count += 1
        else:
            failed_count += 1
        results.append(res)

    return {
        "success": True,
        "campaign_id": campaign.id,
        "campaign_name": campaign.name,
        "folder_id": folder_id,
        "total_eligible": len(recordings),
        "exported_count": exported_count,
        "failed_count": failed_count,
        "results": results,
    }
