from contextlib import asynccontextmanager
from pathlib import Path
import os
import re
import sys
import traceback

from fastapi import FastAPI, HTTPException, Request, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db, init_db

from app.api.routes import (
    agents,
    analytics,
    appointments,
    auth,
    business,
    calls,
    conversations,
    experts,
    leads,
    users,
    voice,
    webhooks,
    campaigns,
    integrations,
    websockets,
    lead_groups,
    knowledge_agents,
    knowledge_library,
    admin_users,
    admin_campaigns,
    admin_billing,
    admin_config,
    local_voice_test,
    voice_agents,
    lead_assignment_routes,
    agent_dashboard,
    mgmt_agents,
    clients,
    messages,
    barge_stream,
    voice_barge,
    lead_recycle,
    chatbot,
    inbound,
    user_groups,
    voice_agent_transfer,
    reports,
    dispositions,
    email,
    tenants,
    password_reset,
    organizations,
    tenant_users,
    roles,
    permissions,
    tenant_dashboard,
    branding,
    plans,
    notifications,
    audit_logs,
    report_export,
    tenant_settings,
    gmail_routes,
    super_admin,
    rag_routes,
    testing,
    calls,
    websockets,
    sip_carriers,
    sip_trunks,
    dids,
    telephony,
    sip_accounts,
    campaign_ai_agent,
    demo_requests,
    call_recordings,
    voice_configs,
    conference,
)
from app.api.v1 import agent_dashboard as agent_dashboard_v1
from app.api.v1 import call_transfer as call_transfer_v1
from app.api.v1 import agent_portal_auth as agent_portal_auth_v1
from app.api.v1 import agent_portal as agent_portal_v1
from app.api.v1 import call_history as call_history_v1

# Configure Loguru to write to log file safely on Windows
try:
    logger.add(
        "backend_requests.log",
        rotation="10 MB",
        level="DEBUG",
        enqueue=True,
        catch=True,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"
    )
except Exception:
    pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — initializing database tables")
    await init_db()

    # Pre-warm embedding model in background thread to avoid live call cold-start latency
    try:
        import asyncio
        from app.rag.embedding_service import get_embedding_model
        asyncio.create_task(asyncio.to_thread(get_embedding_model))
        logger.info("🚀 [WARMUP] Background pre-warming of RAG embedding model initiated.")
    except Exception as e:
        logger.warning(f"Could not trigger embedding model warmup: {e}")

    # Startup check for Resend Email integration
    resend_key = os.getenv("RESEND_API_KEY")
    resend_from = os.getenv("RESEND_FROM_EMAIL")
    if resend_key and resend_from and "xxxxxxxx" not in resend_key:
        logger.info(f"✅ [RESEND CONFIG] Resend Email initialized with sender: {resend_from}")
    else:
        logger.info("[RESEND CONFIG] Resend API key not set or using placeholder (SMTP or fallback email will be used).")

    # Start SIP Inbound Listener Daemon for live PSTN/Asterisk incoming calls (checks SIP_INBOUND_ENABLED)
    try:
        from app.services.sip_inbound_daemon import start_sip_daemon, stop_sip_daemon
        start_sip_daemon()
    except Exception as e:
        logger.warning(f"Could not initialize SIP daemon: {e}")

    # Initialize Call Recordings 24-hour expiry lifecycle & storage synchronization
    try:
        from app.services.call_recording_service import sync_all_call_recordings, start_expiry_cleanup_worker, stop_expiry_cleanup_worker
        from app.db.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await sync_all_call_recordings(session)
        start_expiry_cleanup_worker()
    except Exception as e:
        logger.warning(f"Could not initialize call recordings service: {e}")

    # Synchronize existing SIP Carrier/Trunk records with Multi-Tenant SIP Accounts
    try:
        from app.services.sip_account_service import sync_carrier_trunks_to_sip_accounts
        from app.db.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            synced = await sync_carrier_trunks_to_sip_accounts(session)
            logger.info(f"✅ [SIP SYNC] Synchronized {len(synced)} SIP accounts from existing carrier/trunk configuration.")
    except Exception as e:
        logger.warning(f"Could not synchronize SIP accounts on startup: {e}")

    # Reconcile and resume any active running campaigns on backend startup
    try:
        from app.db.database import AsyncSessionLocal
        from app.db.models import Campaign
        from app.workers.tasks import trigger_campaign_refill
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            running_campaign_ids = (await session.scalars(
                select(Campaign.id).where(Campaign.status == "RUNNING")
            )).all()
            for c_id in running_campaign_ids:
                logger.info(f"🚀 [STARTUP_RECOVERY] Resuming running campaign {c_id}")
                trigger_campaign_refill(str(c_id))
    except Exception as e:
        logger.warning(f"Could not reconcile running campaigns on startup: {e}")

    yield
    logger.info("Shutting down")
    try:
        from app.services.call_recording_service import stop_expiry_cleanup_worker
        stop_expiry_cleanup_worker()
    except Exception:
        pass
    try:
        from app.services.voice_service import shutdown_all_active_sip_calls
        shutdown_all_active_sip_calls()
    except Exception as e:
        logger.debug(f"SIP active calls shutdown note: {e}")
    try:
        from app.api.routes.barge_stream import shutdown_all_barge_sessions
        shutdown_all_barge_sessions()
    except Exception as e:
        logger.debug(f"Barge sessions shutdown note: {e}")
    try:
        from app.services.sip_inbound_daemon import stop_sip_daemon
        stop_sip_daemon()
    except Exception:
        pass
    try:
        from app.db.tenant_router import tenant_db_manager
        await tenant_db_manager.close_all()
    except Exception:
        pass


app = FastAPI(
    title="CallZenza API",
    description=(
        "AI-qualifies-leads-then-hands-off-to-humans sales platform backend. "
        "Multi-tenant FastAPI + PostgreSQL + Redis + Celery, LangGraph-style AI "
        "engagement, RBAC, and mock provider interfaces for Twilio/ElevenLabs/"
        "Deepgram/CRM/Calendar/WhatsApp/SendGrid."
    ),
    version="0.2.0",
    lifespan=lifespan,
)

from app.middleware.tenant_subdomain import TenantSubdomainMiddleware
app.add_middleware(TenantSubdomainMiddleware)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path.startswith("/api/v1/"):
        request.scope["path"] = request.url.path.replace("/api/v1/", "/api/", 1)
    logger.debug(f"Incoming request: {request.method} {request.url.path}")
    response = await call_next(request)
    logger.debug(f"Response status for {request.url.path}: {response.status_code}")
    return response

allowed_origins = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "https://www.callzenza.com",
    "https://callzenza.com"
]
env_origins = os.getenv("ALLOWED_ORIGINS")
allow_all = False
if env_origins:
    if env_origins.strip() == "*":
        allow_all = True
    else:
        for o in env_origins.split(","):
            if o.strip():
                allowed_origins.append(o.strip())

cors_kwargs = {
    "allow_credentials": True,
    "allow_methods": ["*"],
    "allow_headers": ["*"],
}

if allow_all:
    cors_kwargs["allow_origin_regex"] = r"https?://.*"
else:
    cors_kwargs["allow_origins"] = list(set(allowed_origins))
    # Accepts: localhost/127.0.0.1 (any port), any server IPv4 address,
    #          *.aminoid.in, *.callzenza.com, *.vercel.app, *.railway.app
    cors_kwargs["allow_origin_regex"] = (
        r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$"
        r"|https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?$"
        r"|https?://([a-zA-Z0-9\-]+\.)*aminoid\.in(:\d+)?$"
        r"|https://[a-zA-Z0-9\-]+\.vercel\.app$"
        r"|https://[a-zA-Z0-9\-]+\.railway\.app$"
        r"|https?://([a-zA-Z0-9\-]+\.)*callzenza\.com(:\d+)?$"
    )

app.add_middleware(
    CORSMiddleware,
    **cors_kwargs
)

# Serves AI-generated call audio so Twilio's <Play> and browser voice test can fetch it via
# {PUBLIC_BASE_URL}/static/audio/<file>.mp3 with full RFC 7233 Range/206 support.
_static_dir = Path(__file__).resolve().parent / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
(_static_dir / "audio").mkdir(parents=True, exist_ok=True)
(_static_dir / "uploads").mkdir(parents=True, exist_ok=True)


@app.get("/static/audio/{filename}")
async def serve_audio_stream(filename: str, request: Request):
    """
    High-performance audio streamer supporting full content (200 OK)
    and partial range requests (206 Partial Content) with precise Content-Length.
    """
    audio_path = (_static_dir / "audio" / filename).resolve()
    if not audio_path.exists() or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    file_size = audio_path.stat().st_size
    range_header = request.headers.get("range")
    content_type = "audio/mpeg" if filename.endswith(".mp3") else "audio/wav"

    if not range_header:
        with open(audio_path, "rb") as f:
            content = f.read()
        return Response(
            content=content,
            status_code=200,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400",
            },
        )

    range_match = re.match(r"bytes=(\d+)-(\d+)?", range_header)
    if not range_match:
        with open(audio_path, "rb") as f:
            content = f.read()
        return Response(
            content=content,
            status_code=200,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    start = int(range_match.group(1))
    end = int(range_match.group(2)) if range_match.group(2) else file_size - 1

    if start >= file_size or end >= file_size or start > end:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_length = end - start + 1
    with open(audio_path, "rb") as f:
        f.seek(start)
        data = f.read(chunk_length)

    return Response(
        content=data,
        status_code=206,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
        },
    )


@app.get("/recordings/{filename}")
@app.get("/api/voice/calls/{call_id}/recording")
async def serve_call_recording(filename: str = "", call_id: str = "", request: Request = None):
    """
    Streams call audio recording with full range/206 support.
    Synthesizes compliant voice audio on demand if file does not exist on disk.
    """
    from app.services.recording_service import ensure_call_recording, RECORDINGS_DIR

    resolved_name = filename or f"{call_id}_rec.wav"
    if not resolved_name.endswith((".wav", ".mp3", ".ogg")):
        resolved_name = f"{resolved_name}.wav"

    audio_path = (RECORDINGS_DIR / resolved_name).resolve()
    clean_cid = resolved_name.replace("_rec.wav", "").replace(".wav", "")
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        ensure_call_recording(clean_cid, duration_sec=15)

    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No recording available for call {clean_cid} (call was not answered, failed, or contains no speech)."
        )

    file_size = audio_path.stat().st_size
    range_header = request.headers.get("range") if request else None
    content_type = "audio/wav"
    if resolved_name.endswith(".mp3"):
        content_type = "audio/mpeg"
    elif resolved_name.endswith(".ogg"):
        content_type = "audio/ogg"

    if not range_header:
        with open(audio_path, "rb") as f:
            content = f.read()
        return Response(
            content=content,
            status_code=200,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400",
            },
        )

    range_match = re.match(r"bytes=(\d+)-(\d+)?", range_header)
    if not range_match:
        with open(audio_path, "rb") as f:
            content = f.read()
        return Response(
            content=content,
            status_code=200,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    start = int(range_match.group(1))
    end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
    if start >= file_size or end >= file_size or start > end:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_length = end - start + 1
    with open(audio_path, "rb") as f:
        f.seek(start)
        data = f.read(chunk_length)

    return Response(
        content=data,
        status_code=206,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
        },
    )


app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Standardized API error envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "message": exc.detail,
            "error_code": f"HTTP_{exc.status_code}",
            "details": None,
        },
    )

for router in (
    auth.router,
    users.router,
    business.router,
    agents.router,
    leads.router,
    conversations.router,
    experts.router,
    appointments.router,
    analytics.router,
    calls.router,
    voice_agent_transfer.router,
    agent_dashboard.router,
    voice.router,
    webhooks.router,
    campaigns.router,
    campaign_ai_agent.router,
    integrations.router,
    websockets.router,
    lead_groups.router,
    knowledge_agents.router,
    knowledge_library.router,
    admin_users.router,
    admin_campaigns.router,
    admin_billing.router,
    admin_config.router,
    local_voice_test.router,
    voice_agents.router,
    lead_assignment_routes.router,
    mgmt_agents.router,
    clients.router,
    messages.router,
    barge_stream.router,
    voice_barge.router,
    lead_recycle.router,
    chatbot.router,
    inbound.router,
    user_groups.router,
    call_recordings.router,
    reports.router,
    dispositions.router,
    email.router,
    tenants.router,
    password_reset.router,
    organizations.router,
    tenant_users.router,
    roles.router,
    permissions.router,
    tenant_dashboard.router,
    branding.router,
    plans.router,
    notifications.router,
    audit_logs.router,
    report_export.router,
    tenant_settings.router,
    gmail_routes.router,
    super_admin.router,
    rag_routes.router,
    testing.router,
    sip_carriers.router,
    sip_trunks.router,
    dids.router,
    telephony.router,
    sip_accounts.router,
    sip_accounts.v1_router,
    demo_requests.router,
    voice_configs.router,
    conference.router,
):
    app.include_router(router)

app.include_router(agent_dashboard_v1.router)
app.include_router(agent_dashboard_v1.api_router)
app.include_router(call_transfer_v1.router)
app.include_router(call_transfer_v1.api_router)
app.include_router(agent_portal_auth_v1.router)
app.include_router(agent_portal_auth_v1.api_router)
app.include_router(agent_portal_v1.router)
app.include_router(agent_portal_v1.api_router)
app.include_router(call_history_v1.router, prefix="/api/v1")
app.include_router(call_history_v1.router, prefix="/api")


# Meta WhatsApp Webhook URL Aliases (ensuring any Meta callback path works out-of-the-box on production)
@app.get("/api/whatsapp/webhook", tags=["webhooks"])
@app.get("/api/webhooks/whatsapp", tags=["webhooks"])
async def alias_verify_whatsapp_webhook(request: Request):
    return await messages.verify_whatsapp_webhook(request)


@app.post("/api/whatsapp/webhook", tags=["webhooks"])
@app.post("/api/webhooks/whatsapp", tags=["webhooks"])
async def alias_receive_whatsapp_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    return await messages.receive_whatsapp_webhook(request, db)


@app.get("/health", tags=["health"])
@app.get("/api/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.2.1"}


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema["servers"] = [{"url": "/"}]  # Force relative URLs in Swagger UI
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    formatted_tb = traceback.format_exc()
    with open('error_trace.log', 'w') as err_file:
        err_file.write(formatted_tb)
    logger.error(f"Unhandled server error on {request.method} {request.url.path}: {exc}\n{formatted_tb}")
    return JSONResponse(status_code=500, content={'message': 'Internal Server Error', 'detail': str(exc)})