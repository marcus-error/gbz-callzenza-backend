"""Resend email service for CallZenza.
Provides email sending, bulk delivery, live connectivity verification,
and automatic transactional logging in the EmailLog table.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import os
from typing import Any, Optional

from cryptography.fernet import Fernet
from loguru import logger
try:
    import resend
except ImportError:
    resend = None
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.database import AsyncSessionLocal
from app.db.models import EmailLog, Integration

settings = get_settings()


import asyncio
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
import httpx


def _get_cipher(secret_override: Optional[str] = None) -> Fernet:
    """Derive 32-byte Fernet key from secret_key."""
    secret = secret_override or settings.secret_key or "callzenza-default-secret-key-32b"
    derived = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_api_key(api_key: str) -> str:
    """Encrypts an API key or password for safe database storage."""
    if not api_key:
        return ""
    cipher = _get_cipher()
    return cipher.encrypt(api_key.encode("utf-8")).decode("utf-8")


def decrypt_api_key(token: str) -> str:
    """Decrypts a stored API key or password, testing known secret keys with safe fallback."""
    if not token:
        return ""
    if not token.startswith("gAAAAA"):
        return token

    candidate_keys = [
        settings.secret_key,
        os.getenv("SECRET_KEY"),
        "change-me-super-secret",
        "callzenza-default-secret-key-32b",
    ]
    seen = set()
    for key in candidate_keys:
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            cipher = _get_cipher(key)
            decrypted = cipher.decrypt(token.encode("utf-8")).decode("utf-8")
            if decrypted:
                return decrypted
        except Exception:
            continue

    logger.warning("[EMAIL SERVICE] Could not decrypt stored credential token with any known key. Returning empty for env fallback.")
    return ""


def check_smtp_connectivity(
    host: str = "smtp.gmail.com",
    port: int = 587,
    email_address: str = "",
    password: str = "",
    encryption: str = "TLS",
) -> dict:
    """Live SMTP verification with auto-fallback between TLS (587) and SSL (465)."""
    import socket
    import ssl

    if not email_address:
        email_address = os.getenv("SMTP_USER") or os.getenv("SMTP_EMAIL", "")
    if not password:
        password = os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD", "")

    if not host or not email_address or not password:
        return {
            "connected": False,
            "provider": "smtp",
            "error": "SMTP Host, Email Address, and Password are required."
        }

    port_int = int(port or 587)
    enc = (encryption or "TLS").upper()
    clean_pwd = password.strip().strip("\"'").replace(" ", "") if "gmail" in host.lower() else password.strip().strip("\"'")
    if not clean_pwd or clean_pwd.startswith("gAAAAA"):
        env_pwd = os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD", "")
        clean_pwd = env_pwd.strip().strip("\"'").replace(" ", "") if "gmail" in host.lower() else env_pwd.strip().strip("\"'")

    strategies = []
    if enc == "SSL" or port_int == 465:
        strategies.append((port_int or 465, True))
        strategies.append((587, False))
    else:
        strategies.append((port_int or 587, False))
        strategies.append((465, True))

    last_error = None
    ssl_context = ssl.create_default_context()

    for p, use_ssl in strategies:
        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(host, p, timeout=12, context=ssl_context)
                server.login(email_address, clean_pwd)
                server.quit()
            else:
                server = smtplib.SMTP(host, p, timeout=12)
                server.ehlo()
                server.starttls(context=ssl_context)
                server.ehlo()
                server.login(email_address, clean_pwd)
                server.quit()

            return {
                "connected": True,
                "provider": "smtp",
                "host": host,
                "port": p,
                "email": email_address,
            }
        except smtplib.SMTPAuthenticationError as e:
            err_detail = e.smtp_error.decode("utf-8", errors="ignore") if hasattr(e, "smtp_error") and isinstance(e.smtp_error, bytes) else str(e)
            logger.warning(f"[SMTP AUTH FAILED] {err_detail}")
            return {
                "connected": False,
                "provider": "smtp",
                "error": f"SMTP Authentication failed: {err_detail}. (Note: For Gmail, use an App Password generated from myaccount.google.com/apppasswords without spaces)."
            }
        except Exception as e:
            last_error = e
            logger.warning(f"[SMTP VERIFY ATTEMPT FAILED] {host}:{p} (SSL={use_ssl}): {e}. Trying alternate port...")
            continue

    err_str = str(last_error) if last_error else "Connection failed"
    return {
        "connected": False,
        "provider": "smtp",
        "error": f"SMTP Connection failed to {host} on ports 587 and 465: {err_str}. Hosting platforms often block port 587 or require SSL 465."
    }


async def check_sendgrid_connectivity(api_key: str, from_email: Optional[str] = None) -> dict:
    """Live SendGrid verification: authenticates against SendGrid API."""
    key = (api_key or "").strip()
    if not key or "xxxxxxxx" in key:
        return {"connected": False, "provider": "sendgrid", "error": "No valid SendGrid API Key provided."}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(
                "https://api.sendgrid.com/v3/scopes",
                headers={"Authorization": f"Bearer {key}"}
            )
            if res.status_code == 200:
                return {
                    "connected": True,
                    "provider": "sendgrid",
                    "from_email": from_email or "verified_sender@domain.com",
                }
            else:
                err_text = res.text
                try:
                    err_json = res.json()
                    if isinstance(err_json.get("errors"), list) and len(err_json["errors"]) > 0:
                        err_text = err_json["errors"][0].get("message", res.text)
                except Exception:
                    pass
                return {
                    "connected": False,
                    "provider": "sendgrid",
                    "error": f"SendGrid verification failed ({res.status_code}): {err_text}"
                }
    except Exception as e:
        return {"connected": False, "provider": "sendgrid", "error": f"SendGrid API connection failed: {str(e)}"}


def check_resend_connectivity(api_key: Optional[str] = None, from_email: Optional[str] = None) -> dict:
    """Live check — confirms the API key actually authenticates against Resend.
    Calls resend.Domains.list() to verify live credentials against Resend's API.
    Also checks whether the sending domain is verified.
    """
    key = api_key or os.getenv("RESEND_API_KEY") or settings.resend_api_key
    sender = from_email or os.getenv("RESEND_FROM_EMAIL") or settings.resend_from_email or "noreply@callzenza.com"
    sender_domain = sender.split("@")[-1].strip().lower() if "@" in sender else ""

    if not key or "xxxxxxxx" in key:
        return {
            "connected": False,
            "provider": "resend",
            "from_email": sender,
            "error": "No valid RESEND_API_KEY configured. Please provide a valid key starting with 're_'."
        }

    if not resend:
        return False, "resend library not installed"
    try:
        resend.api_key = key
        domains_resp = resend.Domains.list()
        raw_domains = (
            domains_resp.get("data", [])
            if isinstance(domains_resp, dict)
            else (getattr(domains_resp, "data", []) or [])
        )
        
        # Check domain verification status
        domain_status = "not_found"
        verified_domains = []
        for d in raw_domains:
            name = d.get("name", "") if isinstance(d, dict) else getattr(d, "name", "")
            status = d.get("status", "") if isinstance(d, dict) else getattr(d, "status", "")
            if status == "verified":
                verified_domains.append(name)
            if name.lower() == sender_domain:
                domain_status = status

        if sender_domain == "resend.dev":
            domain_status = "default_test_domain"

        return {
            "connected": True,
            "provider": "resend",
            "from_email": sender,
            "sender_domain": sender_domain,
            "domain_status": domain_status,
            "verified_domains": verified_domains,
            "total_domains": len(raw_domains),
        }
    except Exception as e:
        err_msg = str(e)
        logger.error(f"[RESEND CONNECTIVITY CHECK FAILED] {err_msg}")
        return {
            "connected": False,
            "provider": "resend",
            "from_email": sender,
            "error": err_msg
        }


def _send_smtp_sync(
    host: str,
    port: int,
    email_address: str,
    password: str,
    encryption: str,
    sender_name: str,
    to_email: str,
    subject: str,
    html_content: str,
    reply_to: Optional[str] = None,
    attachments: Optional[list[dict]] = None,
) -> dict:
    """Synchronous SMTP email delivery with multi-format attachments and auto-fallback between 587 (TLS) and 465 (SSL)."""
    import mimetypes
    import ssl
    from email.mime.base import MIMEBase
    from email import encoders

    if not email_address:
        email_address = os.getenv("SMTP_USER") or os.getenv("SMTP_EMAIL", "")
    if not password:
        password = os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD", "")

    port_int = int(port or 587)
    enc = (encryption or "TLS").upper()

    clean_pwd = password.strip().strip("\"'").replace(" ", "") if "gmail" in host.lower() else password.strip().strip("\"'")
    if not clean_pwd or clean_pwd.startswith("gAAAAA"):
        env_pwd = os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD", "")
        clean_pwd = env_pwd.strip().strip("\"'").replace(" ", "") if "gmail" in host.lower() else env_pwd.strip().strip("\"'")

    if not email_address or not clean_pwd:
        return {"success": False, "error": "SMTP email address or password not configured."}

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name or "AI Receptionist", email_address))
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    sender_domain = email_address.split("@")[-1].strip() if "@" in email_address else None
    msg["Message-ID"] = make_msgid(domain=sender_domain)
    if reply_to:
        msg["Reply-To"] = reply_to

    # Body part (rendered as HTML)
    part = MIMEText(html_content, "html")
    msg.attach(part)

    # Attach any files preserving exact binary content, extension, and filename
    if attachments:
        for att in attachments:
            fname = att.get("filename") or "attachment"
            content = att.get("content")
            ctype = att.get("content_type") or mimetypes.guess_type(fname)[0] or "application/octet-stream"

            if isinstance(content, str):
                import base64
                try:
                    content = base64.b64decode(content)
                except Exception:
                    content = content.encode("utf-8")

            if content:
                maintype, subtype = ctype.split("/", 1) if "/" in ctype else ("application", "octet-stream")
                part_att = MIMEBase(maintype, subtype)
                part_att.set_payload(content)
                encoders.encode_base64(part_att)
                part_att.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{fname}"'
                )
                msg.attach(part_att)

    # Prepare connection strategies: primary port/protocol first, then alternate
    strategies = []
    if enc == "SSL" or port_int == 465:
        strategies.append((port_int or 465, True))
        strategies.append((587, False))
    else:
        strategies.append((port_int or 587, False))
        strategies.append((465, True))

    last_err = None
    ssl_context = ssl.create_default_context()

    for p, use_ssl in strategies:
        try:
            logger.info(f"[SMTP SEND] Attempting delivery to {to_email} via {host}:{p} (SSL={use_ssl})...")
            if use_ssl:
                server = smtplib.SMTP_SSL(host, p, timeout=15, context=ssl_context)
                server.login(email_address, clean_pwd)
                server.sendmail(email_address, [to_email], msg.as_string())
                server.quit()
            else:
                server = smtplib.SMTP(host, p, timeout=15)
                server.ehlo()
                server.starttls(context=ssl_context)
                server.ehlo()
                server.login(email_address, clean_pwd)
                server.sendmail(email_address, [to_email], msg.as_string())
                server.quit()

            now_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            logger.info(f"[SMTP SEND SUCCESS] Successfully delivered email to {to_email} via {host}:{p}")
            return {"success": True, "id": f"smtp_{now_str}", "port_used": p}
        except smtplib.SMTPAuthenticationError as auth_err:
            err_detail = auth_err.smtp_error.decode("utf-8", errors="ignore") if hasattr(auth_err, "smtp_error") and isinstance(auth_err.smtp_error, bytes) else str(auth_err)
            logger.error(f"[SMTP AUTH ERROR] {err_detail}")
            return {
                "success": False,
                "error": f"Gmail SMTP authentication failed: {err_detail}. Please verify Gmail App Password in .env or Integrations."
            }
        except Exception as e:
            last_err = e
            logger.warning(f"[SMTP ATTEMPT FAILED] {host}:{p} (SSL={use_ssl}): {e}. Retrying on alternate port/protocol...")
            continue

    err_str = str(last_err) if last_err else "SMTP delivery failed"
    logger.error(f"[SMTP SEND FAILED ALL PORTS] Failed delivering to {to_email}: {err_str}")
    return {"success": False, "error": f"SMTP send error (tried ports 587 and 465): {err_str}"}


async def send_smtp_email(
    host: str,
    port: int,
    email_address: str,
    password: str,
    encryption: str,
    sender_name: str,
    to_email: str,
    subject: str,
    html_content: str,
    reply_to: Optional[str] = None,
    attachments: Optional[list[dict]] = None,
) -> dict:
    """Async wrapper around SMTP sending using asyncio.to_thread."""
    try:
        return await asyncio.to_thread(
            _send_smtp_sync,
            host=host,
            port=port,
            email_address=email_address,
            password=password,
            encryption=encryption,
            sender_name=sender_name,
            to_email=to_email,
            subject=subject,
            html_content=html_content,
            reply_to=reply_to,
            attachments=attachments,
        )
    except Exception as e:
        logger.error(f"[SMTP SEND FAILED] {e}")
        return {"success": False, "error": f"SMTP send error: {str(e)}"}


async def send_sendgrid_email(
    api_key: str,
    from_email: str,
    sender_name: str,
    to_email: str,
    subject: str,
    html_content: str,
    reply_to: Optional[str] = None,
) -> dict:
    """Async email sending via SendGrid v3 Mail API."""
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": sender_name or "AI Receptionist"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_content}],
    }
    if reply_to:
        payload["reply_to"] = {"email": reply_to}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if res.status_code in (200, 201, 202):
                msg_id = res.headers.get("X-Message-Id", f"sg_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
                return {"success": True, "id": msg_id}
            else:
                err_text = res.text
                try:
                    err_json = res.json()
                    if isinstance(err_json.get("errors"), list) and len(err_json["errors"]) > 0:
                        err_text = "; ".join(e.get("message", "") for e in err_json["errors"])
                except Exception:
                    pass
                return {"success": False, "error": f"SendGrid error ({res.status_code}): {err_text}"}
    except Exception as e:
        logger.error(f"[SENDGRID SEND FAILED] {e}")
        return {"success": False, "error": f"SendGrid send error: {str(e)}"}


async def resolve_email_config(
    business_id: Any = None,
    db: Optional[AsyncSession] = None
) -> dict[str, Any]:
    """Resolves full email configuration dictionary across providers (SMTP, Resend, SendGrid)."""
    if isinstance(business_id, AsyncSession) and db is None:
        db = business_id
        business_id = None

    smtp_pwd = os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD", "")
    smtp_sec = os.getenv("SMTP_SECURE", "false").lower()
    default_enc = "SSL" if smtp_sec in ("true", "1", "yes") or os.getenv("SMTP_PORT") == "465" else os.getenv("SMTP_ENCRYPTION", "TLS")

    default_config = {
        "provider": "smtp",
        "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "email_address": os.getenv("SMTP_USER") or os.getenv("SMTP_EMAIL", ""),
        "password": smtp_pwd,
        "sender_name": os.getenv("EMAIL_SENDER_NAME", "AI Receptionist"),
        "encryption": default_enc,
        "template": os.getenv("EMAIL_APPOINTMENT_TEMPLATE", ""),
        "resend_api_key": os.getenv("RESEND_API_KEY", settings.resend_api_key or ""),
        "resend_from_email": os.getenv("RESEND_FROM_EMAIL", settings.resend_from_email or "noreply@callzenza.com"),
        "sendgrid_api_key": os.getenv("SENDGRID_API_KEY", ""),
        "sendgrid_from_email": os.getenv("SENDGRID_FROM_EMAIL", ""),
    }

    if business_id and db:
        try:
            # Check unified email_config first
            stmt = select(Integration).where(
                Integration.business_id == business_id,
                Integration.provider.in_(["email_config", "resend", "smtp", "sendgrid"]),
                Integration.is_active == True
            ).order_by(Integration.created_at.desc())
            res = (await db.execute(stmt)).scalars().first()
            if res and isinstance(res.config, dict):
                cfg = res.config
                if res.provider == "resend" and "provider" not in cfg:
                    cfg["provider"] = "resend"
                
                for k, v in cfg.items():
                    if k in ("password", "resend_api_key", "sendgrid_api_key", "api_key") and v:
                        default_config[k] = decrypt_api_key(v)
                    elif v is not None:
                        default_config[k] = v

                # Handle resend backward compatibility
                if "api_key" in cfg and not default_config.get("resend_api_key"):
                    default_config["resend_api_key"] = decrypt_api_key(cfg["api_key"])
                if "from_email" in cfg and not default_config.get("resend_from_email"):
                    default_config["resend_from_email"] = cfg["from_email"]
                if "from_name" in cfg and not default_config.get("sender_name"):
                    default_config["sender_name"] = cfg["from_name"]
        except Exception as e:
            logger.warning(f"[EMAIL SERVICE] Error resolving tenant email config: {e}")

    if not default_config.get("password") or default_config.get("password", "").startswith("gAAAAA"):
        default_config["password"] = os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD", "")
    if not default_config.get("email_address"):
        default_config["email_address"] = os.getenv("SMTP_USER") or os.getenv("SMTP_EMAIL", "")

    return default_config


async def resolve_resend_config(
    business_id: Optional[str] = None,
    db: Optional[AsyncSession] = None
) -> tuple[str, str, str]:
    """Backward-compatible helper for Resend config."""
    cfg = await resolve_email_config(business_id, db)
    return cfg.get("resend_api_key", ""), cfg.get("resend_from_email", "noreply@callzenza.com"), cfg.get("sender_name", "CallZenza")


async def log_email_record(
    to_email: str,
    subject: str,
    status: str,
    resend_id: Optional[str] = None,
    error: Optional[str] = None,
    business_id: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> Optional[EmailLog]:
    """Logs an email transaction into the email_logs table."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    async def _insert(session: AsyncSession):
        record = EmailLog(
            business_id=business_id,
            to_email=to_email,
            subject=subject,
            status=status,
            resend_id=resend_id,
            error=error,
            sent_at=now,
            created_at=now,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record

    try:
        if db:
            return await _insert(db)
        else:
            async with AsyncSessionLocal() as session:
                return await _insert(session)
    except Exception as e:
        logger.error(f"[EMAIL LOG ERROR] Failed to record email log: {e}")
        return None


async def test_email_connection(
    provider: str,
    config: dict[str, Any]
) -> dict[str, Any]:
    """Tests live connection for the specified provider (smtp, resend, sendgrid)."""
    p = (provider or "smtp").lower().strip()
    if p == "smtp":
        return check_smtp_connectivity(
            host=config.get("smtp_host", "smtp.gmail.com"),
            port=config.get("smtp_port", 587),
            email_address=config.get("email_address", ""),
            password=config.get("password", ""),
            encryption=config.get("encryption", "TLS"),
        )
    elif p == "sendgrid":
        return await check_sendgrid_connectivity(
            api_key=config.get("sendgrid_api_key", ""),
            from_email=config.get("sendgrid_from_email"),
        )
    elif p == "resend":
        return check_resend_connectivity(
            api_key=config.get("resend_api_key"),
            from_email=config.get("resend_from_email"),
        )
    else:
        return {"connected": False, "error": f"Unknown email provider: {provider}"}


async def send_email(
    to_email: str,
    subject: str,
    html_content: str,
    cc: Optional[list[str]] = None,
    business_id: Optional[str] = None,
    db: Optional[AsyncSession] = None,
    metadata: Optional[dict] = None,
    reply_to: Optional[str] = None,
    provider_override: Optional[str] = None,
    config_override: Optional[dict[str, Any]] = None,
    attachments: Optional[list[dict]] = None,
) -> dict:
    """Send an email using configured provider (SMTP, SendGrid, or Resend) and log in EmailLog."""
    resolved_config = await resolve_email_config(business_id, db)
    if config_override:
        resolved_config.update(config_override)

    active_provider = (provider_override or resolved_config.get("provider") or "smtp").lower().strip()
    sender_name = resolved_config.get("sender_name") or "AI Receptionist"

    try:
        if active_provider == "smtp":
            host = resolved_config.get("smtp_host") or "smtp.gmail.com"
            port = int(resolved_config.get("smtp_port") or 587)
            email_addr = resolved_config.get("email_address") or ""
            pwd = resolved_config.get("password") or ""
            enc = resolved_config.get("encryption") or "TLS"

            if not host or not email_addr or not pwd:
                err_msg = "SMTP is not fully configured (host, email, and password required)."
                logger.warning(f"[EMAIL SEND SKIPPED] {err_msg}")
                await log_email_record(to_email=to_email, subject=subject, status="failed", error=err_msg, business_id=business_id, db=db)
                return {"success": False, "error": err_msg}

            result = await send_smtp_email(
                host=host,
                port=port,
                email_address=email_addr,
                password=pwd,
                encryption=enc,
                sender_name=sender_name,
                to_email=to_email,
                subject=subject,
                html_content=html_content,
                reply_to=reply_to,
                attachments=attachments,
            )
            status = "sent" if result.get("success") else "failed"
            await log_email_record(to_email=to_email, subject=subject, status=status, resend_id=result.get("id"), error=result.get("error"), business_id=business_id, db=db)
            return result

        elif active_provider == "sendgrid":
            sg_key = resolved_config.get("sendgrid_api_key") or ""
            from_email = resolved_config.get("sendgrid_from_email") or resolved_config.get("email_address") or "noreply@callzenza.com"

            if not sg_key or "xxxxxxxx" in sg_key:
                err_msg = "SendGrid API Key is not configured."
                logger.warning(f"[EMAIL SEND SKIPPED] {err_msg}")
                await log_email_record(to_email=to_email, subject=subject, status="failed", error=err_msg, business_id=business_id, db=db)
                return {"success": False, "error": err_msg}

            result = await send_sendgrid_email(
                api_key=sg_key,
                from_email=from_email,
                sender_name=sender_name,
                to_email=to_email,
                subject=subject,
                html_content=html_content,
                reply_to=reply_to,
            )
            status = "sent" if result.get("success") else "failed"
            await log_email_record(to_email=to_email, subject=subject, status=status, resend_id=result.get("id"), error=result.get("error"), business_id=business_id, db=db)
            return result

        else:
            # Resend provider
            api_key = resolved_config.get("resend_api_key") or os.getenv("RESEND_API_KEY") or settings.resend_api_key or ""
            from_email = resolved_config.get("resend_from_email") or os.getenv("RESEND_FROM_EMAIL") or settings.resend_from_email or "noreply@callzenza.com"

            if not api_key or "xxxxxxxx" in api_key:
                err_msg = "Resend API key is not configured or is a placeholder."
                logger.warning(f"[EMAIL SEND SKIPPED] {err_msg}")
                await log_email_record(to_email=to_email, subject=subject, status="failed", error=err_msg, business_id=business_id, db=db)
                return {"success": False, "error": err_msg}

            resend.api_key = api_key
            payload = {
                "from": f"{sender_name} <{from_email}>",
                "to": [to_email],
                "subject": subject,
                "html": html_content,
            }
            if cc:
                payload["cc"] = cc
            if reply_to:
                payload["reply_to"] = reply_to

            response = resend.Emails.send(payload)
            resend_id = response.get("id") if isinstance(response, dict) else getattr(response, "id", None)
            logger.info(f"[EMAIL SENT] Successfully sent email to {to_email} (Resend ID: {resend_id})")
            await log_email_record(to_email=to_email, subject=subject, status="sent", resend_id=str(resend_id) if resend_id else None, business_id=business_id, db=db)
            return {"success": True, "id": resend_id}

    except Exception as e:
        err_str = str(e)
        logger.error(f"[EMAIL SEND FAILED] Error sending to {to_email} via {active_provider}: {err_str}")
        await log_email_record(to_email=to_email, subject=subject, status="failed", error=err_str, business_id=business_id, db=db)
        return {"success": False, "error": err_str}


async def send_bulk_email(
    recipients: list[str],
    subject: str,
    html_content: str,
    business_id: Optional[str] = None,
    db: Optional[AsyncSession] = None,
) -> list[dict]:
    """Send bulk emails individually to a list of recipients and log each one."""
    results = []
    for email in recipients:
        res = await send_email(
            to_email=email,
            subject=subject,
            html_content=html_content,
            business_id=business_id,
            db=db,
        )
        results.append(res)
    return results


class EmailService:
    send_email = staticmethod(send_email)
    send_bulk_email = staticmethod(send_bulk_email)
    send_smtp_email = staticmethod(send_smtp_email)
    send_sendgrid_email = staticmethod(send_sendgrid_email)
    check_connectivity = staticmethod(check_resend_connectivity)
    check_smtp_connectivity = staticmethod(check_smtp_connectivity)
    check_sendgrid_connectivity = staticmethod(check_sendgrid_connectivity)
    test_connection = staticmethod(test_email_connection)


email_service = EmailService()

