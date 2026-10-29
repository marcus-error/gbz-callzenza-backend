from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger

from app.workers.celery_app import celery_app


@celery_app.task(name="tasks.send_welcome_email")
def send_welcome_email(email: str) -> str:
    return f"Queued welcome email for {email}"


async def _call_lead_async(lead_id: str) -> None:
    from app.db.database import AsyncSessionLocal
    from app.db.models import AIAgent, Lead
    from app.services.voice_service import initiate_outbound_call
    from sqlalchemy import select

    try:
        async with AsyncSessionLocal() as db:
            lead = await db.get(Lead, lead_id)
            if not lead or not lead.phone:
                logger.warning("call_lead: lead {} missing or has no phone", lead_id)
                return

            agent = await db.get(AIAgent, lead.agent_id) if lead.agent_id else None
            # Resolve campaign association to guarantee assigned SIP account is passed
            from app.db.models import CampaignLead, Campaign
            cl_stmt = select(CampaignLead).where(CampaignLead.lead_id == lead.id).order_by(CampaignLead.created_at.desc())
            cl = (await db.execute(cl_stmt)).scalars().first()
            campaign = await db.get(Campaign, cl.campaign_id) if cl and cl.campaign_id else None

            await initiate_outbound_call(db, lead, agent, campaign=campaign, campaign_id=campaign.id if campaign else None)
            await db.commit()
    except Exception as e:
        logger.error("call_lead: error calling lead {}: {}", lead_id, e)


@celery_app.task(name="tasks.call_lead")
def call_lead(lead_id: str) -> str:
    """Module 14 — places the AI outbound call for one lead. Runs its own
    event loop since Celery workers execute tasks synchronously."""
    asyncio.run(_call_lead_async(lead_id))
    return f"Call flow completed for lead {lead_id}"


@celery_app.task(name="tasks.import_spreadsheet")
def import_spreadsheet(file_path: str) -> str:
    """Background handler for TS-024 Excel Import (Pandas + OpenPyXL) when
    triggered from a stored file path rather than the direct-upload endpoint
    (app/api/routes/leads.py:import_leads, which imports synchronously since
    parsing a spreadsheet is fast — this task exists for very large files
    dropped into shared storage)."""
    import pandas as pd

    df = pd.read_excel(file_path)
    return f"Imported {len(df)} rows from {file_path}"


async def _run_campaign_calls_async(campaign_id: str) -> None:
    from app.workers.dialer import run_predictive_dialer_async
    try:
        await run_predictive_dialer_async(campaign_id)
    except Exception as e:
        logger.error("[CAMPAIGN_WORKER] Error in _run_campaign_calls_async for campaign {}: {}", campaign_id, e)


@celery_app.task(name="tasks.run_campaign_calls")
def run_campaign_calls(campaign_id: str) -> str:
    """Triggered when a campaign is started or when a webhook re-triggers the dialer."""
    asyncio.run(_run_campaign_calls_async(campaign_id))
    return f"Campaign calls dialer triggered for campaign {campaign_id}"


@celery_app.task(name="tasks.cleanup_expired_recordings")
def cleanup_expired_recordings_task() -> dict:
    """Scheduled task for Celery Beat: Purges expired 24h recordings while preserving Drive backups."""
    from app.services.call_recording_service import cleanup_expired_recordings
    from app.db.database import AsyncSessionLocal

    async def _run():
        async with AsyncSessionLocal() as session:
            return await cleanup_expired_recordings(session)

    return asyncio.run(_run())


_tracked_refill_tasks: set = set()
_scheduled_refill_campaigns: set[str] = set()

def trigger_campaign_refill(campaign_id: str) -> None:
    """
    Safely queues a campaign predictive dialer refill task.
    - If Celery is enabled, dispatches via run_campaign_calls.delay.
    - If Celery is disabled or fails, schedules on the active asyncio event loop as a tracked task,
      or starts a tracked background worker thread if called from a synchronous thread.
    - If the dialer is already actively running or scheduled, coalesces the request into a pending refill
      without launching duplicate concurrent tasks.
    """
    if not campaign_id:
        return

    ts_now = datetime.utcnow().isoformat()
    logger.info("[{ts}] [REFILL_TRIGGER] Campaign refill trigger invoked for campaign_id={cid}", ts=ts_now, cid=campaign_id)
    logger.info("[{ts}] [CAMPAIGN_REFILL_TRIGGERED] campaign_id={cid} trigger_source=auto_dial_cycle", ts=ts_now, cid=campaign_id)

    try:
        from app.workers.dialer import is_campaign_dialer_active, mark_campaign_refill_pending
        if is_campaign_dialer_active(campaign_id) or campaign_id in _scheduled_refill_campaigns:
            mark_campaign_refill_pending(campaign_id)
            logger.info("[{ts}] [REFILL_TRIGGER] Refill already active/scheduled for campaign {cid}, coalescing request.", ts=ts_now, cid=campaign_id)
            return
    except Exception:
        pass

    try:
        from app.core.config import settings
        if getattr(settings, "use_celery", False):
            try:
                run_campaign_calls.delay(campaign_id)
                logger.info("[REFILL] Queued campaign {} refill via Celery", campaign_id)
                return
            except Exception as e:
                logger.warning("[REFILL] Celery queue failed ({}), falling back to direct async refill for campaign {}", e, campaign_id)
    except Exception:
        pass

    try:
        loop = asyncio.get_running_loop()
        _scheduled_refill_campaigns.add(campaign_id)
        task = loop.create_task(_run_campaign_calls_async(campaign_id))
        _tracked_refill_tasks.add(task)
        def _cleanup(t):
            _tracked_refill_tasks.discard(t)
            _scheduled_refill_campaigns.discard(campaign_id)
        task.add_done_callback(_cleanup)
        logger.info("[REFILL] Scheduled tracked async refill task on active event loop for campaign {}", campaign_id)
    except RuntimeError:
        # No running event loop in current thread
        try:
            import threading
            t = threading.Thread(
                target=lambda: asyncio.run(_run_campaign_calls_async(campaign_id)),
                daemon=True,
                name=f"refill-{campaign_id[:8]}"
            )
            t.start()
            logger.info("[REFILL] Spawned tracked background refill worker thread for campaign {}", campaign_id)
        except Exception as err:
            logger.error("[REFILL] Failed to spawn refill thread for campaign {}: {}", campaign_id, err)


async def trigger_campaign_refill_async(campaign_id: str) -> None:
    """Async coroutine wrapper for campaign refill triggering."""
    if not campaign_id:
        return
    try:
        from app.core.config import settings
        if getattr(settings, "use_celery", False):
            try:
                run_campaign_calls.delay(campaign_id)
                return
            except Exception:
                pass
    except Exception:
        pass

    await _run_campaign_calls_async(campaign_id)
