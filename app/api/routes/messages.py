import os
import uuid
import asyncio
import random
from datetime import datetime, timezone, timedelta
import httpx
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger
from pydantic import BaseModel

from app.core.deps import get_current_business_id, get_current_db_user
from app.db.database import get_db, AsyncSessionLocal
from app.db.models import Integration, Lead, Conversation, Message, User, EmailLog, Business
from app.models.file import TenantFile
from app.core.config import get_settings
from app.services.whatsapp_service import send_whatsapp_message
from app.services.email_service import send_email
from app.services import gmail_service
from app.core.security import decode_access_token
from app.api.routes.websockets import global_ws_manager

router = APIRouter(prefix="/api/messages", tags=["messages"])
settings = get_settings()

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SendWhatsAppRequest(BaseModel):
    content: str

class EmailAttachment(BaseModel):
    filename: str
    content_base64: Optional[str] = None
    file_id: Optional[str] = None
    content_type: Optional[str] = None

class SendEmailRequest(BaseModel):
    to_email: str
    subject: str
    body: str
    attachments: Optional[list[EmailAttachment]] = None

class ReplyEmailRequest(BaseModel):
    body: str

def normalize_e164_phone(phone: str) -> str:
    """Normalizes any phone string into standard E.164 format (+<country_code><number>)."""
    if not phone:
        return ""
    raw = str(phone).strip()
    if raw.startswith("new-"):
        raw = raw[4:].strip()
    has_leading_plus = raw.startswith("+")
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    if digits.startswith("00") and len(digits) > 2:
        return f"+{digits[2:]}"
    if digits.startswith("0") and len(digits) == 11:
        return f"+91{digits[1:]}"
    elif digits.startswith("0"):
        digits = digits.lstrip("0")
    if has_leading_plus:
        return f"+{digits}"
    if len(digits) == 10:
        if digits[0] in ("6", "7", "8", "9"):
            return f"+91{digits}"
        return f"+1{digits}"
    if len(digits) >= 11:
        return f"+{digits}"
    return f"+{digits}"

AVATAR_COLORS = ["#6C63F5", "#10B981", "#F59E0B", "#EF4444", "#3B82F6", "#8B5CF6", "#EC4899", "#14B8A6"]
def get_avatar_color(key: str) -> str:
    if not key:
        return "#6C63F5"
    return AVATAR_COLORS[sum(ord(c) for c in key) % len(AVATAR_COLORS)]

class WhatsAppMessageOut(BaseModel):
    id: str
    from_type: str  # "customer" | "agent"
    text: str
    time: str
    conversation_id: Optional[str] = None
    contact_id: Optional[str] = None
    lead_id: Optional[str] = None
    recipient_phone: Optional[str] = None
    phone: Optional[str] = None
    status: str = "sent"
    latest_message: Optional[str] = None
    timestamp: Optional[str] = None

class WhatsAppChatOut(BaseModel):
    id: str
    name: str
    phone: str
    email: str = ""
    initials: str = "?"
    color: str = "#6C63F5"
    lastMessage: str = ""
    lastTime: str = ""
    unread: int = 0
    conversation_id: Optional[str] = None
    status: Optional[str] = "ACTIVE"

class EmailReplyOut(BaseModel):
    id: str
    from_name: str
    time: str
    body: str

class EmailThreadOut(BaseModel):
    id: str
    leadId: str
    subject: str
    from_name: str
    fromEmail: str
    time: str
    unread: bool
    body: str
    replies: list[EmailReplyOut]
    success: bool = True
    message: str = "Email sent successfully"
    message_id: Optional[str] = None

# ---------------------------------------------------------------------------
# Google OAuth 2.0 for Gmail endpoints
# ---------------------------------------------------------------------------
from fastapi.security import OAuth2PasswordBearer
oauth2_scheme_opt = OAuth2PasswordBearer(tokenUrl="api/auth/token", auto_error=False)

@router.get("/gmail/oauth/start")
async def gmail_oauth_start(
    token: str | None = None,
    auth_token: str | None = Depends(oauth2_scheme_opt),
    db: AsyncSession = Depends(get_db),
):
    """
    Initiates Google OAuth 2.0 flow using official Google Gmail API scopes.
    """
    active_token = token or auth_token
    if not active_token:
        raise HTTPException(status_code=401, detail="Authentication token is required")
        
    if active_token.startswith("Bearer "):
        active_token = active_token[7:]
        
    try:
        payload = decode_access_token(active_token)
        user_id = payload.get("sub")
    except Exception as e:
        logger.error(f"[GMAIL OAUTH] Failed to decode user token: {e}")
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    redirect_uri = settings.google_redirect_uri or f"{settings.public_base_url}/auth/google/callback"
    logger.info(f"[GMAIL OAUTH] Starting Google OAuth flow for user {user.id} -> {redirect_uri}")
    auth_url = gmail_service.get_authorization_url(user.id, redirect_uri)
    return RedirectResponse(url=auth_url)


@router.get("/gmail/oauth/callback")
async def gmail_oauth_callback(
    code: str,
    state: str,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    OAuth Callback: exchanges code for credentials and stores them securely.
    """
    frontend_base = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    messages_url = f"{frontend_base}/voice-agent/dashboard?tab=messages"

    if error:
        logger.warning(f"[GMAIL OAUTH] Callback received error: {error}")
        return RedirectResponse(url=f"{messages_url}&error={error}")

    user_id = state
    user = await db.get(User, user_id)
    if not user:
        # Fallback to first user
        stmt = select(User).where(User.is_active == True).order_by(User.created_at)
        user = (await db.execute(stmt)).scalars().first()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid state parameter: User not found")

    try:
        redirect_uri = settings.google_redirect_uri or f"{settings.public_base_url}/auth/google/callback"
        tokens = await gmail_service.exchange_code_for_tokens(code, redirect_uri)
        email = await gmail_service.resolve_authenticated_email(tokens.get("access_token"), tokens.get("id_token"))
        await gmail_service.save_gmail_credentials(db, user.id, user.business_id, tokens, email)
        return RedirectResponse(url=f"{messages_url}&connected=true")
    except Exception as e:
        logger.error(f"[GMAIL OAUTH] Code exchange or save failed: {e}")
        return RedirectResponse(url=f"{messages_url}&error=oauth_failed")


@router.get("/gmail/oauth/status")
async def get_gmail_status(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    status_info = await gmail_service.get_gmail_status(db, user.id, user.business_id)
    return {
        "connected": status_info["connected"],
        "email": status_info.get("email"),
    }


@router.post("/gmail/oauth/disconnect")
async def disconnect_gmail(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    await gmail_service.disconnect_gmail(db, user.id, user.business_id)
    return {"success": True}

# ---------------------------------------------------------------------------
# helper functions for Gmail Token refresh
# ---------------------------------------------------------------------------
async def _get_gmail_client(db: AsyncSession, user_id: str, business_id: str):
    provider = f"gmail_{user_id}"
    stmt = select(Integration).where(
        Integration.business_id == business_id,
        Integration.provider == provider,
        Integration.is_active == True
    )
    integration = await db.scalar(stmt)
    if not integration:
        return None
    
    config = integration.config
    if config.get("mock"):
        return "mock"

    # Check if real token needs refresh (simplified check for testing)
    # We will fetch and verify on actual call, and try refreshing if we get 401
    return integration

# ---------------------------------------------------------------------------
# Agent-isolated WhatsApp Config endpoints
# ---------------------------------------------------------------------------
class WhatsAppConfigPayload(BaseModel):
    provider: str
    phone_number_id: str
    access_token: str
    phone_number: str
    template: Optional[str] = None
    business_account_id: Optional[str] = None


@router.get("/whatsapp/config")
async def get_whatsapp_config(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    provider = f"whatsapp_{user.id}"
    stmt = select(Integration).where(
        Integration.business_id == user.business_id,
        Integration.provider == provider,
        Integration.is_active == True
    )
    existing = await db.scalar(stmt)
    if not existing:
        stmt_any = select(Integration).where(
            Integration.business_id == user.business_id,
            Integration.provider.startswith("whatsapp"),
            Integration.is_active == True
        )
        existing = await db.scalar(stmt_any)

    if not existing:
        if settings.whatsapp_token and settings.whatsapp_phone_number_id:
            return {
                "connected": True,
                "config": {
                    "provider": "Meta Cloud API (direct)",
                    "phone_number_id": settings.whatsapp_phone_number_id,
                    "phone_number": getattr(settings, "whatsapp_phone_number", "+1 (555) 159-5909"),
                    "business_account_id": getattr(settings, "whatsapp_business_account_id", "916553811148360"),
                    "template": "Hi {{name}}, your appointment is confirmed with CallZenza.",
                }
            }
        return {"connected": False}
        
    return {
        "connected": True,
        "config": {
            "provider": existing.config.get("provider", "Meta Cloud API (direct)"),
            "phone_number_id": existing.config.get("phone_number_id", settings.whatsapp_phone_number_id),
            "phone_number": existing.config.get("phone_number", getattr(settings, "whatsapp_phone_number", "+1 (555) 159-5909")),
            "business_account_id": existing.config.get("business_account_id", getattr(settings, "whatsapp_business_account_id", "916553811148360")),
            "template": existing.config.get("template", "Hi {{name}}, your appointment is confirmed with CallZenza."),
        }
    }


@router.post("/whatsapp/config")
async def save_whatsapp_config(
    payload: WhatsAppConfigPayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    provider = f"whatsapp_{user.id}"
    
    # Check if integration already exists
    stmt = select(Integration).where(
        Integration.business_id == user.business_id,
        Integration.provider == provider
    )
    existing = await db.scalar(stmt)
    
    # Clean access_token if user pasted WHATSAPP_TOKEN= or quotes
    raw_token = (payload.access_token or "").strip()
    if raw_token.startswith("WHATSAPP_TOKEN="):
        raw_token = raw_token[len("WHATSAPP_TOKEN="):].strip()
    raw_token = raw_token.strip("\"'")

    # If mask or empty, preserve existing real token or fall back to env
    if not raw_token or "•" in raw_token or any(ord(c) > 127 for c in raw_token):
        candidate = existing.config.get("access_token", "") if (existing and isinstance(existing.config, dict)) else ""
        if candidate and "•" not in candidate and not any(ord(c) > 127 for c in candidate):
            raw_token = candidate
        else:
            raw_token = (settings.whatsapp_token or "").replace("WHATSAPP_TOKEN=", "").strip("\"'").strip()

    phone_id = (payload.phone_number_id or "").strip() or settings.whatsapp_phone_number_id
    phone_num = (payload.phone_number or "").strip() or getattr(settings, "whatsapp_phone_number", "+1 (555) 159-5909")

    config_dict = {
        "provider": payload.provider or "Meta Cloud API (direct)",
        "phone_number_id": phone_id,
        "access_token": raw_token,
        "phone_number": phone_num,
        "business_account_id": getattr(settings, "whatsapp_business_account_id", "916553811148360"),
        "template": payload.template or "Hi {{name}}, your appointment is confirmed with CallZenza."
    }
    
    if existing:
        existing.config = config_dict
        existing.is_active = True
    else:
        existing = Integration(
            business_id=user.business_id,
            provider=provider,
            config=config_dict,
            is_active=True
        )
        db.add(existing)
        
    await db.commit()

    # Automatically synchronize WhatsApp credentials to .env file
    from app.core.env_utils import update_env_file
    wa_env_map = {}
    if raw_token and "•" not in raw_token:
        wa_env_map["WHATSAPP_TOKEN"] = raw_token
    if phone_id:
        wa_env_map["WHATSAPP_PHONE_NUMBER_ID"] = phone_id
    if phone_num:
        wa_env_map["WHATSAPP_PHONE_NUMBER"] = phone_num
    if payload.business_account_id:
        wa_env_map["WHATSAPP_BUSINESS_ACCOUNT_ID"] = payload.business_account_id
    if wa_env_map:
        update_env_file(wa_env_map)

    return {"success": True}


@router.post("/whatsapp/config/disconnect")
async def disconnect_whatsapp(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    provider = f"whatsapp_{user.id}"
    stmt = select(Integration).where(
        Integration.business_id == user.business_id,
        Integration.provider == provider
    )
    existing = await db.scalar(stmt)
    if existing:
        existing.is_active = False
        await db.commit()
        
    return {"success": True}


@router.post("/whatsapp/config/test")
async def test_whatsapp_connection(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    provider = f"whatsapp_{user.id}"
    stmt = select(Integration).where(
        Integration.business_id == user.business_id,
        Integration.provider == provider,
        Integration.is_active == True
    )
    existing = await db.scalar(stmt)
    if not existing:
        stmt_any = select(Integration).where(
            Integration.business_id == user.business_id,
            Integration.provider.startswith("whatsapp"),
            Integration.is_active == True
        )
        existing = await db.scalar(stmt_any)

    raw_tok = (existing.config.get("access_token") if existing else None) or settings.whatsapp_token or ""
    if raw_tok.startswith("WHATSAPP_TOKEN="):
        raw_tok = raw_tok[len("WHATSAPP_TOKEN="):].strip()
    token = raw_tok.strip("\"'")

    phone_id = (existing.config.get("phone_number_id") if existing else None) or settings.whatsapp_phone_number_id
    
    if not token or not phone_id:
        raise HTTPException(status_code=400, detail="WhatsApp integration not configured")
        
    url = f"https://graph.facebook.com/v23.0/{phone_id}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code == 200:
                return {"success": True, "detail": "Meta API connection healthy and verified!"}
            elif resp.status_code == 401:
                logger.warning(f"[WHATSAPP TEST] Meta 401 Unauthorized: {resp.text}")
                return {"success": False, "detail": "Status 401 Unauthorized: Access Token is expired or invalid. Please update the Meta Access Token."}
            else:
                logger.warning(f"[WHATSAPP TEST] Meta responded: {resp.status_code} - {resp.text}")
                return {"success": False, "detail": f"Meta API error (Status {resp.status_code}): Token or Phone ID rejected."}
        except Exception as e:
            logger.error(f"[WHATSAPP TEST] Request failed: {e}")
            return {"success": False, "detail": f"Failed to connect to Meta API: {str(e)}"}


class TestWhatsAppPayload(BaseModel):
    to_phone: str
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None
    message: Optional[str] = None


@router.post("/whatsapp/config/send-test")
async def send_test_whatsapp_endpoint(
    payload: TestWhatsAppPayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Sends a real test WhatsApp message to the specified recipient phone number."""
    provider = f"whatsapp_{user.id}"
    stmt = select(Integration).where(
        Integration.business_id == user.business_id,
        Integration.provider == provider,
        Integration.is_active == True,
    )
    existing = await db.scalar(stmt)
    if not existing:
        stmt_any = select(Integration).where(
            Integration.business_id == user.business_id,
            Integration.provider.startswith("whatsapp"),
            Integration.is_active == True,
        )
        existing = await db.scalar(stmt_any)

    raw_tok = ""
    if payload.access_token and "•" not in payload.access_token and not any(ord(c) > 127 for c in payload.access_token):
        raw_tok = payload.access_token
    elif existing and isinstance(existing.config, dict):
        db_tok = existing.config.get("access_token", "")
        if db_tok and "•" not in db_tok and not any(ord(c) > 127 for c in db_tok):
            raw_tok = db_tok

    if not raw_tok:
        raw_tok = settings.whatsapp_token or ""
    if raw_tok.startswith("WHATSAPP_TOKEN="):
        raw_tok = raw_tok[len("WHATSAPP_TOKEN="):].strip()
    token = raw_tok.strip("\"'")

    phone_id = (
        (payload.phone_number_id if payload.phone_number_id else None)
        or (existing.config.get("phone_number_id") if existing else None)
        or settings.whatsapp_phone_number_id
        or ""
    )

    if not token or not phone_id:
        raise HTTPException(status_code=400, detail="WhatsApp credentials (Access Token and Phone ID) are required.")

    test_msg = (
        payload.message
        or (existing.config.get("template") if existing and isinstance(existing.config, dict) else None)
        or "Hi {{name}}, your appointment is confirmed with CallZenza."
    )
    res = await send_whatsapp_message(
        to=payload.to_phone,
        text=test_msg,
        token=token,
        phone_number_id=phone_id,
    )

    if res.get("status") == "error":
        err_obj = res.get("error", {})
        err_msg = ""
        err_code = None
        if isinstance(err_obj, dict):
            inner_err = err_obj.get("error", {})
            err_msg = inner_err.get("message") or err_obj.get("message") or ""
            err_code = inner_err.get("code")

        if err_code == 190 or "Authentication Error" in str(err_msg) or "Error validating access token" in str(err_msg):
            return {
                "success": False,
                "message": (
                    "Meta Token Expired (#190): Your Meta Access Token has expired. "
                    "In Meta Developer Portal -> WhatsApp -> API Setup, copy the fresh 'Temporary access token', "
                    "paste it into Access Token, click SAVE CREDENTIALS, and try again."
                ),
                "details": res,
            }

        if err_code == 131005 or "131005" in str(err_msg):
            return {
                "success": False,
                "message": (
                    "Meta Access Denied (#131005): Current token lacks WABA messaging permission. "
                    "Quick Fix: Go to Meta Developer Portal -> WhatsApp -> API Setup -> click [Copy] on 'Temporary access token', "
                    "paste it into Access Token, click SAVE CREDENTIALS, then click SEND TEST MESSAGE."
                ),
                "details": res,
            }

        if err_code == 131030 or "131030" in str(err_msg):
            return {
                "success": False,
                "message": f"Meta Sandbox Restriction: Recipient number '{payload.to_phone}' is not in your Meta allowed test list. In Meta Developer Portal -> WhatsApp -> API Setup -> click 'Manage phone number list' and add {payload.to_phone}.",
                "details": res,
            }

        return {
            "success": False,
            "message": f"WhatsApp delivery failed: {err_msg or str(err_obj)}",
            "details": res,
        }
    elif res.get("status") == "stub":
        return {
            "success": False,
            "message": "WhatsApp credentials not configured on server.",
            "details": res,
        }

    return {
        "success": True,
        "message": f"✅ Real test WhatsApp message successfully sent to {payload.to_phone}!",
        "id": res.get("messages", [{}])[0].get("id") if "messages" in res else None,
        "details": res,
    }


# ---------------------------------------------------------------------------
# WhatsApp Webhook for Meta Cloud API (TS-029)
# Note: Defined before /{lead_id} routes to prevent FastAPI parameterized path conflict
# ---------------------------------------------------------------------------
@router.get("/whatsapp/webhook")
async def verify_whatsapp_webhook(request: Request):
    """Meta webhook verification endpoint."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    expected = getattr(settings, "whatsapp_verify_token", "callzenza_whatsapp_verify_token")
    valid_tokens = {
        expected,
        "callzenza_whatsapp_verify_token",
        "callzenza",
        "callmira",
        os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    }
    valid_tokens = {t for t in valid_tokens if t}
    if mode == "subscribe" and (token in valid_tokens or token == expected):
        logger.info(f"[WHATSAPP WEBHOOK] Handshake verified successfully with challenge: {challenge}")
        return Response(content=challenge or "", media_type="text/plain")
    logger.warning(f"[WHATSAPP WEBHOOK] Verification failed. Received token: '{token}', mode: '{mode}'")
    raise HTTPException(status_code=403, detail="Verification token mismatch")


@router.post("/whatsapp/webhook")
async def receive_whatsapp_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Receives incoming messages and status callbacks from Meta WhatsApp Cloud API."""
    try:
        body = await request.json()
    except Exception:
        return {"status": "ignored", "reason": "invalid_json"}

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})
            messages_list = val.get("messages", [])
            for m in messages_list:
                from_num = m.get("from")
                if not from_num:
                    continue

                content = ""
                m_type = m.get("type")
                if m_type == "text":
                    content = m.get("text", {}).get("body", "")
                elif m_type == "button":
                    content = m.get("button", {}).get("text", "")
                elif m_type == "interactive":
                    content = (
                        m.get("interactive", {}).get("button_reply", {}).get("title")
                        or m.get("interactive", {}).get("list_reply", {}).get("title", "")
                    )
                else:
                    content = f"[{m_type.capitalize() if m_type else 'Message'}]"

                if not content:
                    continue

                clean_phone = normalize_e164_phone(from_num)
                last_10 = "".join(filter(str.isdigit, from_num))[-10:] if len("".join(filter(str.isdigit, from_num))) >= 10 else clean_phone

                # Identify business_id from incoming metadata phone_number_id if possible
                phone_number_id = val.get("metadata", {}).get("phone_number_id")
                business_id = None
                if phone_number_id:
                    integ_stmt = select(Integration).where(
                        Integration.provider.startswith("whatsapp"),
                        Integration.is_active == True,
                    )
                    res_integs = await db.scalars(integ_stmt)
                    for integ in res_integs:
                        c = integ.config or {}
                        if c.get("phone_number_id") == phone_number_id or c.get("phone_id") == phone_number_id:
                            business_id = integ.business_id
                            break

                if not business_id and phone_number_id and phone_number_id == settings.whatsapp_phone_number_id:
                    first_integ = await db.scalar(select(Integration).where(Integration.provider.startswith("whatsapp"), Integration.is_active == True).limit(1))
                    if first_integ:
                        business_id = first_integ.business_id

                if not business_id:
                    stmt_b = select(Business.id).limit(1)
                    business_id = await db.scalar(stmt_b) or str(uuid.uuid4())

                logger.info(f"[WHATSAPP WEBHOOK] Received message from raw={from_num}, normalized={clean_phone}, business={business_id}")

                lead_stmt = select(Lead).where(
                    Lead.business_id == business_id,
                    or_(
                        Lead.phone == clean_phone,
                        Lead.phone == from_num,
                        Lead.phone == (clean_phone[1:] if clean_phone.startswith("+") else clean_phone),
                        Lead.phone.endswith(last_10) if len(last_10) >= 10 else False
                    )
                )
                lead = await db.scalar(lead_stmt)

                if not lead:
                    lead = Lead(
                        id=str(uuid.uuid4()),
                        business_id=business_id,
                        first_name=clean_phone,
                        phone=clean_phone,
                        status="NEW",
                        source="WHATSAPP",
                    )
                    db.add(lead)
                    await db.flush()
                    logger.info(f"[WHATSAPP WEBHOOK] Created new lead {lead.id} with phone {clean_phone}")
                else:
                    if lead.phone != clean_phone:
                        lead.phone = clean_phone
                        db.add(lead)
                        await db.flush()

                stmt_c = select(Conversation).where(
                    Conversation.business_id == business_id,
                    Conversation.lead_id == lead.id,
                    Conversation.channel == "whatsapp"
                )
                conv = await db.scalar(stmt_c)
                if not conv:
                    conv = Conversation(
                        business_id=business_id,
                        lead_id=lead.id,
                        channel="whatsapp",
                        status="ACTIVE"
                    )
                    db.add(conv)
                    await db.flush()
                    logger.info(f"[WHATSAPP WEBHOOK] Created new conversation {conv.id} for lead {lead.id}")

                time_now = _now().strftime("%I:%M %p")
                incoming_msg = Message(
                    conversation_id=conv.id,
                    sender_type="customer",
                    sender_id=lead.id,
                    content=content,
                    meta={"time": time_now, "wa_mid": m.get("id"), "recipient_phone": clean_phone}
                )
                db.add(incoming_msg)
                await db.commit()
                logger.info(f"[WHATSAPP WEBHOOK] Successfully stored message in conv {conv.id} from {clean_phone}: {content[:50]}")

    return {"status": "success"}


# ---------------------------------------------------------------------------
# WhatsApp Business Panel endpoints
# ---------------------------------------------------------------------------
@router.get("/whatsapp/conversations", response_model=list[WhatsAppChatOut])
@router.get("/whatsapp/chats", response_model=list[WhatsAppChatOut])
async def list_whatsapp_conversations(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns all active WhatsApp conversations with contact details, normalized phone, and latest messages."""
    stmt = (
        select(Conversation)
        .where(
            Conversation.channel == "whatsapp",
            Conversation.business_id == user.business_id,
        )
        .order_by(Conversation.updated_at.desc())
    )
    convs = (await db.scalars(stmt)).all()
    
    chats: list[WhatsAppChatOut] = []
    seen_contacts = set()

    for conv in convs:
        lead = await db.get(Lead, conv.lead_id)
        
        # Phone resolution and E.164 normalization
        raw_phone = lead.phone if lead else (conv.lead_id.replace("new-", "") if conv.lead_id.startswith("new-") else "")
        normalized_phone = normalize_e164_phone(raw_phone)
        
        # Deduplicate contacts by normalized phone or lead id
        dedup_key = normalized_phone or conv.lead_id
        if dedup_key in seen_contacts:
            continue
        seen_contacts.add(dedup_key)

        # Contact Name: If contact name is unavailable, display the phone number instead of an empty name (Req 6)
        lead_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else ""
        if not lead_name or lead_name.lower() in ("lead", "customer", "unknown", "none"):
            display_name = normalized_phone if normalized_phone else "Unknown"
        else:
            display_name = lead_name

        # Latest message in this conversation
        msg_stmt = (
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.timestamp.desc())
            .limit(1)
        )
        latest_msg = await db.scalar(msg_stmt)
        
        last_text = latest_msg.content if latest_msg else "No messages yet"
        last_time = ""
        if latest_msg:
            last_time = (latest_msg.meta or {}).get("time") or latest_msg.timestamp.strftime("%I:%M %p")

        # Unread count
        unread_cnt = 0
        try:
            msgs_all = (await db.scalars(select(Message).where(Message.conversation_id == conv.id))).all()
            unread_cnt = sum(1 for m in msgs_all if m.sender_type == "customer" and not (m.meta or {}).get("read", False))
        except Exception:
            pass

        initials = "?"
        if display_name and display_name != normalized_phone:
            initials = display_name[0].upper()
        elif normalized_phone:
            initials = normalized_phone[-2:]

        chats.append(
            WhatsAppChatOut(
                id=conv.lead_id,
                conversation_id=conv.id,
                name=display_name,
                phone=normalized_phone or raw_phone,
                email=lead.email if lead else "",
                initials=initials,
                color=get_avatar_color(conv.lead_id),
                lastMessage=last_text,
                lastTime=last_time,
                unread=unread_cnt,
                status=conv.status or "ACTIVE",
            )
        )

    logger.info(f"[WHATSAPP CONVERSATIONS] Retrieved {len(chats)} WhatsApp conversations for business {user.business_id}")
    return chats


@router.get("/whatsapp/{lead_id}", response_model=list[WhatsAppMessageOut])
async def list_whatsapp_messages(
    lead_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    lead = None
    if len(lead_id) == 36 and "-" in lead_id:
        lead = await db.get(Lead, lead_id)

    if not lead or lead.business_id != user.business_id:
        raw_phone = lead_id.replace("new-", "") if lead_id.startswith("new-") else lead_id
        normalized_phone = normalize_e164_phone(raw_phone) if (raw_phone.startswith("+") or raw_phone.isdigit() or raw_phone.startswith("new-")) else ""
        
        if normalized_phone:
            digits = "".join(filter(str.isdigit, normalized_phone))
            last_10 = digits[-10:] if len(digits) >= 10 else digits
            stmt = select(Lead).where(
                Lead.business_id == user.business_id,
                or_(
                    Lead.phone == normalized_phone,
                    Lead.phone == raw_phone,
                    Lead.phone.endswith(last_10)
                )
            )
            lead = await db.scalar(stmt)

    if not lead:
        return []

    # Find conversation
    stmt = select(Conversation).where(
        Conversation.lead_id == lead.id,
        Conversation.channel == "whatsapp",
        Conversation.business_id == user.business_id
    )
    conv = await db.scalar(stmt)
    if not conv:
        return []

    # Fetch messages
    msg_stmt = select(Message).where(Message.conversation_id == conv.id).order_by(Message.timestamp.asc())
    msg_res = await db.execute(msg_stmt)
    db_messages = msg_res.scalars().all()

    # Filter out any placeholder messages
    dummy_texts = {
        "Hi, following up on the plan you asked about — want me to send the details over here?",
        "Yes please, that'd be great",
        "Sent! It covers the setup fee and the monthly rate we discussed on the call.",
        "Sure, that works for me!",
    }
    filtered_messages = [m for m in db_messages if m.content not in dummy_texts]

    return [
        WhatsAppMessageOut(
            id=m.id,
            from_type="customer" if m.sender_type == "customer" else "agent",
            text=m.content,
            time=(m.meta or {}).get("time") or m.timestamp.strftime("%I:%M %p"),
            conversation_id=conv.id,
            contact_id=lead.id,
            lead_id=lead.id,
            recipient_phone=lead.phone,
            phone=lead.phone,
            status=(m.meta or {}).get("status", "sent"),
            timestamp=(m.meta or {}).get("timestamp") or m.timestamp.isoformat(),
        )
        for m in filtered_messages
    ]


@router.post("/whatsapp/{lead_id}/messages", response_model=WhatsAppMessageOut)
async def send_whatsapp_agent_message(
    lead_id: str,
    payload: SendWhatsAppRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    # 1. Phone extraction & normalization
    raw_phone = lead_id.replace("new-", "") if lead_id.startswith("new-") else lead_id
    normalized_phone = normalize_e164_phone(raw_phone) if (raw_phone.startswith("+") or raw_phone.isdigit() or raw_phone.startswith("new-")) else ""
    
    logger.info(f"[WHATSAPP SEND] Recipient number received: '{lead_id}' | Raw phone: '{raw_phone}' | Normalized E.164: '{normalized_phone}'")

    # 2. Contact lookup / deduplication
    lead = None
    if len(lead_id) == 36 and "-" in lead_id:
        lead = await db.get(Lead, lead_id)

    if not lead and normalized_phone:
        # Check by normalized phone or last 10 digits to prevent duplicate contacts!
        digits = "".join(filter(str.isdigit, normalized_phone))
        last_10 = digits[-10:] if len(digits) >= 10 else digits
        stmt_lookup = select(Lead).where(
            Lead.business_id == user.business_id,
            or_(
                Lead.phone == normalized_phone,
                Lead.phone == raw_phone,
                Lead.phone.endswith(last_10),
            )
        )
        lead = await db.scalar(stmt_lookup)

    if not lead:
        # Create new lead with normalized phone
        new_lead_id = str(uuid.uuid4())
        phone_to_store = normalized_phone if normalized_phone else raw_phone
        lead = Lead(
            id=new_lead_id,
            business_id=user.business_id,
            first_name=phone_to_store if phone_to_store else "Contact",
            last_name="",
            phone=phone_to_store,
            email="",
            status="NEW",
            source="WHATSAPP",
        )
        db.add(lead)
        await db.flush()
        logger.info(f"[WHATSAPP SEND] Created new contact {lead.id} with normalized phone {phone_to_store}")
    else:
        logger.info(f"[WHATSAPP SEND] Reusing existing contact {lead.id} ({lead.first_name} {lead.last_name} | {lead.phone})")
        # Ensure lead.phone is normalized if not already
        if normalized_phone and lead.phone != normalized_phone:
            lead.phone = normalized_phone
            db.add(lead)
            await db.flush()

    # 3. Conversation lookup / update / creation
    stmt_conv = select(Conversation).where(
        Conversation.lead_id == lead.id,
        Conversation.channel == "whatsapp",
        Conversation.business_id == user.business_id
    )
    conv = await db.scalar(stmt_conv)
    if not conv:
        conv = Conversation(
            id=str(uuid.uuid4()),
            business_id=user.business_id,
            lead_id=lead.id,
            channel="whatsapp",
            status="ACTIVE",
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(conv)
        await db.flush()
        logger.info(f"[WHATSAPP SEND] Created new conversation {conv.id} for lead {lead.id}")
    else:
        conv.updated_at = _now()
        conv.status = "ACTIVE"
        db.add(conv)
        logger.info(f"[WHATSAPP SEND] Updated existing conversation {conv.id} for lead {lead.id}")

    # 4. Message creation & persistence
    time_now = _now().strftime("%I:%M %p")
    iso_now = _now().isoformat()
    msg = Message(
        id=str(uuid.uuid4()),
        conversation_id=conv.id,
        sender_type="agent",
        sender_id=user.id,
        content=payload.content,
        meta={
            "time": time_now,
            "timestamp": iso_now,
            "recipient_phone": lead.phone,
            "status": "sent",
        },
        timestamp=_now(),
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    logger.info(f"[WHATSAPP SEND] Stored message {msg.id} in conversation {conv.id}")

    # 5. Send via WhatsApp Meta Cloud API (keep existing working integration logic intact)
    stmt_wa = select(Integration).where(
        Integration.business_id == user.business_id,
        Integration.provider == f"whatsapp_{user.id}",
        Integration.is_active == True
    )
    wa_integration = await db.scalar(stmt_wa)
    if not wa_integration:
        stmt_wa_any = select(Integration).where(
            Integration.business_id == user.business_id,
            Integration.provider.startswith("whatsapp"),
            Integration.is_active == True
        )
        wa_integration = await db.scalar(stmt_wa_any)
    
    wa_token = None
    wa_phone_id = None
    if wa_integration and isinstance(wa_integration.config, dict):
        wa_token = wa_integration.config.get("access_token")
        wa_phone_id = wa_integration.config.get("phone_number_id")

    active_token = (wa_token or settings.whatsapp_token or "").strip()
    if active_token.startswith("WHATSAPP_TOKEN="):
        active_token = active_token[len("WHATSAPP_TOKEN="):].strip()
    active_token = active_token.strip("\"'")

    active_phone_id = (wa_phone_id or settings.whatsapp_phone_number_id or "").strip()

    if lead.phone and active_token and active_phone_id:
        try:
            logger.info(f"[WHATSAPP SEND] Invoking Meta Cloud API to {lead.phone} via phone_id {active_phone_id}")
            res = await send_whatsapp_message(
                to=lead.phone,
                text=payload.content,
                token=active_token,
                phone_number_id=active_phone_id
            )
            if res.get("status") == "error":
                logger.error(f"[WHATSAPP SEND] Meta Cloud API rejected delivery to {lead.phone}: {res.get('error')}")
            else:
                logger.info(f"[WHATSAPP SEND] Meta Cloud API accepted delivery to {lead.phone}: {res}")
        except Exception as e:
            logger.error(f"[WHATSAPP SEND] API send failed: {e}")

    # 6. Return enriched API response (Requirement 8)
    return WhatsAppMessageOut(
        id=msg.id,
        from_type="agent",
        text=msg.content,
        time=time_now,
        conversation_id=conv.id,
        contact_id=lead.id,
        lead_id=lead.id,
        recipient_phone=lead.phone,
        phone=lead.phone,
        status="sent",
        latest_message=msg.content,
        timestamp=iso_now,
    )

# ---------------------------------------------------------------------------
# Gmail Panel endpoints (Emails)
# ---------------------------------------------------------------------------
@router.get("/emails/{lead_id}", response_model=list[EmailThreadOut])
async def list_lead_emails(
    lead_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    lead = await db.get(Lead, lead_id)
    if not lead or lead.business_id != user.business_id:
        if not lead:
            try:
                lead = Lead(
                    id=lead_id if len(lead_id) == 36 and "-" in lead_id else str(uuid.uuid4()),
                    business_id=user.business_id,
                    first_name="Lead",
                    last_name="",
                    phone="",
                    email="",
                    status="NEW",
                    source="EMAIL",
                )
                db.add(lead)
                await db.flush()
            except Exception:
                await db.rollback()
                return []
        else:
            return []

    status_info = await gmail_service.get_gmail_status(db, user.id, user.business_id)
    if not status_info["connected"]:
        # Return empty list if integration is not active yet (frontend handles rendering connect button)
        return []

    # Fetch email threads from database for this lead
    stmt = select(Conversation).where(
        Conversation.lead_id == lead_id,
        Conversation.channel == "email",
        Conversation.business_id == user.business_id,
    )
    conv = await db.scalar(stmt)
    if not conv:
        return []

    msg_stmt = (
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.timestamp.desc())
    )
    msg_res = await db.execute(msg_stmt)
    db_emails = msg_res.scalars().all()

    threads = []
    for e in db_emails:
        meta = e.meta or {}
        replies = [
            EmailReplyOut(
                id=str(r.get("id", idx)),
                from_name=r.get("from_name", "You"),
                time=r.get("time", "Just now"),
                body=r.get("body", ""),
            )
            for idx, r in enumerate(meta.get("replies", []))
        ]
        threads.append(
            EmailThreadOut(
                id=e.id,
                leadId=lead_id,
                subject=meta.get("subject", "No Subject"),
                from_name=meta.get("from_name", "You"),
                fromEmail=meta.get("from_email", ""),
                time=meta.get("time") or e.timestamp.strftime("%b %d, %I:%M %p"),
                unread=meta.get("unread", False),
                body=e.content,
                replies=replies,
                success=True,
                message="Email retrieved successfully",
                message_id=meta.get("gmail_message_id"),
            )
        )
    return threads


@router.post("/emails/{lead_id}", response_model=EmailThreadOut)
async def compose_email(
    lead_id: str,
    payload: SendEmailRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Send an email via the user's authenticated Gmail account using Google Gmail API.
    Records the sent message into the database Conversation and EmailLog.
    """
    lead = None
    if lead_id and lead_id not in ("new", "direct", "none", "undefined"):
        lead = await db.get(Lead, lead_id)
        if lead and lead.business_id != user.business_id:
            lead = None

    if not lead:
        stmt = select(Lead).where(
            Lead.email == payload.to_email,
            Lead.business_id == user.business_id,
        )
        lead = await db.scalar(stmt)

    if not lead:
        lead = Lead(
            business_id=user.business_id,
            first_name=payload.to_email.split("@")[0].capitalize(),
            email=payload.to_email,
            status="NEW",
            source="EMAIL",
        )
        db.add(lead)
        await db.flush()

    # Dispatch via custom configured email provider (SMTP / Resend / SendGrid) or Gmail API
    custom_email_integ = await db.scalar(
        select(Integration).where(
            Integration.business_id == user.business_id,
            Integration.provider.in_(["email_config", "smtp", "sendgrid", "resend"]),
            Integration.is_active == True,
        )
    )

    thread_id = None
    provider_name = "gmail"

    # Resolve attachments preserving exact original format
    resolved_attachments: list[dict] = []
    if payload.attachments:
        for att in payload.attachments:
            file_bytes = None
            fname = att.filename
            mtype = att.content_type
            if att.file_id:
                tfile = await db.get(TenantFile, att.file_id)
                if tfile and os.path.exists(tfile.file_path):
                    with open(tfile.file_path, "rb") as f:
                        file_bytes = f.read()
                    fname = tfile.original_filename or fname
                    mtype = tfile.mime_type or mtype
            if not file_bytes and att.content_base64:
                import base64
                try:
                    file_bytes = base64.b64decode(att.content_base64)
                except Exception:
                    file_bytes = att.content_base64.encode("utf-8")
            if file_bytes:
                resolved_attachments.append({
                    "filename": fname,
                    "content": file_bytes,
                    "content_type": mtype,
                })

    if not resolved_attachments and "Attached:" in payload.body:
        import re
        matches = re.findall(r"Attached:\s*([^\r\n]+)", payload.body)
        for match in matches:
            clean_name = match.strip().split("(")[0].strip().replace("📎", "").strip()
            stmt_f = select(TenantFile).where(
                TenantFile.tenant_id == user.business_id,
                TenantFile.original_filename == clean_name
            ).order_by(TenantFile.created_at.desc())
            tfile = await db.scalar(stmt_f)
            if tfile and os.path.exists(tfile.file_path):
                with open(tfile.file_path, "rb") as f:
                    file_bytes = f.read()
                resolved_attachments.append({
                    "filename": tfile.original_filename,
                    "content": file_bytes,
                    "content_type": tfile.mime_type,
                })

    if custom_email_integ:
        provider_name = custom_email_integ.provider or "smtp"
        send_res = await send_email(
            to_email=payload.to_email,
            subject=payload.subject,
            html_content=payload.body,
            business_id=user.business_id,
            db=db,
            attachments=resolved_attachments if resolved_attachments else None,
        )
        if not send_res.get("success"):
            raise HTTPException(status_code=400, detail=send_res.get("error") or "Failed to send email via configured email provider")
        message_id = send_res.get("id") or "email_sent"
        cfg = custom_email_integ.config if isinstance(custom_email_integ.config, dict) else {}
        sender_email = cfg.get("email_address") or cfg.get("resend_from_email") or user.email
    else:
        # Fallback to Gmail OAuth
        result = await gmail_service.send_gmail_message(
            db=db,
            user_id=user.id,
            business_id=user.business_id,
            to_email=payload.to_email,
            subject=payload.subject,
            body=payload.body,
        )

        if not result.get("success"):
            # Try default email service fallback
            fallback_res = await send_email(
                to_email=payload.to_email,
                subject=payload.subject,
                html_content=payload.body,
                business_id=user.business_id,
                db=db,
                attachments=resolved_attachments if resolved_attachments else None,
            )
            if not fallback_res.get("success"):
                status_code = result.get("status_code", 400)
                if status_code == 401:
                    status_code = 400
                error_msg = result.get("error", "Failed to send email")
                raise HTTPException(status_code=status_code, detail=error_msg)
            message_id = fallback_res.get("id") or "email_sent"
            sender_email = user.email
        else:
            message_id = result.get("message_id")
            thread_id = result.get("thread_id")
            sender_email = result.get("sender") or user.email

    # Log in EmailLog
    email_log = EmailLog(
        business_id=user.business_id,
        to_email=payload.to_email,
        subject=payload.subject,
        status="sent",
        resend_id=f"{provider_name}_{message_id}",
    )
    db.add(email_log)

    # Find or create Conversation
    stmt_conv = select(Conversation).where(
        Conversation.lead_id == lead.id,
        Conversation.channel == "email",
        Conversation.business_id == user.business_id,
    )
    conv = await db.scalar(stmt_conv)
    if not conv:
        conv = Conversation(
            business_id=user.business_id,
            lead_id=lead.id,
            channel="email",
            status="ACTIVE",
        )
        db.add(conv)
        await db.flush()

    time_now = _now().strftime("%b %d, %I:%M %p")
    msg = Message(
        conversation_id=conv.id,
        sender_type="agent",
        sender_id=user.id,
        message_type="TEXT",
        content=payload.body,
        meta={
            "subject": payload.subject,
            "from_name": user.full_name or "You",
            "from_email": sender_email,
            "to_email": payload.to_email,
            "gmail_message_id": message_id,
            "thread_id": thread_id,
            "provider": provider_name,
            "status": "sent",
            "time": time_now,
            "unread": False,
            "replies": [],
            "attachments": [
                {
                    "filename": a["filename"],
                    "content_type": a.get("content_type"),
                }
                for a in resolved_attachments
            ] if resolved_attachments else [],
        },
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    return EmailThreadOut(
        id=msg.id,
        leadId=lead.id,
        subject=payload.subject,
        from_name=user.full_name or "You",
        fromEmail=sender_email,
        time=time_now,
        unread=False,
        body=payload.body,
        replies=[],
        success=True,
        message="Email sent successfully",
        message_id=message_id,
    )


@router.post("/emails/{lead_id}/{msg_id}/reply")
async def reply_email(
    lead_id: str,
    msg_id: str,
    payload: ReplyEmailRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    lead = None
    if lead_id and lead_id not in ("new", "direct", "none", "undefined"):
        lead = await db.get(Lead, lead_id)
        if lead and lead.business_id != user.business_id:
            lead = None

    msg = await db.get(Message, msg_id)
    meta = dict(msg.meta or {}) if msg else {}
    if not lead and msg and msg.conversation_id:
        conv = await db.get(Conversation, msg.conversation_id)
        if conv and conv.lead_id:
            lead = await db.get(Lead, conv.lead_id)

    recipient_email = (lead.email if lead else None) or meta.get("from_email") or meta.get("to_email")
    if not recipient_email:
        raise HTTPException(status_code=400, detail="No recipient email found for this lead")
    subject = meta.get("subject", "Re: Conversation")
    gmail_msg_id = meta.get("gmail_message_id")
    thread_id = meta.get("thread_id")

    # Check custom email integration
    custom_email_integ = await db.scalar(
        select(Integration).where(
            Integration.business_id == user.business_id,
            Integration.provider.in_(["email_config", "smtp", "sendgrid", "resend"]),
            Integration.is_active == True,
        )
    )

    if custom_email_integ:
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        send_res = await send_email(
            to_email=recipient_email,
            subject=reply_subject,
            html_content=payload.body,
            business_id=user.business_id,
            db=db,
        )
        if not send_res.get("success"):
            raise HTTPException(
                status_code=400,
                detail=send_res.get("error") or "Failed to send email reply via configured email provider",
            )
    else:
        # Send threaded reply via Gmail API
        result = await gmail_service.send_gmail_message(
            db=db,
            user_id=user.id,
            business_id=user.business_id,
            to_email=recipient_email,
            subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
            body=payload.body,
            reply_to_message_id=gmail_msg_id,
            thread_id=thread_id,
        )

        if not result.get("success"):
            # Fallback to configured SMTP/email service
            fallback_res = await send_email(
                to_email=recipient_email,
                subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
                html_content=payload.body,
                business_id=user.business_id,
                db=db,
            )
            if not fallback_res.get("success"):
                status_code = 400
                error_msg = fallback_res.get("error") or result.get("error", "Failed to send reply via Gmail API")
                raise HTTPException(
                    status_code=status_code,
                    detail=error_msg,
                )

    # Append reply to message metadata in DB
    time_now = _now().strftime("%I:%M %p")
    new_reply = {
        "id": str(uuid.uuid4()),
        "from_name": user.full_name or "You",
        "time": time_now,
        "body": payload.body,
    }
    if msg:
        replies = list(meta.get("replies", []))
        replies.append(new_reply)
        meta["replies"] = replies
        msg.meta = meta
        db.add(msg)
        await db.commit()

    return {"success": True}

