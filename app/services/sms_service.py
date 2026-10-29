"""Twilio SMS + Voice service (TS-021 Voice Calling, TS-028 SMS Service)."""

import httpx
from base64 import b64encode

from app.core.config import get_settings

settings = get_settings()

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


def _auth_header() -> dict[str, str]:
    token = b64encode(f"{settings.twilio_account_sid}:{settings.twilio_auth_token}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def send_sms(to: str, body: str) -> dict:
    if not settings.twilio_account_sid:
        return {"status": "stub", "detail": "Set TWILIO_* env vars to send real SMS"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TWILIO_API_BASE}/Accounts/{settings.twilio_account_sid}/Messages.json",
            headers=_auth_header(),
            data={"To": to, "From": settings.twilio_phone_number, "Body": body},
        )
        resp.raise_for_status()
        return resp.json()


async def place_call(to: str, twiml_url: str) -> dict:
    if not settings.twilio_account_sid:
        return {"status": "stub", "detail": "Set TWILIO_* env vars to place real calls"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TWILIO_API_BASE}/Accounts/{settings.twilio_account_sid}/Calls.json",
            headers=_auth_header(),
            data={"To": to, "From": settings.twilio_phone_number, "Url": twiml_url},
        )
        resp.raise_for_status()
        return resp.json()
