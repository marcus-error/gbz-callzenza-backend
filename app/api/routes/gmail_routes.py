"""Gmail and Google OAuth API endpoints for CallZenza.
Provides standard, Swagger-documented routes for Gmail integration:
- GET  /auth/google
- GET  /auth/google/callback
- GET  /gmail/status
- POST /gmail/send
- POST /gmail/disconnect
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from loguru import logger
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_db_user
from app.core.security import decode_access_token
from app.db.database import get_db
from app.db.models import Conversation, EmailLog, Lead, Message, User
from app.services import gmail_service
from app.services.google_drive_service import (
    is_drive_oauth_state,
    decode_drive_oauth_state,
    sanitize_return_url,
    resolve_frontend_base_url,
    exchange_code_for_drive_tokens,
    get_user_email_from_token,
    save_drive_credentials,
)

router = APIRouter(tags=["Gmail"])
settings = get_settings()

oauth2_scheme_opt = OAuth2PasswordBearer(tokenUrl="api/auth/login", auto_error=False)


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------
class GmailSendRequest(BaseModel):
    to: EmailStr
    subject: str
    body: str

    class Config:
        json_schema_extra = {
            "example": {
                "to": "customer@example.com",
                "subject": "Pricing information",
                "body": "Hello, this is a test email sent from CallZenza using Gmail API.",
            }
        }


class GmailSendResponse(BaseModel):
    success: bool
    message: str
    message_id: Optional[str] = None
    error: Optional[str] = None


class GmailStatusResponse(BaseModel):
    connected: bool
    email: Optional[str] = None


class GmailDisconnectResponse(BaseModel):
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Helper to resolve user for OAuth initiation
# ---------------------------------------------------------------------------
async def _resolve_user_for_oauth(
    token: Optional[str],
    auth_header: Optional[str],
    db: AsyncSession,
) -> Optional[User]:
    raw_token = token or auth_header
    if raw_token:
        if raw_token.startswith("Bearer "):
            raw_token = raw_token[7:]
        try:
            payload = decode_access_token(raw_token)
            user_id = payload.get("sub")
            if user_id:
                user = await db.get(User, user_id)
                if user:
                    return user
        except Exception as e:
            logger.debug(f"[GMAIL OAUTH] Could not decode token: {e}")

    # Fallback to agent11@gmail.com first, then first active user
    stmt = select(User).where(User.email == "agent11@gmail.com")
    agent = await db.scalar(stmt)
    if agent:
        return agent

    stmt = select(User).where(User.is_active == True).order_by(User.created_at)
    res = await db.execute(stmt)
    return res.scalars().first()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get(
    "/auth/google",
    summary="Initiate Google OAuth 2.0 flow for Gmail",
    description="Generates the Google OAuth authorization URL with Gmail sending scope and redirects the user.",
)
async def auth_google(
    token: Optional[str] = Query(None, description="JWT auth token of the user"),
    auth_token: Optional[str] = Depends(oauth2_scheme_opt),
    redirect_uri: Optional[str] = Query(None, description="Optional redirect URI override"),
    db: AsyncSession = Depends(get_db),
):
    user = await _resolve_user_for_oauth(token, auth_token, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to connect Gmail",
        )

    auth_url = gmail_service.get_authorization_url(
        user_id=user.id,
        redirect_uri=redirect_uri or settings.google_redirect_uri,
    )
    logger.info(f"[GMAIL OAUTH] Redirecting user {user.id} ({user.email}) to Google OAuth")
    return RedirectResponse(url=auth_url)


@router.get(
    "/auth/google/callback",
    summary="Google OAuth 2.0 Callback",
    description="Receives authorization code from Google, exchanges it for tokens, stores credentials, and redirects to frontend.",
)
async def auth_google_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None, description="OAuth state payload"),
    error: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    # -------------------------------------------------------------------------
    # 1. Dispatch Google Drive Export & Backup OAuth Flow
    # -------------------------------------------------------------------------
    if is_drive_oauth_state(state):
        drive_payload = decode_drive_oauth_state(state)
        default_frontend = (getattr(settings, "frontend_url", None) or os.getenv("FRONTEND_URL", "http://localhost:3000")).rstrip("/")
        if not drive_payload:
            logger.warning("[DRIVE OAUTH] Rejected invalid or expired OAuth state")
            return RedirectResponse(
                url=f"{default_frontend}/reports?tab=call-recordings&error=invalid_or_expired_state"
            )

        user_id = str(drive_payload.get("user_id") or "system")
        business_id = str(drive_payload.get("business_id") or "default")
        return_url = sanitize_return_url(drive_payload.get("return_url"))
        frontend_base = resolve_frontend_base_url(
            origin=drive_payload.get("origin"),
            subdomain=drive_payload.get("subdomain"),
        )
        sep = "&" if "?" in return_url else "?"
        destination = f"{frontend_base}{return_url}"

        # Handle user cancellation / denied permission
        if error:
            logger.warning(f"[DRIVE OAUTH] Google OAuth callback returned error: {error}")
            return RedirectResponse(url=f"{destination}{sep}error={urllib.parse.quote_plus(error)}")

        if not code:
            logger.warning("[DRIVE OAUTH] Missing authorization code")
            return RedirectResponse(url=f"{destination}{sep}error=missing_code")

        try:
            tokens = await exchange_code_for_drive_tokens(
                code=code,
                redirect_uri=settings.google_redirect_uri,
            )
            access_token = tokens.get("access_token", "")
            drive_email = await get_user_email_from_token(access_token) if access_token else None

            await save_drive_credentials(
                db=db,
                business_id=business_id,
                user_id=user_id,
                tokens=tokens,
                email=drive_email,
            )

            logger.info(f"[DRIVE OAUTH] Successfully connected Google Drive for user {user_id}, tenant {business_id}: {drive_email}")
            return RedirectResponse(url=f"{destination}{sep}drive_connected=true")
        except Exception as e:
            logger.error(f"[DRIVE OAUTH] Token exchange/save failed: {e}")
            return RedirectResponse(url=f"{destination}{sep}error=oauth_failed")

    # -------------------------------------------------------------------------
    # 2. Existing Gmail OAuth Flow (Unchanged)
    # -------------------------------------------------------------------------
    frontend_base = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    messages_url = f"{frontend_base}/voice-agent/dashboard?tab=messages"

    if error:
        logger.warning(f"[GMAIL OAUTH] OAuth callback returned error: {error}")
        return RedirectResponse(url=f"{messages_url}&error={error}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    user_id = state
    user = None
    if user_id:
        user = await db.get(User, user_id)

    if not user:
        stmt = select(User).where(User.email == "agent11@gmail.com")
        user = await db.scalar(stmt)

    if not user:
        # Fallback to first user
        stmt = select(User).where(User.is_active == True).order_by(User.created_at)
        user = (await db.execute(stmt)).scalars().first()

    if not user:
        raise HTTPException(status_code=400, detail="User not found for OAuth state")

    try:
        # 1. Exchange code for tokens
        tokens = await gmail_service.exchange_code_for_tokens(
            code=code,
            redirect_uri=settings.google_redirect_uri,
        )

        # 2. Resolve authenticated Gmail address
        access_token = tokens.get("access_token", "")
        id_token = tokens.get("id_token")
        gmail_address = await gmail_service.resolve_authenticated_email(access_token, id_token)

        # 3. Store credentials in database
        await gmail_service.save_gmail_credentials(
            db=db,
            user_id=user.id,
            business_id=user.business_id,
            tokens=tokens,
            email=gmail_address,
        )

        logger.info(f"[GMAIL OAUTH] Successfully connected Gmail for user {user.id}: {gmail_address}")

        html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Google Gmail Connected</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #F8FAFC; color: #1D2433; }}
    .card {{ background: white; border: 1px solid #E1E4EA; border-radius: 12px; padding: 32px; text-align: center; box-shadow: 0 4px 12px rgba(0,0,0,0.05); max-width: 420px; }}
    h2 {{ color: #4F46E5; margin-top: 0; font-size: 20px; }}
    p {{ font-size: 13px; color: #64748B; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Gmail Connected Successfully!</h2>
    <p>Authorized as <b>{gmail_address or user.email}</b>.<br/>You can now compose and send emails directly.</p>
    <p style="font-size: 11px; color: #94A3B8;">This window will close automatically...</p>
  </div>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage({{ type: 'GMAIL_AUTH_SUCCESS', email: '{gmail_address}' }}, '*');
        setTimeout(() => {{ window.close(); }}, 1200);
      }} else {{
        setTimeout(() => {{ window.location.href = '{messages_url}&connected=true'; }}, 1200);
      }}
    }} catch (e) {{
      window.location.href = '{messages_url}&connected=true';
    }}
  </script>
</body>
</html>"""
        return HTMLResponse(content=html_content)
    except Exception as e:
        logger.error(f"[GMAIL OAUTH] Token exchange/save failed: {e}")
        return RedirectResponse(url=f"{messages_url}&error=oauth_failed")


@router.get(
    "/gmail/status",
    response_model=GmailStatusResponse,
    summary="Get Gmail connection status",
    description="Returns whether the current user has an active, authenticated Gmail OAuth connection and the connected email address.",
)
async def get_gmail_connection_status(
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    status_data = await gmail_service.get_gmail_status(
        db=db,
        user_id=current_user.id,
        business_id=current_user.business_id,
    )
    return GmailStatusResponse(
        connected=status_data["connected"],
        email=status_data.get("email"),
    )


@router.post(
    "/gmail/send",
    response_model=GmailSendResponse,
    summary="Send an email via connected Gmail account",
    description="Sends an email using the authenticated Gmail account via Google Gmail API. The From address is automatically set to the authenticated Gmail account.",
)
async def send_gmail_email(
    payload: GmailSendRequest,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    # Dispatch email via Gmail API
    result = await gmail_service.send_gmail_message(
        db=db,
        user_id=current_user.id,
        business_id=current_user.business_id,
        to_email=str(payload.to),
        subject=payload.subject,
        body=payload.body,
    )

    if not result.get("success"):
        status_code = result.get("status_code", 400)
        if status_code == 401:
            status_code = 400
        error_msg = result.get("error", "Failed to send email via Gmail API")
        raise HTTPException(status_code=status_code, detail=error_msg)

    message_id = result.get("message_id")
    sender_email = result.get("sender") or current_user.email

    # Log in EmailLog
    email_log = EmailLog(
        business_id=current_user.business_id,
        to_email=str(payload.to),
        subject=payload.subject,
        status="sent",
        resend_id=f"gmail_{message_id}",
    )
    db.add(email_log)

    # Find or create Lead for contact record
    stmt_lead = select(Lead).where(
        Lead.email == str(payload.to),
        Lead.business_id == current_user.business_id,
    )
    lead = await db.scalar(stmt_lead)
    if not lead:
        lead = Lead(
            business_id=current_user.business_id,
            first_name=str(payload.to).split("@")[0].capitalize(),
            email=str(payload.to),
            status="NEW",
            source="EMAIL",
        )
        db.add(lead)
        await db.flush()

    # Find or create Conversation
    stmt_conv = select(Conversation).where(
        Conversation.lead_id == lead.id,
        Conversation.channel == "email",
        Conversation.business_id == current_user.business_id,
    )
    conv = await db.scalar(stmt_conv)
    if not conv:
        conv = Conversation(
            business_id=current_user.business_id,
            lead_id=lead.id,
            channel="email",
            status="ACTIVE",
        )
        db.add(conv)
        await db.flush()

    # Record Message
    time_now = gmail_service._now().strftime("%b %d, %I:%M %p")
    msg = Message(
        conversation_id=conv.id,
        sender_type="agent",
        sender_id=current_user.id,
        message_type="TEXT",
        content=payload.body,
        meta={
            "subject": payload.subject,
            "from_name": current_user.full_name or "You",
            "from_email": sender_email,
            "to_email": str(payload.to),
            "gmail_message_id": message_id,
            "provider": "gmail",
            "status": "sent",
            "time": time_now,
            "unread": False,
            "replies": [],
        },
    )
    db.add(msg)
    await db.commit()

    return GmailSendResponse(
        success=True,
        message="Email sent successfully",
        message_id=message_id,
    )


@router.post(
    "/gmail/disconnect",
    response_model=GmailDisconnectResponse,
    summary="Disconnect Gmail integration",
    description="Revokes stored tokens, deactivates the integration, and resets Gmail connection status.",
)
async def disconnect_gmail_integration(
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    await gmail_service.disconnect_gmail(
        db=db,
        user_id=current_user.id,
        business_id=current_user.business_id,
    )
    return GmailDisconnectResponse(
        success=True,
        message="Gmail disconnected successfully",
    )
