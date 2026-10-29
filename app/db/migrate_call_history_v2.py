"""
Call History v2 — Migration & Backfill Script
Creates call_recordings and recording_access_audit tables if they do not exist.
Backfills existing calls.recording_url into call_recordings without fabricating rows.
Additive only: No ALTER on existing tables.
"""
import asyncio
from pathlib import Path
from sqlalchemy import select, text
from loguru import logger

from app.db.database import AsyncSessionLocal, Base, engine
import app.db.call_history_models  # registers CallRecording and RecordingAccessAudit with Base
from app.db.call_history_models import CallRecording
from app.db.models import Call


async def run_migration_and_backfill():
    logger.info("Ensuring call_recordings and recording_access_audit tables exist...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Checking for existing recordings to backfill...")
    async with AsyncSessionLocal() as session:
        # Check existing calls with recording_url
        stmt = select(Call).where(Call.recording_url.isnot(None), Call.recording_url != "")
        result = await session.execute(stmt)
        calls = result.scalars().all()

        backfilled_count = 0
        for call in calls:
            # Check if already present in call_recordings
            existing = await session.execute(
                select(CallRecording).where(
                    CallRecording.call_id == call.id,
                    CallRecording.segment_index == 0
                )
            )
            if existing.scalar_one_or_none():
                continue

            url = call.recording_url
            if not url:
                continue

            source = "provider" if url.startswith("http") else "local"
            storage_provider = "twilio" if "twilio" in url.lower() or url.startswith("http") else "local"
            fmt = "mp3" if url.endswith(".mp3") else "wav"
            duration_ms = (call.duration_seconds or 0) * 1000

            size_bytes = None
            if source == "local" and url.startswith("/recordings/"):
                fname = url.split("/recordings/")[-1]
                rec_dir = Path(__file__).resolve().parent.parent / "static" / "recordings"
                p = rec_dir / fname
                if p.exists():
                    size_bytes = p.stat().st_size

            rec = CallRecording(
                call_id=call.id,
                segment_index=0,
                source=source,
                storage_provider=storage_provider,
                storage_key=url,
                format=fmt,
                channels=1,
                duration_ms=duration_ms,
                size_bytes=size_bytes,
                status="ready",
            )
            session.add(rec)
            backfilled_count += 1

        if backfilled_count > 0:
            await session.commit()
            logger.info(f"Successfully backfilled {backfilled_count} recording rows into call_recordings.")
        else:
            logger.info("No new recordings to backfill.")


if __name__ == "__main__":
    asyncio.run(run_migration_and_backfill())
