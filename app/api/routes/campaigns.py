import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Response, status, BackgroundTasks
from sqlalchemy import select, delete, or_, and_, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from loguru import logger

from app.core.config import settings
from app.core.deps import get_current_business_id, get_current_db_user, require_supervisor_or_admin
from app.db.database import get_db
from app.db.models import Campaign, CampaignLead, Lead, User, Business, Expert, Client, LeadGroup, AIAgent, Call, DialerRun, Integration
from app.schemas.campaign import CampaignCreate, CampaignOut, CampaignUpdate, CampaignLeadOut
from app.workers.tasks import run_campaign_calls
from app.utils.redis_check import is_redis_online
from app.services.email_service import encrypt_api_key, decrypt_api_key
from app.services.tts_service import get_effective_murf_key, get_effective_snapserve_key

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

DEFAULT_BUSINESS_ID = "c3dce06c-8330-4724-ba33-b5282b3c3596"


def check_campaign_access(campaign: Campaign, user: User) -> None:
    """
    Validates if an authenticated user has authorization to access/modify a campaign.
    Enforces multi-tenant organization isolation while allowing default tenant users.
    
    Raises:
        HTTPException 403 if access is forbidden.
    """
    user_role = (user.role or "USER").upper()
    if user_role == "SUPER_ADMIN":
        return

    if user.business_id:
        if campaign.business_id and user.business_id != campaign.business_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You do not have access to this campaign"
            )
        if campaign.business_id == user.business_id or campaign.user_id == user.id:
            return
    elif campaign.user_id == user.id:
        return

    # Specific check for voice agents/experts
    if user_role in ("VOICE_AGENT", "AGENT"):
        if campaign.voice_agent_id == user.id or campaign.agent_id == user.id:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Forbidden: You do not have access to this campaign"
    )

import uuid
import shutil
from pathlib import Path
from fastapi import UploadFile, File

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

@router.post("/upload-pdf")
async def upload_campaign_pdf(
    file: UploadFile = File(...),
):
    file_id = str(uuid.uuid4())
    safe_filename = file.filename or "uploaded_script.txt"
    filename = f"{file_id}_{safe_filename}"
    file_path = UPLOAD_DIR / filename
    
    with file_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Extract text from the uploaded file (PDF, DOCX, TXT, CSV, MD, etc.)
    extracted_text = ""
    lower_fn = safe_filename.lower()
    try:
        if lower_fn.endswith(".pdf"):
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(file_path))
                pages = [p.extract_text() or "" for p in reader.pages]
                extracted_text = "\n".join(pages).strip()
            except Exception as pe:
                logger.warning(f"Could not extract PDF with pypdf: {pe}")
        elif lower_fn.endswith((".docx", ".doc")):
            try:
                import docx
                doc = docx.Document(str(file_path))
                extracted_text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()]).strip()
            except Exception as de:
                logger.warning(f"Could not extract DOCX text: {de}")
        else:
            # Plain text, CSV, markdown, JSON, HTML, etc.
            try:
                with file_path.open("r", encoding="utf-8", errors="replace") as tf:
                    extracted_text = tf.read().strip()
            except Exception as te:
                logger.warning(f"Could not read text file: {te}")
    except Exception as e:
        logger.warning(f"Text extraction failed for {safe_filename}: {e}")
        
    return {
        "filename": safe_filename,
        "url": f"/static/uploads/{filename}",
        "extracted_text": extracted_text
    }

from app.db.models import Campaign, CampaignLead, Lead, User, Business, Client

@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    try:
        business_id = user.business_id
        if not business_id:
            biz = await db.scalar(select(Business).order_by(Business.created_at))
            if not biz:
                biz = Business(
                    id="c3dce06c-8330-4724-ba33-b5282b3c3596",
                    name="CallZenza Primary",
                    domain="callzenza.com",
                )
                db.add(biz)
                await db.flush()
            business_id = biz.id

        client_id = payload.client_id if payload.client_id and payload.client_id.strip() else None
        client_name = payload.client_name or ""
        client_phone = payload.client_phone or ""

        if client_id:
            client_obj = await db.get(Client, client_id)
            if client_obj:
                client_name = client_obj.name or client_obj.company_name or client_name
                client_phone = client_obj.contact_number or client_phone
            else:
                # Client ID does not exist on this server DB — reset to prevent ForeignKeyViolation
                client_id = None

        if not client_id and client_name.strip():
            # Auto-link or create Client record
            existing_client = await db.scalar(
                select(Client).where(Client.name == client_name.strip(), Client.business_id == business_id)
            )
            if not existing_client:
                existing_client = Client(
                    id=str(uuid.uuid4()),
                    business_id=business_id,
                    name=client_name.strip(),
                    company_name=payload.client_company or client_name.strip(),
                    contact_number=client_phone,
                )
                db.add(existing_client)
                await db.flush()
            client_id = existing_client.id

        # Verify foreign keys exist in DB before referencing to prevent ForeignKeyViolation
        valid_agent_id = None
        if payload.agent_id and payload.agent_id.strip():
            if await db.get(AIAgent, payload.agent_id.strip()):
                valid_agent_id = payload.agent_id.strip()

        valid_voice_agent_id = None
        if payload.voice_agent_id and payload.voice_agent_id.strip():
            if await db.get(Expert, payload.voice_agent_id.strip()):
                valid_voice_agent_id = payload.voice_agent_id.strip()

        valid_lead_group_id = None
        if payload.lead_group_id and payload.lead_group_id.strip():
            if await db.get(LeadGroup, payload.lead_group_id.strip()):
                valid_lead_group_id = payload.lead_group_id.strip()

        valid_sip_account_id = None
        if payload.sip_account_id and payload.sip_account_id.strip():
            from app.services.sip_account_service import validate_sip_account_for_campaign
            is_valid, err_msg, sip_acc = await validate_sip_account_for_campaign(
                db, payload.sip_account_id.strip(), business_id, call_direction="outbound"
            )
            if not is_valid:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)
            valid_sip_account_id = sip_acc.id

        # Validate script markup in schedule if provided
        if payload.schedule and isinstance(payload.schedule, dict):
            script_val = payload.schedule.get("script")
            if script_val and isinstance(script_val, str):
                from app.services.script_parser import validate_script_markup
                is_valid, err_msg = validate_script_markup(script_val)
                if not is_valid:
                    raise HTTPException(status_code=422, detail=err_msg)

        carrier_id = payload.carrier_id
        trunk_id = payload.trunk_id
        caller_did_id = payload.caller_did_id
        twilio_number = payload.twilio_number or ""

        if valid_sip_account_id and sip_acc:
            acc_settings = sip_acc.settings or {}
            if not carrier_id and acc_settings.get("carrier_id"):
                carrier_id = acc_settings.get("carrier_id")
            if not trunk_id and acc_settings.get("trunk_id"):
                trunk_id = acc_settings.get("trunk_id")
            if not caller_did_id and acc_settings.get("caller_did_id"):
                caller_did_id = acc_settings.get("caller_did_id")
            if (not twilio_number or twilio_number == "LOCAL_TEST") and sip_acc.caller_id:
                twilio_number = sip_acc.caller_id

        if caller_did_id and str(caller_did_id).strip() and valid_sip_account_id:
            from app.services.sip_account_service import validate_sip_did_for_campaign
            is_valid_did, did_err, did_obj = await validate_sip_did_for_campaign(
                db, valid_sip_account_id, str(caller_did_id).strip(), business_id
            )
            if not is_valid_did:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=did_err)
            if did_obj and did_obj.phone_number:
                twilio_number = did_obj.phone_number.strip()

        # Process and validate campaign voice_config
        raw_vc = payload.voice_config or {}
        if hasattr(raw_vc, "model_dump"):
            raw_vc = raw_vc.model_dump(exclude_unset=True)
        elif not isinstance(raw_vc, dict):
            raw_vc = {}

        if raw_vc:
            prov = str(raw_vc.get("provider") or "murf").lower().strip()
            if prov not in ("murf", "elevenlabs", "fish_audio", "snapserve"):
                raise HTTPException(status_code=400, detail=f"Unsupported TTS provider: {prov}")
            raw_vc["provider"] = prov

            cred_id = raw_vc.get("credential_id")
            if cred_id:
                integ = await db.scalar(
                    select(Integration).where(
                        Integration.id == cred_id,
                        Integration.business_id == business_id,
                        Integration.is_active == True,
                    )
                )
                if not integ:
                    raise HTTPException(
                        status_code=400,
                        detail="Selected credential does not exist or does not belong to your organization."
                    )
                raw_vc["has_credential"] = True

            custom_key = raw_vc.get("api_key")
            if custom_key and str(custom_key).strip() and not str(custom_key).startswith("••"):
                raw_vc["api_key"] = encrypt_api_key(str(custom_key).strip())
                raw_vc["has_credential"] = True
            elif cred_id:
                raw_vc["api_key"] = None

        camp_sched = payload.schedule or {}
        if raw_vc:
            camp_sched["voice_config"] = raw_vc

        campaign = Campaign(
            business_id=business_id,
            user_id=user.id,
            name=payload.name,
            agent_type=payload.agent_type or "AI_VOICE",
            agent_id=valid_agent_id,
            voice_agent_id=valid_voice_agent_id,
            lead_group_id=valid_lead_group_id,
            client_id=client_id,
            client_name=client_name or "",
            client_phone=client_phone or "",
            twilio_number=twilio_number,
            carrier_id=carrier_id,
            trunk_id=trunk_id,
            caller_did_id=caller_did_id,
            sip_account_id=valid_sip_account_id,
            schedule=camp_sched,
            voice_config=raw_vc,
            status="IDLE",
            auto_dial_enabled=bool(payload.auto_dial_enabled) if payload.auto_dial_enabled is not None else False,
            auto_dial_ratio=int(payload.auto_dial_ratio) if payload.auto_dial_ratio is not None else 1,
        )
        db.add(campaign)
        await db.flush()  # get campaign.id

        target_lead_ids = list(payload.lead_ids or [])

        # If lead_group_id provided and target_lead_ids is empty, fetch all leads in group
        if valid_lead_group_id and not target_lead_ids:
            group_leads = (await db.scalars(select(Lead.id).where(Lead.group_id == valid_lead_group_id))).all()
            target_lead_ids = list(group_leads)

        # Add leads to campaign_leads (only assign to voice agent if primary campaign agent is a human voice agent)
        is_human_agent = (campaign.agent_type == "HUMAN")
        assigned_expert = campaign.voice_agent_id if is_human_agent else None
        for lead_id in target_lead_ids:
            lead = await db.get(Lead, lead_id)
            if lead:
                if business_id and lead.business_id != business_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Lead '{lead_id}' does not belong to your organization."
                    )
                if assigned_expert:
                    lead.assigned_expert_id = assigned_expert
                    db.add(lead)
                campaign_lead = CampaignLead(
                    campaign_id=campaign.id,
                    lead_id=lead_id,
                    assigned_expert_id=assigned_expert,
                    status="PENDING",
                )
                db.add(campaign_lead)

        await db.commit()
        await db.refresh(campaign)
        return await _enrich_campaign(campaign, db)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Campaign creation error")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Campaign creation failed: {str(exc)[:200]}",
        )


async def _enrich_campaign(c: Campaign, db: AsyncSession) -> Campaign:
    try:
        if c.client_id and not c.client_name:
            client_obj = await db.get(Client, c.client_id)
            if client_obj:
                c.client_name = client_obj.name or client_obj.company_name or ""
                c.client_phone = client_obj.contact_number or ""
    except Exception:
        pass

    try:
        if c.voice_agent_id and not getattr(c, "voice_agent_name", None):
            expert_obj = await db.get(Expert, c.voice_agent_id)
            if expert_obj:
                setattr(c, "voice_agent_name", expert_obj.name or "")
    except Exception:
        pass

    try:
        if c.lead_group_id and not getattr(c, "lead_group_name", None):
            group_obj = await db.get(LeadGroup, c.lead_group_id)
            if group_obj:
                setattr(c, "lead_group_name", group_obj.filename or "")
    except Exception:
        pass

    try:
        if c.sip_account_id:
            from app.db.models import SIPAccount
            sip_acc = await db.get(SIPAccount, c.sip_account_id)
            if sip_acc:
                setattr(c, "sip_account_name", sip_acc.name)
                setattr(c, "sip_account_status", "enabled" if sip_acc.enabled else "disabled")
                reg_st = sip_acc.registration_status or "UNREGISTERED"
                try:
                    from app.services.sip_inbound_daemon import get_daemon_registration_info
                    d_info = get_daemon_registration_info(sip_acc.id)
                    if not sip_acc.enabled:
                        reg_st = "DISABLED"
                    elif d_info.get("is_registered") and d_info.get("account_id") == sip_acc.id:
                        reg_st = "REGISTERED"
                    elif sip_acc.direction and sip_acc.direction.lower() == "outbound" and sip_acc.last_health_status == "HEALTHY":
                        reg_st = "PEER_ACTIVE"
                except Exception:
                    pass
                setattr(c, "sip_account_registration", reg_st)
    except Exception as e:
        logger.warning(f"Error enriching campaign SIP account: {e}")

    try:
        dialable_cnt = (
            await db.scalar(
                select(func.count(CampaignLead.id)).where(
                    CampaignLead.campaign_id == c.id,
                    CampaignLead.status.in_(["PENDING", "RETRY_SCHEDULED"])
                )
            )
        ) or 0
        total_cnt = (
            await db.scalar(
                select(func.count(CampaignLead.id)).where(
                    CampaignLead.campaign_id == c.id
                )
            )
        ) or 0

        if total_cnt == 0 and c.lead_group_id:
            total_cnt = (
                await db.scalar(
                    select(func.count(Lead.id)).where(Lead.group_id == c.lead_group_id)
                )
            ) or 0
            dialable_cnt = (
                await db.scalar(
                    select(func.count(Lead.id)).where(
                        Lead.group_id == c.lead_group_id,
                        Lead.status.in_(["NEW", "PENDING", "ACTIVE", "RETRY_SCHEDULED"])
                    )
                )
            ) or total_cnt

        setattr(c, "dialable_leads", dialable_cnt)
        setattr(c, "total_leads", total_cnt)
    except Exception as err:
        logger.warning(f"Error enriching campaign leads count: {err}")

    try:
        # Mask credentials in voice_config so plaintext or raw ciphertext is never returned
        if getattr(c, "voice_config", None) and isinstance(c.voice_config, dict):
            safe_cfg = dict(c.voice_config)
            raw_k = safe_cfg.get("api_key")
            if raw_k:
                safe_cfg["has_credential"] = True
                safe_cfg["api_key"] = "••••••••"
            elif safe_cfg.get("credential_id"):
                safe_cfg["has_credential"] = True
            c.voice_config = safe_cfg
    except Exception as e:
        logger.warning(f"Error enriching campaign voice_config: {e}")

    return c


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        user_role = (user.role or "USER").upper()
        if user.business_id:
            if user_role in ("VOICE_AGENT", "AGENT"):
                expert = await db.scalar(select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email)))
                if expert:
                    result = await db.scalars(
                        select(Campaign)
                        .where(
                            and_(
                                Campaign.business_id == user.business_id,
                                or_(
                                    Campaign.voice_agent_id == expert.id,
                                    Campaign.agent_id == expert.id,
                                    Campaign.voice_agent_id == user.id,
                                    Campaign.agent_id == user.id,
                                    Campaign.business_id == user.business_id
                                )
                            )
                        )
                        .order_by(Campaign.created_at.desc())
                    )
                else:
                    result = await db.scalars(
                        select(Campaign).where(
                            Campaign.business_id == user.business_id
                        ).order_by(Campaign.created_at.desc())
                    )
            else:
                stmt = select(Campaign).where(Campaign.business_id == user.business_id).order_by(Campaign.created_at.desc())
                result = await db.scalars(stmt)
        elif user_role == "SUPER_ADMIN":
            stmt = select(Campaign).order_by(Campaign.created_at.desc())
            result = await db.scalars(stmt)
        else:
            return []

        camps = list(result.all())
        for c in camps:
            await _enrich_campaign(c, db)
        return camps
    except Exception as exc:
        logger.exception(f"Error fetching campaigns: {exc}")
        return []


@router.get("/active-pd-status", response_model=dict)
async def get_active_pd_status_alias(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice_agents import get_active_pd_status
    return await get_active_pd_status(user=user, db=db)


@router.get("/twilio-number")
async def get_twilio_number_alias(
    business_id: str = Depends(get_current_business_id),
) -> dict:
    from app.api.routes.integrations import get_twilio_number
    return await get_twilio_number(business_id=business_id)


@router.get("/voice-providers")
async def get_voice_providers(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Lists supported TTS providers, metadata, parameters, and saved organization credentials.
    Strictly filters integrations by the user's business_id.
    """
    target_bid = user.business_id

    saved_integrations = (
        await db.scalars(
            select(Integration).where(
                Integration.business_id == target_bid,
                Integration.provider.in_(["murf", "elevenlabs", "fish_audio", "snapserve"]),
                Integration.is_active == True,
            )
        )
    ).all()

    org_credentials = []
    for integ in saved_integrations:
        cfg = integ.config or {}
        raw_key = str(cfg.get("api_key") or "")
        label = cfg.get("label") or f"{integ.provider.title()} Credential"
        masked = ("••••••••" + raw_key[-4:]) if len(raw_key) > 4 else "••••••••"
        org_credentials.append({
            "id": integ.id,
            "provider": integ.provider,
            "label": label,
            "masked_key": masked,
            "is_active": integ.is_active,
        })

    providers_catalog = [
        {
            "id": "murf",
            "name": "Murf AI",
            "description": "Ultra low-latency conversational speech synthesis with expressive emotion styles.",
            "default_model": "falcon",
            "models": [
                {"id": "falcon", "name": "Murf Falcon (Ultra Low Latency Streaming)", "sample_rate": 8000},
                {"id": "GEN2", "name": "Murf Gen 2", "sample_rate": 8000},
            ],
            "default_voice": "Pooja",
            "voices": [
                {"id": "Pooja", "name": "Pooja (Indian English / Conversational Female)", "gender": "Female", "locale": "en-IN"},
                {"id": "Marcus", "name": "Marcus (Authoritative Male)", "gender": "Male", "locale": "en-US"},
                {"id": "Natalie", "name": "Natalie (Friendly Female)", "gender": "Female", "locale": "en-US"},
                {"id": "Matthew", "name": "Matthew (Professional Male)", "gender": "Male", "locale": "en-US"},
                {"id": "Alicia", "name": "Alicia (Warm Female)", "gender": "Female", "locale": "en-US"},
                {"id": "Ken", "name": "Ken (Energetic Male)", "gender": "Male", "locale": "en-US"},
                {"id": "Clint", "name": "Clint (Deep Voice Male)", "gender": "Male", "locale": "en-US"},
            ],
            "styles": ["Conversational", "Promo", "Narrative", "Sad", "Angry", "Calm"],
            "supported_parameters": ["voice_id", "model", "style", "rate", "pitch", "channel_type", "volume_offset"],
            "has_env_fallback": bool(get_effective_murf_key()),
        },
        {
            "id": "elevenlabs",
            "name": "ElevenLabs",
            "description": "High-fidelity multilingual neural voices with fine-grained prosody control.",
            "default_model": "eleven_turbo_v2_5",
            "models": [
                {"id": "eleven_turbo_v2_5", "name": "Eleven Turbo v2.5 (Fastest Latency)"},
                {"id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2 (29 Languages)"},
                {"id": "eleven_flash_v2_5", "name": "Eleven Flash v2.5"},
            ],
            "default_voice": "EXAVITQu4vr4xnSDxMaL",
            "voices": [
                {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella (Expressive Female)", "gender": "Female", "locale": "en-US"},
                {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel (Calm Female)", "gender": "Female", "locale": "en-US"},
                {"id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi (Strong Female)", "gender": "Female", "locale": "en-US"},
                {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni (Well-rounded Male)", "gender": "Male", "locale": "en-US"},
                {"id": "VR6AewLTigWG4xSOukaG", "name": "Arnold (Crisp Male)", "gender": "Male", "locale": "en-US"},
                {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam (Deep Male)", "gender": "Male", "locale": "en-US"},
            ],
            "supported_parameters": ["voice_id", "model_id", "stability", "similarity_boost", "style", "speed", "use_speaker_boost"],
            "has_env_fallback": bool(settings.elevenlabs_api_key or os.getenv("ELEVENLABS_API_KEY")),
        },
        {
            "id": "fish_audio",
            "name": "Fish Audio",
            "description": "Cutting-edge voice cloning and synthesis via official Fish Audio REST API.",
            "default_model": "speech-1.5",
            "models": [
                {"id": "speech-1.5", "name": "Fish Speech 1.5"},
            ],
            "default_voice": "",
            "voices": [
                {"id": "", "name": "Default Voice Model", "gender": "Neutral", "locale": "multi"},
            ],
            "supported_parameters": ["reference_id", "model", "temperature", "top_p", "latency", "speed", "volume"],
            "has_env_fallback": bool(getattr(settings, "fish_audio_api_key", None) or os.getenv("FISH_AUDIO_API_KEY")),
        },
        {
            "id": "snapserve",
            "name": "SnapServe AI",
            "description": "Native low-latency telephony voice engine optimized for 8 kHz G.711 μ-law playback.",
            "default_model": "default",
            "models": [
                {"id": "default", "name": "SnapServe Telephony Engine", "sample_rate": 8000},
            ],
            "default_voice": "ca3007f96ae7499ab87d27ea3599956a",
            "voices": [
                {"id": "ca3007f96ae7499ab87d27ea3599956a", "name": "SnapServe Premium Female (8kHz)", "gender": "Female", "locale": "en-US"},
            ],
            "supported_parameters": ["voice_id", "format", "sample_rate"],
            "has_env_fallback": bool(get_effective_snapserve_key()),
        },
    ]

    return {
        "providers": providers_catalog,
        "saved_credentials": org_credentials,
    }


@router.get("/voice-providers/{provider}/voices")
async def get_provider_voices(
    provider: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetches voices dynamically for a specific provider.
    Uses organization credentials or fallback catalogs.
    """
    prov = provider.lower().strip()
    target_bid = user.business_id

    from app.services.tts_service import resolve_tts_credential
    from app.api.routes.voice_configs import _fetch_provider_voices_internal

    key = await resolve_tts_credential(prov, business_id=target_bid, db=db)
    return await _fetch_provider_voices_internal(prov, key)


@router.post("/voice-preview")
async def preview_campaign_voice(
    payload: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Synthesizes and streams preview audio for the configured voice settings.
    Enforces organization ownership of referenced credentials.
    """
    text = payload.get("text") or "Hello, this is a live voice preview for your campaign."
    voice_config = payload.get("voice_config") or payload
    target_bid = user.business_id or "00000000-0000-0000-0000-000000000001"
    is_super = str(user.role or "").upper().strip() in {"SUPER_ADMIN", "SUPERADMIN"}

    cred_id = voice_config.get("credential_id")
    if cred_id:
        integ = await db.get(Integration, cred_id)
        if not integ or (not is_super and integ.business_id != target_bid):
            raise HTTPException(status_code=400, detail="Invalid credential: not found in your organization.")

    from app.services.tts_service import synthesize_preview
    try:
        audio_bytes = await synthesize_preview(
            text=text,
            voice_config=voice_config,
            business_id=target_bid,
            db=db,
        )
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Failed to synthesize voice preview audio.")

        media_type = "audio/wav" if audio_bytes.startswith(b"RIFF") else "audio/mpeg"
        return Response(content=audio_bytes, media_type=media_type)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[VOICE PREVIEW] Synthesis error: {exc}")
        raise HTTPException(status_code=500, detail=f"Voice preview failed: {str(exc)}")


@router.get("/{id}", response_model=CampaignOut)
async def get_campaign(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)
    await _enrich_campaign(campaign, db)
    return campaign


@router.get("/{id}/script", response_model=dict)
async def get_campaign_script(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)
        
    sched = campaign.schedule if isinstance(campaign.schedule, dict) else {}
    raw_script = sched.get("script") or getattr(campaign, "script", None) or sched.get("description") or f"Hi, I am calling regarding {campaign.name}."
    
    questions = sched.get("qualification_questions") or []
    if not questions and isinstance(raw_script, str):
        import re
        lines = [line.strip() for line in raw_script.splitlines() if line.strip()]
        for line in lines:
            if line.endswith("?") or re.match(r"^(Question|\d+[\.\)])", line, re.IGNORECASE):
                cleaned = re.sub(r"^(Question\s*\d+[\:\-]?|\d+[\.\)])\s*", "", line, flags=re.IGNORECASE).strip()
                if len(cleaned) > 5 and cleaned not in questions:
                    questions.append(cleaned)

    if not questions:
        # Dynamically extract questions from the script text — no hardcoded industry assumptions
        text_full = f"{campaign.name} {sched.get('objective', '')} {raw_script}"
        # Try to extract any question-like lines from script or objective
        for line in text_full.splitlines():
            line_clean = line.strip()
            if line_clean.endswith("?") and len(line_clean) > 10 and line_clean not in questions:
                questions.append(line_clean)
                if len(questions) >= 3:
                    break
        if not questions:
            questions = [
                f"1. Are you interested in {campaign.name}?",
                "2. What is your current requirement or timeframe?",
                "3. Are you the key decision maker for this decision?",
            ]

    required_info = sched.get("required_info") or []
    if not required_info:
        # Dynamically extract required fields from script — no hardcoded industry assumptions
        text_full = f"{campaign.name} {sched.get('objective', '')} {raw_script}"
        for line in text_full.splitlines():
            line_clean = line.strip()
            # Look for lines that look like required field labels (short, title-case, no punctuation sentence)
            if (10 < len(line_clean) < 60
                    and not line_clean.endswith("?")
                    and re.match(r'^[A-Z][A-Za-z\s&]+$', line_clean)
                    and line_clean not in required_info):
                required_info.append(line_clean)
                if len(required_info) >= 3:
                    break
        if not required_info:
            required_info = [
                "Customer Name Verification",
                "Contact Number & Email",
                "Service Requirement Details",
            ]

    transfer_rules = sched.get("transfer_rules") or f"Transfer to Sales Representative if customer meets qualification criteria for {campaign.name}."

    from app.services.script_parser import parse_script_segments
    segments = parse_script_segments(str(raw_script), strict=False)
    detected_emotions = list(dict.fromkeys(s.get("emotion") for s in segments if s.get("emotion")))
    total_pause_duration = sum(s.get("duration", 0) for s in segments if s.get("type") == "pause")

    return {
        "campaign_id": campaign.id,
        "campaign_name": campaign.name,
        "script": raw_script,
        "segments": segments,
        "detected_emotions": detected_emotions,
        "total_pause_duration": round(total_pause_duration, 2),
        "qualification_questions": questions,
        "required_info": required_info,
        "transfer_rules": transfer_rules,
        "client_name": campaign.client_name or "",
        "client_phone": campaign.client_phone or "",
    }


@router.get("/{id}/leads", response_model=list[CampaignLeadOut])
async def get_campaign_leads(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    result = await db.scalars(
        select(CampaignLead)
        .where(CampaignLead.campaign_id == id)
        .order_by(CampaignLead.created_at.asc(), CampaignLead.id.asc())
    )
    return list(result.all())


@router.get("/{id}/clients")
async def get_campaign_clients(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    clients_list = []
    seen_ids = set()

    # 1. Fetch leads from CampaignLead table in ascending order
    stmt = (
        select(Lead)
        .join(CampaignLead, CampaignLead.lead_id == Lead.id)
        .where(CampaignLead.campaign_id == id)
        .order_by(CampaignLead.created_at.asc(), Lead.created_at.asc(), Lead.id.asc())
    )
    c_leads = (await db.execute(stmt)).scalars().all()
    for l in c_leads:
        if l.id not in seen_ids:
            seen_ids.add(l.id)
            clients_list.append({
                "id": l.id,
                "client_id": l.id,
                "name": f"{l.first_name or ''} {l.last_name or ''}".strip() or "Customer",
                "phone": l.phone or "",
                "email": l.email or "",
                "company": l.company or "",
                "location": l.location or "",
                "status": l.status or "Active",
                "qualification_status": l.qualification_status or "UNQUALIFIED",
            })

    # 2. Fallback to LeadGroup if CampaignLead is empty or has fewer leads
    if campaign.lead_group_id:
        stmt_g = select(Lead).where(Lead.group_id == campaign.lead_group_id)
        g_leads = (await db.execute(stmt_g)).scalars().all()
        for l in g_leads:
            if l.id not in seen_ids:
                seen_ids.add(l.id)
                clients_list.append({
                    "id": l.id,
                    "client_id": l.id,
                    "name": f"{l.first_name or ''} {l.last_name or ''}".strip() or "Customer",
                    "phone": l.phone or "",
                    "email": l.email or "",
                    "company": l.company or "",
                    "location": l.location or "",
                    "status": l.status or "Active",
                    "qualification_status": l.qualification_status or "UNQUALIFIED",
                })

    # 3. Include Campaign assigned Client if exists
    if campaign.client_id:
        c_obj = await db.get(Client, campaign.client_id)
        if c_obj and c_obj.id not in seen_ids:
            seen_ids.add(c_obj.id)
            clients_list.insert(0, {
                "id": c_obj.id,
                "client_id": c_obj.id,
                "name": c_obj.name or c_obj.company_name or "Primary Client",
                "phone": c_obj.contact_number or "",
                "email": c_obj.email or "",
                "company": c_obj.company_name or "",
                "location": c_obj.address or "",
                "status": c_obj.status or "Active",
                "qualification_status": "QUALIFIED",
            })

    return {
        "campaign_id": id,
        "campaign_name": campaign.name,
        "total_clients": len(clients_list),
        "clients": clients_list,
    }


@router.patch("/{id}", response_model=CampaignOut)
async def update_campaign(
    id: str,
    payload: CampaignUpdate,
    user: User = Depends(require_supervisor_or_admin),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    data = payload.model_dump(exclude_unset=True)
    if "schedule" in data and isinstance(data["schedule"], dict):
        script_val = data["schedule"].get("script")
        if script_val and isinstance(script_val, str):
            from app.services.script_parser import validate_script_markup
            is_valid, err_msg = validate_script_markup(script_val)
            if not is_valid:
                raise HTTPException(status_code=422, detail=err_msg)

    lead_ids = data.pop("lead_ids", None)
    if "sip_account_id" in data:
        raw_sid = data.get("sip_account_id")
        if raw_sid and str(raw_sid).strip():
            from app.services.sip_account_service import validate_sip_account_for_campaign
            is_valid, err_msg, sip_acc = await validate_sip_account_for_campaign(
                db, str(raw_sid).strip(), campaign.business_id, call_direction="outbound"
            )
            if not is_valid:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)
            campaign.sip_account_id = sip_acc.id
            acc_settings = sip_acc.settings or {}
            if not campaign.carrier_id and acc_settings.get("carrier_id"):
                campaign.carrier_id = acc_settings.get("carrier_id")
            if not campaign.trunk_id and acc_settings.get("trunk_id"):
                campaign.trunk_id = acc_settings.get("trunk_id")
            if not campaign.caller_did_id and acc_settings.get("caller_did_id"):
                campaign.caller_did_id = acc_settings.get("caller_did_id")
            if (not campaign.twilio_number or campaign.twilio_number == "LOCAL_TEST") and sip_acc.caller_id:
                campaign.twilio_number = sip_acc.caller_id
        else:
            campaign.sip_account_id = None

    target_sip_acc_id = campaign.sip_account_id
    target_did_id = data.get("caller_did_id", getattr(campaign, "caller_did_id", None))
    if target_did_id and str(target_did_id).strip() and target_sip_acc_id:
        from app.services.sip_account_service import validate_sip_did_for_campaign
        is_val_did, did_err, did_obj = await validate_sip_did_for_campaign(
            db, target_sip_acc_id, str(target_did_id).strip(), campaign.business_id
        )
        if not is_val_did:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=did_err)
        if did_obj and did_obj.phone_number:
            data["twilio_number"] = did_obj.phone_number.strip()
            campaign.twilio_number = did_obj.phone_number.strip()

    if "voice_config" in data and data["voice_config"] is not None:
        v_cfg = data["voice_config"]
        if hasattr(v_cfg, "model_dump"):
            v_cfg = v_cfg.model_dump(exclude_unset=True)
        elif not isinstance(v_cfg, dict):
            v_cfg = {}

        if v_cfg:
            prov = str(v_cfg.get("provider") or "murf").lower().strip()
            if prov not in ("murf", "elevenlabs", "fish_audio", "snapserve"):
                raise HTTPException(status_code=400, detail=f"Unsupported TTS provider: {prov}")
            v_cfg["provider"] = prov

            cred_id = v_cfg.get("credential_id")
            if cred_id:
                integ = await db.scalar(
                    select(Integration).where(
                        Integration.id == cred_id,
                        Integration.business_id == campaign.business_id,
                        Integration.is_active == True,
                    )
                )
                if not integ:
                    raise HTTPException(
                        status_code=400,
                        detail="Selected credential does not exist or does not belong to your organization."
                    )
                v_cfg["has_credential"] = True

            raw_key = v_cfg.get("api_key")
            if raw_key and str(raw_key).strip() and not str(raw_key).startswith("••"):
                v_cfg["api_key"] = encrypt_api_key(str(raw_key).strip())
                v_cfg["has_credential"] = True
            elif raw_key and str(raw_key).startswith("••"):
                existing_key = (campaign.voice_config or {}).get("api_key")
                v_cfg["api_key"] = existing_key
                v_cfg["has_credential"] = bool(existing_key)
            elif cred_id:
                v_cfg["api_key"] = None

            campaign.voice_config = v_cfg
            if isinstance(campaign.schedule, dict):
                campaign.schedule["voice_config"] = v_cfg
        data.pop("voice_config", None)

    for field, val in data.items():
        setattr(campaign, field, val)

    if lead_ids is not None:
        await db.execute(delete(CampaignLead).where(CampaignLead.campaign_id == id))
        is_human_agent = (campaign.agent_type == "HUMAN")
        assigned_expert = campaign.voice_agent_id if is_human_agent else None
        for lid in lead_ids:
            lead = await db.get(Lead, lid)
            if lead:
                if campaign.business_id and lead.business_id != campaign.business_id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Lead '{lid}' does not belong to your organization."
                    )
                db.add(CampaignLead(campaign_id=id, lead_id=lid, assigned_expert_id=assigned_expert, status="PENDING"))

    await db.commit()
    await db.refresh(campaign)
    await _enrich_campaign(campaign, db)
    return campaign


@router.post("/{id}/start", response_model=CampaignOut)
async def start_campaign(
    id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    # Validate assigned SIP account eligibility before activating campaign
    if not campaign.sip_account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot activate campaign: No outbound SIP account assigned. Please assign an active SIP account to the campaign before starting."
        )
    from app.services.sip_account_service import validate_sip_account_for_campaign
    is_valid, err_msg, sip_acc = await validate_sip_account_for_campaign(
        db, campaign.sip_account_id, campaign.business_id, call_direction="outbound"
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot activate campaign: {err_msg}"
        )

    # Validate or auto-link client
    if not campaign.client_id and not (campaign.client_name and campaign.client_name.strip()):
        first_client = await db.scalar(select(Client).where(Client.business_id == campaign.business_id))
        if first_client:
            campaign.client_id = first_client.id
            campaign.client_name = first_client.name or first_client.company_name or "Client"
            campaign.client_phone = first_client.contact_number or ""
            db.add(campaign)
        else:
            raise HTTPException(status_code=400, detail="Please assign a Client before starting this campaign.")

    # Validate or populate leads from lead group (STRICT: Never pull random business leads)
    c_leads_count = (await db.scalar(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == id))) or 0
    if c_leads_count == 0:
        target_lead_ids = []
        if campaign.lead_group_id:
            group_leads = (await db.scalars(select(Lead.id).where(Lead.group_id == campaign.lead_group_id))).all()
            target_lead_ids = list(group_leads)

        for lid in target_lead_ids:
            lead_obj = await db.get(Lead, lid)
            if lead_obj and lead_obj.business_id == campaign.business_id:
                db.add(CampaignLead(
                    campaign_id=id,
                    lead_id=lid,
                    status="PENDING",
                ))
        if target_lead_ids:
            await db.flush()
            c_leads_count = (await db.scalar(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == id))) or 0

    if c_leads_count == 0:
        raise HTTPException(status_code=400, detail="Cannot start campaign: No leads assigned. Please assign leads or a lead list to this campaign before starting.")

    if not campaign.voice_agent_id and not campaign.agent_id:
        ai_agent = await db.scalar(select(AIAgent).where(AIAgent.business_id == campaign.business_id, AIAgent.status == "ACTIVE"))
        if ai_agent:
            campaign.agent_id = ai_agent.id
            db.add(campaign)
        else:
            raise HTTPException(status_code=400, detail="Please assign a Voice Agent or AI Agent before starting this campaign.")

    from app.workers.dialer import PENDING_STATUSES
    from sqlalchemy import update

    # Ensure eligible dialable leads exist for dialer execution
    pending_cnt = (await db.scalar(
        select(func.count(CampaignLead.id)).where(
            CampaignLead.campaign_id == id,
            CampaignLead.status.in_(PENDING_STATUSES)
        )
    )) or 0

    if pending_cnt == 0:
        existing_lead_ids = set((await db.scalars(
            select(CampaignLead.lead_id).where(CampaignLead.campaign_id == id)
        )).all())
        target_lead_ids = []
        if campaign.lead_group_id:
            group_leads = (await db.scalars(select(Lead.id).where(Lead.group_id == campaign.lead_group_id))).all()
            target_lead_ids = [lid for lid in group_leads if lid not in existing_lead_ids]

        for lid in target_lead_ids:
            db.add(CampaignLead(
                campaign_id=id,
                lead_id=lid,
                status="PENDING",
            ))
        if target_lead_ids:
            await db.flush()
            pending_cnt = len(target_lead_ids)

    if pending_cnt == 0 and (campaign.status == "COMPLETED" or campaign.completed_at is not None):
        # Starting or restarting a previously completed campaign: reset non-qualified called/failed leads to PENDING
        await db.execute(
            update(CampaignLead)
            .where(
                CampaignLead.campaign_id == id,
                CampaignLead.status.in_(["FAILED", "NO_ANSWER", "BUSY", "CANCELLED", "CALLED", "COMPLETED"])
            )
            .values(status="PENDING")
        )
        await db.flush()
        pending_cnt = (await db.scalar(
            select(func.count(CampaignLead.id)).where(
                CampaignLead.campaign_id == id,
                CampaignLead.status.in_(PENDING_STATUSES)
            )
        )) or 0

    from datetime import datetime
    prev_status = campaign.status
    campaign.completed_at = None
    campaign.status = "RUNNING"
    campaign.auto_dial_enabled = True
    if not campaign.started_at:
        campaign.started_at = datetime.utcnow()

    # Check if run exists, create if not present
    stmt = select(DialerRun).where(DialerRun.campaign_id == id, DialerRun.status == "RUNNING")
    run = (await db.scalars(stmt)).first()
    if not run:
        run = DialerRun(campaign_id=id, status="RUNNING")
        db.add(run)

    # Activate Voice Agent / Expert for outbound calling
    if campaign.voice_agent_id:
        expert = await db.get(Expert, campaign.voice_agent_id)
        if expert:
            expert.auto_dial_enabled = True
            if expert.status in ("OFFLINE", "PAUSED", "ON BREAK", "COFFEE_BREAK", "LUNCH_BREAK"):
                expert.status = "AVAILABLE"
            db.add(expert)

    logger.info(
        f"[CAMPAIGN_STATE_TRANSITION] campaign_id={campaign.id} prev_status={prev_status} "
        f"new_status={campaign.status} auto_dial_enabled={campaign.auto_dial_enabled} "
        f"reason='user_started_campaign' initiating_function='start_campaign'"
    )
    await db.commit()
    await db.refresh(campaign)

    try:
        from app.api.routes.websockets import global_ws_manager
        await global_ws_manager.broadcast({
            "event": "campaign_status_changed",
            "campaign_id": str(campaign.id),
            "status": str(campaign.status),
            "business_id": str(campaign.business_id or "default")
        })
    except Exception as ws_err:
        logger.warning(f"WebSocket broadcast error on campaign start: {ws_err}")

    # Trigger calling worker if configured for automated or AI Agent calling
    if campaign.auto_dial_enabled or campaign.agent_id or campaign.voice_agent_id:
        try:
            from app.workers.tasks import trigger_campaign_refill
            trigger_campaign_refill(str(campaign.id))
        except Exception as dialer_dispatch_err:
            logger.warning(f"Dialer dispatch notice on campaign start: {dialer_dispatch_err}")
            try:
                from app.workers.tasks import _run_campaign_calls_async
                background_tasks.add_task(_run_campaign_calls_async, campaign.id)
            except Exception as bg_err:
                logger.error(f"Failed fallback background task on campaign start: {bg_err}")

    await _enrich_campaign(campaign, db)
    return campaign


@router.post("/{id}/pause", response_model=CampaignOut)
async def pause_campaign(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Campaign:
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    prev_status = campaign.status
    campaign.status = "PAUSED"
    campaign.auto_dial_enabled = False

    stmt = select(DialerRun).where(DialerRun.campaign_id == id, DialerRun.status == "RUNNING")
    run = (await db.scalars(stmt)).first()
    if run:
        run.status = "PAUSED"
        run.paused_at = datetime.utcnow()

    # Free up un-dispatched reserved leads back to PENDING
    await db.execute(
        update(CampaignLead)
        .where(CampaignLead.campaign_id == id, CampaignLead.status == "RESERVED")
        .values(status="PENDING")
    )

    logger.info(
        f"[CAMPAIGN_STATE_TRANSITION] campaign_id={campaign.id} prev_status={prev_status} "
        f"new_status={campaign.status} auto_dial_enabled={campaign.auto_dial_enabled} "
        f"reason='user_paused_campaign' initiating_function='pause_campaign'"
    )
    await db.commit()
    await db.refresh(campaign)

    try:
        from app.api.routes.websockets import global_ws_manager
        await global_ws_manager.broadcast({
            "event": "campaign_status_changed",
            "campaign_id": str(campaign.id),
            "status": str(campaign.status),
            "business_id": str(campaign.business_id or "default")
        })
    except Exception as ws_err:
        logger.warning(f"WebSocket broadcast error on campaign pause: {ws_err}")

    await _enrich_campaign(campaign, db)
    return campaign


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_campaign(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)
        
    await db.execute(delete(CampaignLead).where(CampaignLead.campaign_id == id))
    await db.delete(campaign)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)



from app.db.models import DialerRun
from datetime import datetime

@router.post("/{id}/dialer/start", response_model=dict)
async def start_dialer(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)
        
    from app.workers.dialer import PENDING_STATUSES
    from sqlalchemy import update

    pending_cnt = (await db.scalar(
        select(func.count(CampaignLead.id)).where(
            CampaignLead.campaign_id == id,
            CampaignLead.status.in_(PENDING_STATUSES)
        )
    )) or 0

    if pending_cnt == 0:
        existing_lead_ids = set((await db.scalars(
            select(CampaignLead.lead_id).where(CampaignLead.campaign_id == id)
        )).all())
        target_lead_ids = []
        if campaign.lead_group_id:
            group_leads = (await db.scalars(select(Lead.id).where(Lead.group_id == campaign.lead_group_id))).all()
            target_lead_ids = [lid for lid in group_leads if lid not in existing_lead_ids]
        if not target_lead_ids and campaign.business_id:
            biz_leads = (await db.scalars(select(Lead.id).where(Lead.business_id == campaign.business_id).limit(100))).all()
            target_lead_ids = [lid for lid in biz_leads if lid not in existing_lead_ids]

        for lid in target_lead_ids:
            db.add(CampaignLead(
                campaign_id=id,
                lead_id=lid,
                status="PENDING",
            ))
        if target_lead_ids:
            await db.flush()
            pending_cnt = len(target_lead_ids)

    if pending_cnt == 0 and (campaign.status == "COMPLETED" or campaign.completed_at is not None):
        await db.execute(
            update(CampaignLead)
            .where(
                CampaignLead.campaign_id == id,
                CampaignLead.status.in_(["FAILED", "NO_ANSWER", "BUSY", "CANCELLED", "CALLED", "COMPLETED"])
            )
            .values(status="PENDING")
        )
        await db.flush()

    prev_status = campaign.status
    campaign.completed_at = None
    campaign.status = "RUNNING"
    campaign.auto_dial_enabled = True
    if not campaign.started_at:
        from datetime import datetime
        campaign.started_at = datetime.utcnow()
    
    # Check if run exists
    stmt = select(DialerRun).where(DialerRun.campaign_id == id, DialerRun.status == "RUNNING")
    run = (await db.scalars(stmt)).first()
    if not run:
        run = DialerRun(campaign_id=id, status="RUNNING")
        db.add(run)

    # Activate Voice Agent / Expert for outbound calling
    if campaign.voice_agent_id:
        expert = await db.get(Expert, campaign.voice_agent_id)
        if expert:
            expert.auto_dial_enabled = True
            if expert.status in ("OFFLINE", "PAUSED", "ON BREAK", "COFFEE_BREAK", "LUNCH_BREAK"):
                expert.status = "AVAILABLE"
            db.add(expert)

    logger.info(
        f"[CAMPAIGN_STATE_TRANSITION] campaign_id={campaign.id} prev_status={prev_status} "
        f"new_status={campaign.status} auto_dial_enabled={campaign.auto_dial_enabled} "
        f"reason='dialer_started' initiating_function='start_dialer'"
    )
        
    await db.commit()
    
    try:
        from app.api.routes.websockets import global_ws_manager
        await global_ws_manager.broadcast({
            "event": "campaign_status_changed",
            "campaign_id": str(campaign.id),
            "status": str(campaign.status),
            "business_id": str(campaign.business_id or "default")
        })
    except Exception as ws_err:
        logger.warning(f"WebSocket broadcast error on dialer start: {ws_err}")

    try:
        from app.workers.tasks import trigger_campaign_refill
        trigger_campaign_refill(str(campaign.id))
    except Exception as err:
        logger.warning(f"Background dialer dispatch notice for campaign {campaign.id}: {err}")
    
    return {"message": "Dialer started"}

@router.post("/{id}/dialer/pause", response_model=dict)
async def pause_dialer(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)
        
    prev_status = campaign.status
    campaign.status = "PAUSED"
    campaign.auto_dial_enabled = False
    
    stmt = select(DialerRun).where(DialerRun.campaign_id == id, DialerRun.status == "RUNNING")
    run = (await db.scalars(stmt)).first()
    if run:
        run.status = "PAUSED"
        run.paused_at = datetime.utcnow()

    # Free up un-dispatched reserved leads back to PENDING
    await db.execute(
        update(CampaignLead)
        .where(CampaignLead.campaign_id == id, CampaignLead.status == "RESERVED")
        .values(status="PENDING")
    )

    logger.info(
        f"[CAMPAIGN_STATE_TRANSITION] campaign_id={campaign.id} prev_status={prev_status} "
        f"new_status={campaign.status} auto_dial_enabled={campaign.auto_dial_enabled} "
        f"reason='dialer_paused' initiating_function='pause_dialer'"
    )
        
    await db.commit()

    try:
        from app.api.routes.websockets import global_ws_manager
        await global_ws_manager.broadcast({
            "event": "campaign_status_changed",
            "campaign_id": str(campaign.id),
            "status": str(campaign.status),
            "business_id": str(campaign.business_id or "default")
        })
    except Exception as ws_err:
        logger.warning(f"WebSocket broadcast error on dialer pause: {ws_err}")

    return {"message": "Dialer paused"}

@router.post("/{id}/dialer/stop", response_model=dict)
async def stop_dialer(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)
        
    prev_status = campaign.status
    campaign.status = "COMPLETED"
    campaign.auto_dial_enabled = False
    campaign.completed_at = datetime.utcnow()
    
    stmt = select(DialerRun).where(DialerRun.campaign_id == id, DialerRun.status.in_(["RUNNING", "PAUSED"]))
    run = (await db.scalars(stmt)).first()
    if run:
        run.status = "STOPPED"
        run.stopped_at = datetime.utcnow()
        
    # Free up reserved leads
    from sqlalchemy import update
    await db.execute(
        update(CampaignLead)
        .where(CampaignLead.campaign_id == id, CampaignLead.status == "RESERVED")
        .values(status="PENDING")
    )

    logger.info(
        f"[CAMPAIGN_STATE_TRANSITION] campaign_id={campaign.id} prev_status={prev_status} "
        f"new_status={campaign.status} auto_dial_enabled={campaign.auto_dial_enabled} "
        f"reason='dialer_stopped' initiating_function='stop_dialer'"
    )
        
    await db.commit()

    try:
        from app.api.routes.websockets import global_ws_manager
        await global_ws_manager.broadcast({
            "event": "campaign_status_changed",
            "campaign_id": str(campaign.id),
            "status": str(campaign.status),
            "business_id": str(campaign.business_id or "default")
        })
    except Exception as ws_err:
        logger.warning(f"WebSocket broadcast error on dialer stop: {ws_err}")

    return {"message": "Dialer stopped"}

@router.get("/{id}/dialer/status", response_model=dict)
async def dialer_status(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)
        
    from datetime import datetime
    now_utc = datetime.utcnow()
    from app.workers.dialer import count_campaign_active_calls, _parse_ratio
    active_count, active_breakdown = await count_campaign_active_calls(db, id)

    # Status breakdown across all campaign leads
    stmt_breakdown = select(CampaignLead.status, func.count(CampaignLead.id)).where(
        CampaignLead.campaign_id == id
    ).group_by(CampaignLead.status)
    breakdown_rows = (await db.execute(stmt_breakdown)).all()
    status_counts = {status: count for status, count in breakdown_rows}
    ratio = _parse_ratio(campaign.auto_dial_ratio)
    target_capacity = max(1, ratio)

    # Reconcile campaign completed state if all leads are done
    rem_pending = status_counts.get("PENDING", 0) + status_counts.get("RETRY_SCHEDULED", 0) + status_counts.get("NEW", 0)
    if rem_pending == 0 and active_count == 0 and campaign.status == "RUNNING":
        logger.info(f"[DIALER_STATUS] Campaign {id} has 0 pending leads and 0 active calls. Auto-transitioning to COMPLETED.")
        campaign.status = "COMPLETED"
        campaign.completed_at = now_utc
        db.add(campaign)
        await db.commit()

    return {
        "status": campaign.status,
        "auto_dial_enabled": campaign.auto_dial_enabled,
        "ratio": ratio,
        "target_capacity": target_capacity,
        "active_calls": active_count,
        "available_slots": max(0, target_capacity - active_count),
        "pending_leads": status_counts.get("PENDING", 0),
        "ringing_calls": status_counts.get("RINGING", 0),
        "completed_calls": status_counts.get("COMPLETED", 0),
        "failed_calls": status_counts.get("FAILED", 0),
        "breakdown": status_counts,
    }



# ---------------------------------------------------------------------------
# Script Parsing, Validation & Emotion-Aware Speech Preview Endpoints
# ---------------------------------------------------------------------------
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List

class ScriptParsePayload(BaseModel):
    script: str = Field(..., description="Script containing <emotion type='...' /> and <break time='...s' /> tags")
    lead_name: Optional[str] = ""
    agent_name: Optional[str] = ""
    strict: Optional[bool] = True

class ScriptUpdatePayload(BaseModel):
    script: str = Field(..., description="Updated script text")

class SpeechPreviewPayload(BaseModel):
    script: Optional[str] = None
    lead_name: Optional[str] = "Customer"
    agent_name: Optional[str] = "Alex"
    voice_id: Optional[str] = None
    lang: Optional[str] = "en"


@router.post("/script/parse")
async def parse_script_general(
    payload: ScriptParsePayload,
    user: User = Depends(get_current_db_user),
):
    """
    Parses any campaign script containing emotion and pause tags into machine-readable segments.
    Validates emotion types and pause durations (0.1s to 10.0s).
    """
    from app.services.script_parser import parse_script_segments, validate_script_markup
    if payload.strict:
        is_valid, err = validate_script_markup(payload.script)
        if not is_valid:
            raise HTTPException(status_code=422, detail=err)

    try:
        segments = parse_script_segments(
            payload.script,
            strict=bool(payload.strict),
            lead_name=payload.lead_name or "",
            agent_name=payload.agent_name or "",
        )
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))

    total_speech = len([s for s in segments if s.get("type") == "speech"])
    total_pause = sum(s.get("duration", 0) for s in segments if s.get("type") == "pause")
    emotions = list(dict.fromkeys(s.get("emotion") for s in segments if s.get("emotion")))

    return {
        "success": True,
        "segments": segments,
        "total_speech_segments": total_speech,
        "total_pause_duration": round(total_pause, 2),
        "detected_emotions": emotions,
    }


@router.post("/speech-preview")
async def preview_speech_general(
    payload: SpeechPreviewPayload,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Generates an emotion-aware speech synthesis preview with exact pause durations
    for a standalone or draft script (e.g. while configuring in Step 2).
    """
    script_text = payload.script or "Hello [Lead Name], this is [Agent Name] calling."
    from app.services.script_parser import validate_script_markup
    is_valid, err = validate_script_markup(script_text)
    if not is_valid:
        raise HTTPException(status_code=422, detail=err)

    from app.services.tts_service import synthesize_script_audio
    try:
        _, audio_url, segments = await synthesize_script_audio(
            script=script_text,
            lang=payload.lang or "en",
            lead_name=payload.lead_name or "Customer",
            agent_name=payload.agent_name or "Alex",
            voice_id_override=payload.voice_id,
        )
    except Exception as exc:
        logger.error(f"Speech preview synthesis error: {exc}")
        raise HTTPException(status_code=500, detail=f"Speech preview generation failed: {exc}")

    total_speech = len([s for s in segments if s.get("type") == "speech"])
    total_pause = sum(s.get("duration", 0) for s in segments if s.get("type") == "pause")
    emotions = list(dict.fromkeys(s.get("emotion") for s in segments if s.get("emotion")))

    return {
        "audio_url": audio_url,
        "segments": segments,
        "total_speech_segments": total_speech,
        "total_pause_duration": round(total_pause, 2),
        "detected_emotions": emotions,
    }


@router.put("/{id}/script", response_model=dict)
async def update_campaign_script(
    id: str,
    payload: ScriptUpdatePayload,
    user: User = Depends(require_supervisor_or_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Updates the campaign script in PostgreSQL schedule and validates all emotion
    and pause tags.
    """
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    from app.services.script_parser import validate_script_markup, parse_script_segments
    is_valid, err = validate_script_markup(payload.script)
    if not is_valid:
        raise HTTPException(status_code=422, detail=err)

    sched = dict(campaign.schedule or {})
    sched["script"] = payload.script
    campaign.schedule = sched
    await db.commit()
    await db.refresh(campaign)

    segments = parse_script_segments(payload.script, strict=False)
    emotions = list(dict.fromkeys(s.get("emotion") for s in segments if s.get("emotion")))
    total_pause = sum(s.get("duration", 0) for s in segments if s.get("type") == "pause")

    return {
        "success": True,
        "campaign_id": campaign.id,
        "script": payload.script,
        "segments": segments,
        "detected_emotions": emotions,
        "total_pause_duration": round(total_pause, 2),
    }


@router.post("/{id}/script/parse", response_model=dict)
async def parse_specific_campaign_script(
    id: str,
    payload: Optional[ScriptParsePayload] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Parses a specific campaign's script with variable resolution using campaign client & agent metadata.
    """
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    sched = campaign.schedule if isinstance(campaign.schedule, dict) else {}
    script_to_parse = (payload.script if payload and payload.script else None) or sched.get("script") or ""

    from app.services.script_parser import parse_script_segments, validate_script_markup
    is_valid, err = validate_script_markup(script_to_parse)
    if not is_valid:
        raise HTTPException(status_code=422, detail=err)

    lead_name = (payload.lead_name if payload and payload.lead_name else None) or campaign.client_name or "Customer"
    agent_name = (payload.agent_name if payload and payload.agent_name else None) or getattr(campaign, "voice_agent_name", None) or "Alex"

    segments = parse_script_segments(script_to_parse, strict=False, lead_name=lead_name, agent_name=agent_name)
    total_speech = len([s for s in segments if s.get("type") == "speech"])
    total_pause = sum(s.get("duration", 0) for s in segments if s.get("type") == "pause")
    emotions = list(dict.fromkeys(s.get("emotion") for s in segments if s.get("emotion")))

    return {
        "campaign_id": campaign.id,
        "segments": segments,
        "total_speech_segments": total_speech,
        "total_pause_duration": round(total_pause, 2),
        "detected_emotions": emotions,
    }


@router.post("/{id}/speech-preview", response_model=dict)
async def preview_campaign_speech(
    id: str,
    payload: Optional[SpeechPreviewPayload] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Generates an emotion-aware audio preview with physical pause silences for a campaign.
    """
    campaign = await db.get(Campaign, id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    check_campaign_access(campaign, user)

    sched = campaign.schedule if isinstance(campaign.schedule, dict) else {}
    script_text = (payload.script if payload and payload.script else None) or sched.get("script") or "Hello [Lead Name], this is [Agent Name] calling."

    from app.services.script_parser import validate_script_markup
    is_valid, err = validate_script_markup(script_text)
    if not is_valid:
        raise HTTPException(status_code=422, detail=err)

    lead_name = (payload.lead_name if payload and payload.lead_name else None) or campaign.client_name or "Customer"
    agent_name = (payload.agent_name if payload and payload.agent_name else None) or getattr(campaign, "voice_agent_name", None) or "Alex"
    lang = (payload.lang if payload and payload.lang else None) or sched.get("default_language") or "en"

    from app.services.tts_service import synthesize_script_audio
    try:
        _, audio_url, segments = await synthesize_script_audio(
            script=script_text,
            lang=lang,
            lead_name=lead_name,
            agent_name=agent_name,
            voice_id_override=payload.voice_id if payload else None,
        )
    except Exception as exc:
        logger.error(f"Campaign speech preview error: {exc}")
        raise HTTPException(status_code=500, detail=f"Audio generation failed: {exc}")

    total_speech = len([s for s in segments if s.get("type") == "speech"])
    total_pause = sum(s.get("duration", 0) for s in segments if s.get("type") == "pause")
    emotions = list(dict.fromkeys(s.get("emotion") for s in segments if s.get("emotion")))

    return {
        "campaign_id": campaign.id,
        "audio_url": audio_url,
        "segments": segments,
        "total_speech_segments": total_speech,
        "total_pause_duration": round(total_pause, 2),
        "detected_emotions": emotions,
    }

