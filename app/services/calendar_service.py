"""Module 22/26 — Google Calendar provider interface + mock implementation."""
from __future__ import annotations
from datetime import datetime
from loguru import logger

from app.core.config import get_settings

settings = get_settings()


async def book_slot(scheduled_at: datetime, notes: str = "") -> dict:
    if settings.google_client_id and settings.google_client_secret:
        # TODO: real Google Calendar API v3 call using stored OAuth tokens.
        logger.info("Would call Google Calendar API for slot {}", scheduled_at)
    else:
        logger.info("[mock-calendar] booked slot {} — {}", scheduled_at, notes)
    return {"provider": "google_calendar", "scheduled_at": scheduled_at.isoformat(), "status": "booked"}
