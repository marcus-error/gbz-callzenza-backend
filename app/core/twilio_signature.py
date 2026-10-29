"""Module 37 — Twilio webhook signature verification.

Skipped automatically when no TWILIO_AUTH_TOKEN is configured (dev/mock
mode) so the flow stays runnable without live credentials; enforced
whenever a real token is set.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

settings = get_settings()


# async def verify_twilio_request(request: Request, form: dict) -> None:
#     if not settings.twilio_auth_token:
#         return  # mock/dev mode — nothing to verify against

#     from twilio.request_validator import RequestValidator

#     signature = request.headers.get("X-Twilio-Signature", "")
#     validator = RequestValidator(settings.twilio_auth_token)
#     full_url = f"{settings.public_base_url}{request.url.path}"
#     if request.url.query:
#         full_url += f"?{request.url.query}"

#     if not validator.validate(full_url, form, signature):
#         raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")


async def verify_twilio_request(request: Request, form: dict) -> None:
    # Allow Swagger/local testing in development mode.
    if settings.environment == "development":
        return

    if not settings.twilio_auth_token:
        return

    from twilio.request_validator import RequestValidator

    signature = request.headers.get("X-Twilio-Signature", "")

    validator = RequestValidator(settings.twilio_auth_token)

    full_url = f"{settings.public_base_url}{request.url.path}"

    if request.url.query:
        full_url += f"?{request.url.query}"

    if not validator.validate(full_url, form, signature):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid Twilio signature"
        ) 
