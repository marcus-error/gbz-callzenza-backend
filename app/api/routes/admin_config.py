from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.core.deps import require_admin
from app.db.database import get_db
from app.db.models import User, SystemConfig

router = APIRouter(prefix="/api/admin/config", tags=["admin_config"])


class ConfigOut(BaseModel):
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    llm_provider: str
    gemini_api_key: str
    openai_api_key: str
    anthropic_api_key: str


class ConfigUpdatePayload(BaseModel):
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_phone_number: Optional[str] = None
    llm_provider: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None


def mask_secret(value: str) -> str:
    if not value or value.strip() == "":
        return ""
    if value.startswith("AC") or len(value) < 8:
        # Keep Twilio SID partly visible or similar short strings
        return value[:6] + "••••••••"
    return "••••••••••••••••••••"


@router.get("", response_model=ConfigOut)
async def get_system_config(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    result = await db.scalars(select(SystemConfig))
    configs = {cfg.config_key: cfg.config_value for cfg in result.all()}

    return {
        "twilio_account_sid": mask_secret(configs.get("twilio_account_sid", "")),
        "twilio_auth_token": "••••••••" if configs.get("twilio_auth_token") else "",
        "twilio_phone_number": configs.get("twilio_phone_number", ""),
        "llm_provider": configs.get("llm_provider", "gemini"),
        "gemini_api_key": "••••••••" if configs.get("gemini_api_key") else "",
        "openai_api_key": "••••••••" if configs.get("openai_api_key") else "",
        "anthropic_api_key": "••••••••" if configs.get("anthropic_api_key") else "",
    }


@router.put("", response_model=ConfigOut)
async def update_system_config(
    payload: ConfigUpdatePayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    fields = payload.model_dump(exclude_unset=True)

    for key, val in fields.items():
        if val is None or "••••" in val:
            # Skip update if it was masked/not edited
            continue

        stmt = select(SystemConfig).where(SystemConfig.config_key == key)
        cfg = await db.scalar(stmt)
        if not cfg:
            cfg = SystemConfig(config_key=key, config_value=val)
            db.add(cfg)
        else:
            cfg.config_value = val
            db.add(cfg)

    await db.commit()

    # Retrieve updated config
    result = await db.scalars(select(SystemConfig))
    configs = {cfg.config_key: cfg.config_value for cfg in result.all()}

    return {
        "twilio_account_sid": mask_secret(configs.get("twilio_account_sid", "")),
        "twilio_auth_token": "••••••••" if configs.get("twilio_auth_token") else "",
        "twilio_phone_number": configs.get("twilio_phone_number", ""),
        "llm_provider": configs.get("llm_provider", "gemini"),
        "gemini_api_key": "••••••••" if configs.get("gemini_api_key") else "",
        "openai_api_key": "••••••••" if configs.get("openai_api_key") else "",
        "anthropic_api_key": "••••••••" if configs.get("anthropic_api_key") else "",
    }
