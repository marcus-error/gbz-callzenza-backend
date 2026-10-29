import os
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_business_id, get_current_business_id_optional
from app.core.env_utils import (
    get_provider_defaults_from_env,
    sync_provider_to_env,
    update_env_file,
)
from app.db.database import get_db
from app.db.models import Integration
from app.schemas.integration import IntegrationCreate, IntegrationOut, IntegrationUpdate
from app.services.email_service import (
    check_resend_connectivity,
    check_smtp_connectivity,
    check_sendgrid_connectivity,
    test_email_connection,
    resolve_email_config,
    send_email,
    encrypt_api_key,
    decrypt_api_key,
)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
settings = get_settings()


class ResendConfigRequest(BaseModel):
    api_key: Optional[str] = None
    from_email: str
    from_name: Optional[str] = "CallZenza"


class EmailConfigRequest(BaseModel):
    provider: str = "smtp"  # "smtp", "resend", "sendgrid"
    smtp_host: Optional[str] = "smtp.gmail.com"
    smtp_port: Optional[int] = 587
    email_address: Optional[str] = ""
    password: Optional[str] = None
    sender_name: Optional[str] = "AI Receptionist"
    encryption: Optional[str] = "TLS"
    template: Optional[str] = ""
    resend_api_key: Optional[str] = None
    resend_from_email: Optional[str] = None
    sendgrid_api_key: Optional[str] = None
    sendgrid_from_email: Optional[str] = None


class TestConnectionRequest(BaseModel):
    provider: str = "smtp"
    smtp_host: Optional[str] = "smtp.gmail.com"
    smtp_port: Optional[int] = 587
    email_address: Optional[str] = ""
    password: Optional[str] = None
    encryption: Optional[str] = "TLS"
    resend_api_key: Optional[str] = None
    resend_from_email: Optional[str] = None
    sendgrid_api_key: Optional[str] = None
    sendgrid_from_email: Optional[str] = None


class SendTestEmailRequest(BaseModel):
    to_email: EmailStr
    provider: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    email_address: Optional[str] = None
    password: Optional[str] = None
    sender_name: Optional[str] = None
    encryption: Optional[str] = None
    resend_api_key: Optional[str] = None
    resend_from_email: Optional[str] = None
    sendgrid_api_key: Optional[str] = None
    sendgrid_from_email: Optional[str] = None
    template: Optional[str] = None


@router.get("/twilio/number")
async def get_twilio_number(
    business_id: str = Depends(get_current_business_id),
) -> dict:
    """Returns the Twilio caller-ID number configured in the backend's .env
    (TWILIO_PHONE_NUMBER), so the frontend can auto-fill it instead of
    relying on a hardcoded/manually-typed placeholder."""
    settings = get_settings()
    return {
        "twilio_phone_number": settings.twilio_phone_number,
        "configured": bool(settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_phone_number),
    }


@router.get("", response_model=list[IntegrationOut])
async def list_integrations(
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> list[Integration]:
    result = await db.scalars(select(Integration).where(Integration.business_id == business_id))
    existing_list = list(result.all())
    existing_map = {item.provider: item for item in existing_list}
    defaults = get_provider_defaults_from_env()

    now = datetime.now(timezone.utc)
    for prov, default_cfg in defaults.items():
        if prov in existing_map:
            curr_cfg = dict(existing_map[prov].config or {})
            changed = False
            for k, v in default_cfg.items():
                if not curr_cfg.get(k) and v:
                    curr_cfg[k] = v
                    changed = True
            if changed:
                existing_map[prov].config = curr_cfg
        else:
            virtual = Integration(
                id=str(uuid.uuid4()),
                business_id=business_id,
                provider=prov,
                config=default_cfg,
                is_active=True,
                created_at=now,
            )
            existing_list.append(virtual)

    return existing_list

@router.post("", response_model=IntegrationOut, status_code=status.HTTP_201_CREATED)
async def create_or_update_integration(
    payload: IntegrationCreate,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    # Automatically synchronize incoming credentials to .env file
    sync_provider_to_env(payload.provider, payload.config)

    # Check if provider already exists in DB
    existing = await db.scalar(
        select(Integration).where(
            Integration.business_id == business_id,
            Integration.provider == payload.provider
        )
    )
    if existing:
        existing.config = payload.config
        existing.is_active = payload.is_active
        await db.commit()
        await db.refresh(existing)
        return existing

    integration = Integration(
        business_id=business_id,
        provider=payload.provider,
        config=payload.config,
        is_active=payload.is_active,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration

@router.patch("/{provider}", response_model=IntegrationOut)
async def update_integration(
    provider: str,
    payload: IntegrationUpdate,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Integration:
    integration = await db.scalar(
        select(Integration).where(
            Integration.business_id == business_id,
            Integration.provider == provider
        )
    )
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    data = payload.model_dump(exclude_unset=True)
    if "config" in data and data["config"] is not None:
        integration.config = {**integration.config, **data["config"]}
        sync_provider_to_env(provider, integration.config)
    if "is_active" in data and data["is_active"] is not None:
        integration.is_active = data["is_active"]

    await db.commit()
    await db.refresh(integration)
    return integration


@router.get("/email/config")
async def get_email_config_endpoint(
    business_id: str = Depends(get_current_business_id_optional),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves current email configuration for the business across SMTP, Resend, and SendGrid."""
    cfg = await resolve_email_config(business_id, db)
    
    # Check active status from DB
    existing = await db.scalar(
        select(Integration).where(
            Integration.business_id == business_id,
            Integration.provider.in_(["email_config", "resend", "smtp", "sendgrid"]),
            Integration.is_active == True
        )
    )
    is_connected = bool(existing and existing.is_active) or bool(
        cfg.get("email_address") or (cfg.get("resend_api_key") and "xxxxxxxx" not in cfg.get("resend_api_key", ""))
    )

    return {
        "provider": cfg.get("provider", "smtp"),
        "smtp_host": cfg.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": int(cfg.get("smtp_port", 587)),
        "email_address": cfg.get("email_address", ""),
        "has_password": bool(cfg.get("password")),
        "sender_name": cfg.get("sender_name", "AI Receptionist"),
        "encryption": cfg.get("encryption", "TLS"),
        "template": cfg.get("template", ""),
        "resend_from_email": cfg.get("resend_from_email", "noreply@callzenza.com"),
        "has_resend_api_key": bool(cfg.get("resend_api_key") and "xxxxxxxx" not in cfg.get("resend_api_key", "")),
        "sendgrid_from_email": cfg.get("sendgrid_from_email", ""),
        "has_sendgrid_api_key": bool(cfg.get("sendgrid_api_key")),
        "connected": is_connected,
        "is_active": is_connected,
    }


@router.post("/email/config")
async def save_email_config_endpoint(
    payload: EmailConfigRequest,
    business_id: str = Depends(get_current_business_id_optional),
    db: AsyncSession = Depends(get_db),
):
    """Saves multi-provider email configuration with encryption."""
    provider = (payload.provider or "smtp").lower().strip()
    
    # Load existing to preserve unchanged passwords/keys
    existing = await db.scalar(
        select(Integration).where(
            Integration.business_id == business_id,
            Integration.provider == "email_config"
        )
    )
    old_cfg = (existing.config or {}) if existing else {}

    # SMTP password
    pwd = payload.password
    if not pwd or pwd == "••••••••••••••••••••":
        encrypted_pwd = old_cfg.get("password") or ""
    else:
        encrypted_pwd = encrypt_api_key(pwd)

    # Resend Key
    resend_key = payload.resend_api_key
    if not resend_key or resend_key == "••••••••••••••••••••":
        encrypted_resend = old_cfg.get("resend_api_key") or ""
    else:
        encrypted_resend = encrypt_api_key(resend_key)

    # SendGrid Key
    sg_key = payload.sendgrid_api_key
    if not sg_key or sg_key == "••••••••••••••••••••":
        encrypted_sg = old_cfg.get("sendgrid_api_key") or ""
    else:
        encrypted_sg = encrypt_api_key(sg_key)

    new_cfg = {
        "provider": provider,
        "smtp_host": payload.smtp_host or "smtp.gmail.com",
        "smtp_port": payload.smtp_port or 587,
        "email_address": payload.email_address or "",
        "password": encrypted_pwd,
        "sender_name": payload.sender_name or "AI Receptionist",
        "encryption": payload.encryption or "TLS",
        "template": payload.template or "",
        "resend_api_key": encrypted_resend,
        "resend_from_email": payload.resend_from_email or "",
        "sendgrid_api_key": encrypted_sg,
        "sendgrid_from_email": payload.sendgrid_from_email or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if existing:
        existing.config = new_cfg
        existing.is_active = True
        await db.commit()
        await db.refresh(existing)
    else:
        integration = Integration(
            business_id=business_id,
            provider="email_config",
            config=new_cfg,
            is_active=True,
        )
        db.add(integration)
        await db.commit()
        await db.refresh(integration)

    # Also keep Integration(provider="resend") in sync if Resend is configured
    if payload.resend_from_email or encrypted_resend:
        resend_integ = await db.scalar(
            select(Integration).where(
                Integration.business_id == business_id,
                Integration.provider == "resend"
            )
        )
        if resend_integ:
            resend_integ.config = {
                "api_key": encrypted_resend,
                "from_email": payload.resend_from_email,
                "from_name": payload.sender_name,
            }
            resend_integ.is_active = (provider == "resend")
        else:
            db.add(Integration(
                business_id=business_id,
                provider="resend",
                config={
                    "api_key": encrypted_resend,
                    "from_email": payload.resend_from_email,
                    "from_name": payload.sender_name,
                },
                is_active=(provider == "resend")
            ))
        await db.commit()

    # Automatically synchronize email credentials to .env file
    email_env_map = {}
    if payload.smtp_host:
        email_env_map["SMTP_HOST"] = str(payload.smtp_host).strip()
    if payload.smtp_port:
        email_env_map["SMTP_PORT"] = str(payload.smtp_port).strip()
    if payload.email_address:
        email_env_map["SMTP_USER"] = str(payload.email_address).strip()
    if pwd and pwd != "••••••••••••••••••••":
        email_env_map["SMTP_PASS"] = f'"{pwd.strip()}"'
    if resend_key and resend_key != "••••••••••••••••••••":
        email_env_map["RESEND_API_KEY"] = str(resend_key).strip()
    if payload.resend_from_email:
        email_env_map["RESEND_FROM_EMAIL"] = str(payload.resend_from_email).strip()
    if email_env_map:
        update_env_file(email_env_map)

    return {
        "success": True,
        "status": "connected",
        "message": "Email configuration saved successfully!",
        "provider": provider,
    }


@router.post("/email/test-connection")
async def test_email_connection_endpoint(
    payload: TestConnectionRequest,
    business_id: str = Depends(get_current_business_id_optional),
    db: AsyncSession = Depends(get_db),
):
    """Tests live connectivity to the selected provider without sending an email."""
    provider = (payload.provider or "smtp").lower().strip()
    saved = await resolve_email_config(business_id, db)

    # Merge payload with saved config for any masked or missing values
    test_cfg = dict(saved)
    if payload.smtp_host:
        test_cfg["smtp_host"] = payload.smtp_host
    if payload.smtp_port:
        test_cfg["smtp_port"] = payload.smtp_port
    if payload.email_address:
        test_cfg["email_address"] = payload.email_address
    if payload.password and payload.password != "••••••••••••••••••••":
        test_cfg["password"] = payload.password
    if payload.encryption:
        test_cfg["encryption"] = payload.encryption
    if payload.resend_api_key and payload.resend_api_key != "••••••••••••••••••••":
        test_cfg["resend_api_key"] = payload.resend_api_key
    if payload.resend_from_email:
        test_cfg["resend_from_email"] = payload.resend_from_email
    if payload.sendgrid_api_key and payload.sendgrid_api_key != "••••••••••••••••••••":
        test_cfg["sendgrid_api_key"] = payload.sendgrid_api_key
    if payload.sendgrid_from_email:
        test_cfg["sendgrid_from_email"] = payload.sendgrid_from_email

    result = await test_email_connection(provider, test_cfg)
    return result


@router.post("/email/send-test")
async def send_test_email_endpoint(
    payload: SendTestEmailRequest,
    business_id: str = Depends(get_current_business_id_optional),
    db: AsyncSession = Depends(get_db),
):
    """Dispatches a real test email to verify end-to-end inbox delivery."""
    saved = await resolve_email_config(business_id, db)
    provider = (payload.provider or saved.get("provider") or "smtp").lower().strip()

    # Build config overrides
    overrides: dict = {}
    if payload.smtp_host:
        overrides["smtp_host"] = payload.smtp_host
    if payload.smtp_port:
        overrides["smtp_port"] = payload.smtp_port
    if payload.email_address:
        overrides["email_address"] = payload.email_address
    if payload.password and payload.password != "••••••••••••••••••••":
        overrides["password"] = payload.password
    if payload.sender_name:
        overrides["sender_name"] = payload.sender_name
    if payload.encryption:
        overrides["encryption"] = payload.encryption
    if payload.resend_api_key and payload.resend_api_key != "••••••••••••••••••••":
        overrides["resend_api_key"] = payload.resend_api_key
    if payload.resend_from_email:
        overrides["resend_from_email"] = payload.resend_from_email
    if payload.sendgrid_api_key and payload.sendgrid_api_key != "••••••••••••••••••••":
        overrides["sendgrid_api_key"] = payload.sendgrid_api_key
    if payload.sendgrid_from_email:
        overrides["sendgrid_from_email"] = payload.sendgrid_from_email

    subject = f"Test Email Delivery - {provider.upper()} Verification"
    body_text = payload.template if payload.template else (
        f"<div style='font-family: Arial, sans-serif; padding: 20px; background-color: #f8fafc; border-radius: 8px;'>"
        f"<h2 style='color: #4f46e5; margin-bottom: 8px;'>Email Integration Test Successful!</h2>"
        f"<p style='color: #334155; font-size: 14px;'>This is a verification email sent from your <strong>AI Receptionist</strong> platform.</p>"
        f"<ul style='color: #475569; font-size: 13px; line-height: 1.8;'>"
        f"<li><strong>Provider:</strong> {provider.upper()}</li>"
        f"<li><strong>Delivered to:</strong> {payload.to_email}</li>"
        f"<li><strong>Timestamp:</strong> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</li>"
        f"</ul>"
        f"<p style='color: #64748b; font-size: 12px; margin-top: 20px;'>Your email integration is verified and ready for appointment confirmations and reminders.</p>"
        f"</div>"
    )

    result = await send_email(
        to_email=str(payload.to_email),
        subject=subject,
        html_content=body_text,
        business_id=business_id,
        db=db,
        provider_override=provider,
        config_override=overrides if overrides else None,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Failed to send test email. Please check your provider settings."
        )

    return {
        "success": True,
        "message": f"Test email successfully sent to {payload.to_email} via {provider.upper()}!",
        "id": result.get("id"),
        "provider": provider,
    }


@router.post("/email/resend")
async def configure_resend_email(
    payload: ResendConfigRequest,
    business_id: str = Depends(get_current_business_id_optional),
    db: AsyncSession = Depends(get_db),
):
    key = (payload.api_key or "").strip()
    if not key or key == "••••••••••••••••••••":
        import os
        key = os.getenv("RESEND_API_KEY", "").strip()

    from_email = payload.from_email.strip()
    from_name = (payload.from_name or "CallZenza").strip()

    if not from_email:
        raise HTTPException(status_code=400, detail="From Email cannot be empty.")

    # Live verification: authenticate against Resend if a real key is present
    if key and not key.startswith("re_xxxx"):
        check = check_resend_connectivity(api_key=key)
        if not check.get("connected"):
            raise HTTPException(
                status_code=400,
                detail=f"Live Resend verification failed: {check.get('error') or 'Invalid API key'}"
            )

    # Encrypt key for secure DB storage
    encrypted_key = encrypt_api_key(key) if key else ""
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = await db.scalar(
        select(Integration).where(
            Integration.business_id == business_id,
            Integration.provider == "resend"
        )
    )
    if existing:
        existing.config = {
            "api_key": encrypted_key,
            "from_email": from_email,
            "from_name": from_name,
            "last_verified_at": now_iso,
        }
        existing.is_active = True
        await db.commit()
        await db.refresh(existing)
    else:
        integration = Integration(
            business_id=business_id,
            provider="resend",
            config={
                "api_key": encrypted_key,
                "from_email": from_email,
                "from_name": from_name,
                "last_verified_at": now_iso,
            },
            is_active=True,
        )
        db.add(integration)
        await db.commit()
        await db.refresh(integration)

    return {
        "success": True,
        "status": "connected",
        "message": "Resend integration verified and connected successfully!",
        "from_email": from_email,
    }


@router.get("/email/status")
async def get_email_integration_status(
    business_id: str = Depends(get_current_business_id_optional),
    db: AsyncSession = Depends(get_db),
):
    """Returns current status for the frontend badge with live re-verification."""
    cfg = await resolve_email_config(business_id, db)
    provider = cfg.get("provider", "smtp")
    
    existing = await db.scalar(
        select(Integration).where(
            Integration.business_id == business_id,
            Integration.provider.in_(["email_config", "resend", "smtp", "sendgrid"]),
            Integration.is_active == True
        )
    )
    if existing:
        return {
            "connected": True,
            "from_email": cfg.get("email_address") or cfg.get("resend_from_email") or cfg.get("sendgrid_from_email"),
            "provider": provider,
            "source": "database",
        }

    # Fallback env check
    env_key = os.getenv("RESEND_API_KEY") or settings.resend_api_key
    if env_key and "xxxxxxxx" not in env_key:
        check = check_resend_connectivity(api_key=env_key)
        return {
            "connected": check.get("connected", False),
            "from_email": os.getenv("RESEND_FROM_EMAIL") or settings.resend_from_email,
            "provider": "resend",
            "source": "environment",
        }

    return {
        "connected": False,
        "from_email": None,
        "provider": provider,
        "source": "none"
    }


@router.post("/email/disconnect")
async def disconnect_email_integration(
    business_id: str = Depends(get_current_business_id_optional),
    db: AsyncSession = Depends(get_db),
):
    result = await db.scalars(
        select(Integration).where(
            Integration.business_id == business_id,
            Integration.provider.in_(["email_config", "resend", "smtp", "sendgrid"])
        )
    )
    for integ in result.all():
        integ.is_active = False
    await db.commit()
    return {"success": True, "message": "Email integration disconnected."}