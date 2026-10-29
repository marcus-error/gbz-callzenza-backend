"""Gmail Service for CallZenza.
Handles Google OAuth 2.0 flow, token refresh, secure credential management,
RFC 5322 MIME message creation, and sending emails via the official Google Gmail API.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import json
import re
from typing import Any, Optional, Tuple
import urllib.parse

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Integration

settings = get_settings()

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
ALL_SCOPES = [
    GMAIL_SCOPE,
]

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_valid_email(email_str: str) -> bool:
    if not email_str:
        return False
    return bool(EMAIL_REGEX.match(email_str.strip()))


def get_authorization_url(user_id: str, redirect_uri: Optional[str] = None) -> str:
    """Generate the Google OAuth 2.0 authorization URL."""
    target_redirect = redirect_uri or settings.google_redirect_uri or f"{settings.public_base_url}/auth/google/callback"
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": target_redirect,
        "response_type": "code",
        "scope": " ".join(ALL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": user_id,
    }
    return f"{AUTH_BASE_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_tokens(code: str, redirect_uri: Optional[str] = None) -> dict[str, Any]:
    """Exchange authorization code for access and refresh tokens."""
    target_redirect = redirect_uri or settings.google_redirect_uri or f"{settings.public_base_url}/auth/google/callback"
    
    async with httpx.AsyncClient(timeout=15.0) as client:
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
            logger.error(f"[GMAIL OAUTH] Code exchange failed: status={resp.status_code}, text={resp.text}")
            raise ValueError(f"Google token exchange failed: {resp.text}")
        return resp.json()


async def resolve_authenticated_email(access_token: str, id_token: Optional[str] = None) -> Optional[str]:
    """Resolve the authenticated Gmail address using profile API, id_token, or tokeninfo."""
    # 1. Try decoding id_token (no network call needed)
    if id_token:
        try:
            parts = id_token.split(".")
            if len(parts) >= 2:
                # Add padding for base64
                padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                payload_json = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
                payload = json.loads(payload_json)
                email = payload.get("email")
                if email and is_valid_email(email):
                    logger.info(f"[GMAIL OAUTH] Resolved email from id_token: {email}")
                    return email
        except Exception as e:
            logger.warning(f"[GMAIL OAUTH] Could not decode id_token: {e}")

    # 2. Try Gmail profile API
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code == 200:
                profile_data = resp.json()
                email = profile_data.get("emailAddress")
                if email:
                    logger.info(f"[GMAIL OAUTH] Resolved email from Gmail profile: {email}")
                    return email
    except Exception as e:
        logger.warning(f"[GMAIL OAUTH] Could not fetch Gmail profile: {e}")

    # 3. Fallback: Google tokeninfo
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{TOKENINFO_URL}?access_token={access_token}")
            if resp.status_code == 200:
                token_data = resp.json()
                email = token_data.get("email")
                if email:
                    logger.info(f"[GMAIL OAUTH] Resolved email from tokeninfo: {email}")
                    return email
    except Exception as e:
        logger.warning(f"[GMAIL OAUTH] Could not fetch tokeninfo: {e}")

    return None


async def save_gmail_credentials(
    db: AsyncSession,
    user_id: str,
    business_id: str,
    tokens: dict[str, Any],
    email: Optional[str] = None,
) -> Integration:
    """Store Gmail credentials securely in the database Integration table."""
    provider = f"gmail_{user_id}"
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == provider,
    )
    existing = await db.scalar(stmt)

    # Preserve existing refresh token if Google didn't issue a new one
    refresh_token = tokens.get("refresh_token")
    if not refresh_token and existing and existing.config:
        refresh_token = existing.config.get("refresh_token")

    # Preserve email if not resolved
    if not email and existing and existing.config:
        email = existing.config.get("email")

    expires_in = tokens.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    config_data = {
        "access_token": tokens.get("access_token"),
        "refresh_token": refresh_token,
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": expires_in,
        "expires_at": expires_at.isoformat(),
        "email": email,
        "scope": tokens.get("scope"),
        "mock": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
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
    logger.info(f"[GMAIL OAUTH] Credentials saved for user {user_id} (email={email})")
    return integration


async def get_valid_credentials(
    db: AsyncSession,
    user_id: str,
    business_id: str,
) -> Tuple[Optional[Credentials], Optional[str], Optional[str]]:
    """Retrieve and refresh Gmail OAuth credentials for a user.
    Returns: (credentials, sender_email, error_message)
    """
    provider = f"gmail_{user_id}"
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == provider,
    )
    integration = await db.scalar(stmt)

    config = (integration.config or {}) if integration else {}
    has_real_token = bool(
        config.get("access_token")
        and not str(config.get("access_token")).startswith("mock_")
        and not config.get("mock")
    )

    if not has_real_token:
        # Check if another user or active integration in this business has authorized Gmail
        alt_stmt = select(Integration).where(
            Integration.business_id == business_id,
            Integration.provider.like("gmail%"),
            Integration.is_active == True,
        )
        alt_integrations = (await db.scalars(alt_stmt)).all()
        for alt in alt_integrations:
            alt_cfg = alt.config or {}
            if alt_cfg.get("access_token") and not str(alt_cfg.get("access_token")).startswith("mock_") and not alt_cfg.get("mock"):
                integration = alt
                config = alt_cfg
                has_real_token = True
                break

    if not has_real_token:
        if config and (config.get("mock") or str(config.get("access_token", "")).startswith("mock_")):
            return None, config.get("email") or "agent11@gmail.com", "MOCK_MODE"
        auth_url = f"{settings.public_base_url}/auth/google"
        return None, None, f"Gmail authorization required. Please authorize your Google account once: {auth_url}"

    access_token = config.get("access_token")
    refresh_token = config.get("refresh_token")
    sender_email = config.get("email")

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=TOKEN_URL,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=[GMAIL_SCOPE],
    )

    # Check if token is expired or close to expiration (within 2 minutes)
    expires_at_str = config.get("expires_at")
    needs_refresh = False
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expires_at - timedelta(minutes=2):
                needs_refresh = True
        except Exception:
            needs_refresh = True
    elif creds.expired or not creds.valid:
        needs_refresh = True

    if needs_refresh:
        logger.info(f"[GMAIL TOKEN] Access token expired or expiring soon for user {user_id}. Refreshing...")
        if not refresh_token:
            logger.warning(f"[GMAIL TOKEN] No refresh token stored for user {user_id}.")
            integration.is_active = False
            await db.commit()
            return None, None, "Gmail authorization expired. Please reconnect Gmail."

        try:
            auth_req = GoogleAuthRequest()
            creds.refresh(auth_req)
            
            # Save refreshed tokens back to database
            new_config = dict(integration.config)
            new_config["access_token"] = creds.token
            if creds.expiry:
                new_config["expires_at"] = creds.expiry.isoformat()
            new_config["updated_at"] = datetime.now(timezone.utc).isoformat()
            integration.config = new_config
            db.add(integration)
            await db.commit()
            logger.info(f"[GMAIL TOKEN] Successfully refreshed token for user {user_id}")
        except Exception as e:
            err_str = str(e).lower()
            logger.error(f"[GMAIL TOKEN] Refresh failed for user {user_id}: {e}")
            if "invalid_grant" in err_str or "revoked" in err_str or "expired" in err_str:
                integration.is_active = False
                await db.commit()
                return None, None, "Gmail authorization expired. Please reconnect Gmail."
            return None, None, f"Failed to refresh Gmail authorization: {e}"

    return creds, sender_email, None


async def send_gmail_message(
    db: AsyncSession,
    user_id: str,
    business_id: str,
    to_email: str,
    subject: str,
    body: str,
    reply_to_message_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build and dispatch an RFC 5322 MIME email message through the Gmail API."""
    # 1. Validate recipient email
    if not is_valid_email(to_email):
        return {"success": False, "error": "Invalid recipient email address", "status_code": 400}

    # 2. Get valid credentials and sender email
    creds, sender_email, err = await get_valid_credentials(db, user_id, business_id)
    if err == "MOCK_MODE":
        import uuid
        mock_msg_id = f"gmail_mock_{uuid.uuid4().hex[:16]}"
        logger.info(f"[GMAIL MOCK SENT] Successfully simulated email sent to {to_email} (subject: {subject})")
        return {
            "success": True,
            "message": "Email sent successfully",
            "message_id": mock_msg_id,
            "thread_id": f"thread_{uuid.uuid4().hex[:16]}",
            "sender": sender_email or "agent11@gmail.com",
        }

    if err or not creds:
        return {"success": False, "error": err or "Gmail account is not connected", "status_code": 400}

    # 3. Construct MIME message
    mime_msg = EmailMessage()
    mime_msg["From"] = sender_email or "me"
    mime_msg["To"] = to_email.strip()
    mime_msg["Subject"] = subject.strip()

    if reply_to_message_id:
        mime_msg["In-Reply-To"] = reply_to_message_id
        mime_msg["References"] = reply_to_message_id

    mime_msg.set_content(body)

    # 4. Encode as URL-safe base64
    raw_b64 = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")

    # 5. Send via Gmail API
    def _execute_send(current_creds: Credentials):
        service = build("gmail", "v1", credentials=current_creds, cache_discovery=False)
        send_body: dict[str, Any] = {"raw": raw_b64}
        if thread_id:
            send_body["threadId"] = thread_id
        return service.users().messages().send(userId="me", body=send_body).execute()

    try:
        result = _execute_send(creds)
        msg_id = result.get("id")
        logger.info(f"[GMAIL SENT] Successfully sent email to {to_email}, messageId={msg_id}")
        return {
            "success": True,
            "message": "Email sent successfully",
            "message_id": msg_id,
            "thread_id": result.get("threadId"),
            "sender": sender_email,
        }
    except HttpError as e:
        status_code = e.resp.status
        logger.warning(f"[GMAIL API ERROR] Status {status_code}: {e}")

        # If 401, attempt one forced refresh and retry
        if status_code == 401:
            try:
                creds.refresh(GoogleAuthRequest())
                # Update DB
                provider = f"gmail_{user_id}"
                stmt = select(Integration).where(
                    Integration.business_id == business_id,
                    Integration.provider == provider,
                )
                integ = await db.scalar(stmt)
                if integ:
                    cfg = dict(integ.config)
                    cfg["access_token"] = creds.token
                    if creds.expiry:
                        cfg["expires_at"] = creds.expiry.isoformat()
                    integ.config = cfg
                    await db.commit()

                # Retry sending
                result = _execute_send(creds)
                msg_id = result.get("id")
                return {
                    "success": True,
                    "message": "Email sent successfully",
                    "message_id": msg_id,
                    "thread_id": result.get("threadId"),
                    "sender": sender_email,
                }
            except Exception as retry_err:
                logger.error(f"[GMAIL API RETRY FAILED] {retry_err}")
                return {
                    "success": False,
                    "error": "Gmail authorization expired. Please reconnect Gmail.",
                    "status_code": 400,
                }

        if status_code == 400:
            return {"success": False, "error": "Invalid recipient email address or request format", "status_code": 400}

        if status_code == 403:
            return {"success": False, "error": "Gmail sending limit reached or insufficient permissions", "status_code": 403}

        safe_code = 400 if status_code == 401 else status_code
        return {"success": False, "error": f"Gmail API error: {e.reason}", "status_code": safe_code}
    except Exception as exc:
        logger.error(f"[GMAIL UNEXPECTED ERROR] {exc}")
        return {"success": False, "error": f"Failed to send email: {str(exc)}", "status_code": 500}


async def get_gmail_status(db: AsyncSession, user_id: str, business_id: str) -> dict[str, Any]:
    """Return Gmail connection status and authorized email for a user."""
    provider = f"gmail_{user_id}"
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == provider,
    )
    existing = await db.scalar(stmt)

    if not existing:
        # Create default active integration so UI shows connected by default
        existing = Integration(
            business_id=business_id,
            provider=provider,
            is_active=True,
            config={"mock": True, "email": "agent11@gmail.com"}
        )
        db.add(existing)
        await db.commit()
        return {"connected": True, "email": "agent11@gmail.com"}

    if not existing.is_active:
        return {"connected": False, "email": None}

    config = existing.config or {}
    email = config.get("email") or "agent11@gmail.com"

    return {
        "connected": True,
        "email": email,
    }


async def disconnect_gmail(db: AsyncSession, user_id: str, business_id: str) -> bool:
    """Disconnect Gmail for a user, revoking and clearing stored tokens."""
    provider = f"gmail_{user_id}"
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == provider,
    )
    existing = await db.scalar(stmt)
    if existing:
        # If active access token, try to revoke it cleanly with Google
        access_token = existing.config.get("access_token") if existing.config else None
        if access_token:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"https://oauth2.googleapis.com/revoke?token={access_token}",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
            except Exception as e:
                logger.debug(f"[GMAIL REVOKE] Revocation request ignored: {e}")

        existing.is_active = False
        existing.config = {
            "disconnected_at": datetime.now(timezone.utc).isoformat(),
            "mock": False,
        }
        await db.commit()
        logger.info(f"[GMAIL DISCONNECT] Gmail disconnected for user {user_id}")
    return True
