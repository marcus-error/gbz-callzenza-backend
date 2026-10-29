"""
Module: DND & Compliance Suppression Service.
Enforces Do-Not-Call (DND/DNC) persistence, intent recognition, number canonicalization,
and strict pre-dial suppression checks across all outbound telephony entry points.
Zero hardcoded assumptions.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
from loguru import logger
from sqlalchemy import select, or_, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DoNotCallRecord, Lead, CampaignLead, Call


def canonical_phone_key(phone: Any) -> str:
    """Normalizes phone numbers to a canonical digit string (e.g. 10-digit mobile)
    so that +919585310712, 09585310712, 919585310712, and 9585310712 match the exact same key.
    """
    if not phone:
        return ""
    digits = "".join(c for c in str(phone) if c.isdigit())
    if not digits:
        return ""
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) >= 11:
        digits = digits.lstrip("0")
    elif len(digits) > 10 and digits[-10] in "6789":
        digits = digits[-10:]
    return digits


DND_PHRASES_REGEX = re.compile(
    r"""(?ix)
    \b(?:
        # English phrases
        don'?t\s+(?:ever\s+)?call(?:\s+(?:me|this\s+number|here))?(?:\s+again)?|
        do\s+not\s+(?:ever\s+)?call(?:\s+(?:me|this\s+number|here))?(?:\s+again)?|
        stop\s+calling(?:\s+(?:me|this\s+number|here))?|
        remove\s+(?:my\s+)?(?:number|phone|name)(?:\s+from\s+(?:your|the)\s+list)?|
        take\s+(?:my\s+)?(?:number|phone|me)\s+off(?:\s+(?:your|the)\s+list)?|
        delete\s+my\s+number|
        put\s+me\s+on(?:\s+your)?\s+(?:the\s+)?(?:dnd|dnc|do\s+not\s+call)(?:\s+list)?|
        i\s+don'?t\s+want\s+(?:any\s+)?more\s+calls|
        no\s+more\s+calls|
        never\s+call(?:\s+(?:me|this\s+number))?(?:\s+again)?|
        lose\s+my\s+number|
        block\s+(?:my\s+)?number|
        dnd\s+list|
        do\s+not\s+disturb
    )\b|
    # Tamil / Tanglish phrases
    (?:
        இனிமேல்\s*அழைக்காதீர்கள்|
        அழைக்க\s*வேண்டாம்|
        கால்\s*பண்ணாதீங்க|
        கால்\s*பன்னாதீங்க|
        போன்\s*பண்ணாதீங்க|
        போன்\s*பன்னாதீங்க|
        லிஸ்ட்ல\s*இருந்து\s*எடுங்க|
        என்\s*நம்பருக்கு\s*கால்\s*பண்ணாதீங்க|
        (?:ini|inime|inimey|enakku)?\s*(?:call|phone)\s*(?:panna\s*vendam|panadhinga|pannatheenga|pannathinga|seiyadheergal)|
        (?:call|phone)\s*seiyadheergal|
        call\s*pannatheenga|
        call\s*pannathinga|
        call\s*panadhinga
    )|
    # Hindi phrases
    (?:
        दोबारा\s*(?:कॉल|फोन)\s*मत\s*(?:करना|कीजिए)|
        (?:कॉल|फोन)\s*मत\s*(?:करो|करना|कीजिए)|
        नंबर\s*(?:हटा\s*दो|डिलीट\s*करो)|
        (?:dobara|phir\s*se)?\s*(?:call|phone)\s*mat\s*(?:karna|karo|kijiye)|
        mera\s*number\s*(?:list\s*se\s*)?hata\s*do|
        call\s*mat\s*(?:karo|karna|kijiye)
    )
    """,
)


def detect_dnd_intent(text: str) -> bool:
    """Detects whether caller utterance conveys a Do-Not-Call (DND) or stop-calling request."""
    if not text:
        return False
    clean = text.strip()
    return bool(DND_PHRASES_REGEX.search(clean))


async def is_number_suppressed(
    db: AsyncSession,
    phone: str,
    business_id: Optional[str] = None,
) -> bool:
    """Checks whether the normalized destination phone number is suppressed by DND/DNC.
    Concurrency-safe and multi-tenant scoped.
    """
    norm = canonical_phone_key(phone)
    if not norm:
        return False

    # 1. Check DoNotCallRecord in dnd_registry
    conds = [DoNotCallRecord.normalized_phone == norm, DoNotCallRecord.is_active == True]
    if business_id:
        conds.append(or_(DoNotCallRecord.business_id == business_id, DoNotCallRecord.business_id.is_(None)))

    dnd_stmt = select(DoNotCallRecord.id).where(and_(*conds)).limit(1)
    dnd_match = (await db.execute(dnd_stmt)).scalar_one_or_none()
    if dnd_match:
        logger.info(f"[DND_CHECK] Phone '{phone}' (normalized '{norm}') is SUPPRESSED in dnd_registry (record={dnd_match})")
        return True

    # 2. Check Lead table for marked DNC status
    lead_conds = [
        or_(
            Lead.status.in_(["DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED"]),
            Lead.call_result.ilike("%dnc%"),
            Lead.call_result.ilike("%do not call%"),
        )
    ]
    if business_id:
        lead_conds.append(Lead.business_id == business_id)

    # Check matching phone suffixes
    lead_phone_match = or_(
        Lead.phone == phone,
        Lead.phone.endswith(norm),
        Lead.phone.like(f"%{norm}"),
    )
    lead_conds.append(lead_phone_match)

    lead_stmt = select(Lead.id).where(and_(*lead_conds)).limit(1)
    lead_dnc = (await db.execute(lead_stmt)).scalar_one_or_none()
    if lead_dnc:
        logger.info(f"[DND_CHECK] Phone '{phone}' (normalized '{norm}') is SUPPRESSED via Lead {lead_dnc} (status=DNC)")
        return True

    return False


async def add_to_dnd(
    db: AsyncSession,
    phone: str,
    business_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    call_id: Optional[str] = None,
    reason: str = "Customer requested Do Not Call",
    source: str = "AI_VOICE_CALL",
) -> DoNotCallRecord:
    """Persists a Do-Not-Call (DND) record, normalizes the phone number, updates relevant
    lead and campaign lead records, and scopes it to the tenant organization.
    """
    norm = canonical_phone_key(phone)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1. Lookup or create DoNotCallRecord
    stmt = select(DoNotCallRecord).where(
        DoNotCallRecord.normalized_phone == norm,
        or_(DoNotCallRecord.business_id == business_id, DoNotCallRecord.business_id.is_(None)) if business_id else True
    ).limit(1)
    record = (await db.execute(stmt)).scalars().first()

    if record:
        record.is_active = True
        record.reason = reason
        record.source = source
        record.updated_at = now
        if business_id and not record.business_id:
            record.business_id = business_id
        if campaign_id:
            record.campaign_id = campaign_id
        if call_id:
            record.call_id = call_id
        if lead_id and not record.lead_id:
            record.lead_id = lead_id
        db.add(record)
    else:
        record = DoNotCallRecord(
            business_id=business_id,
            phone_number=phone,
            normalized_phone=norm,
            lead_id=lead_id,
            campaign_id=campaign_id,
            call_id=call_id,
            source=source,
            reason=reason,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(record)

    # 2. Update Lead record(s) matching phone
    if norm:
        lead_stmt = select(Lead).where(
            or_(
                Lead.id == lead_id,
                Lead.phone == phone,
                Lead.phone.endswith(norm),
            )
        )
        leads_res = (await db.execute(lead_stmt)).scalars().all()
        for ld in leads_res:
            ld.status = "DNC"
            ld.call_status = "DNC"
            ld.call_result = "Do Not Call (DNC)"
            db.add(ld)

            # Update CampaignLead entries for this lead
            cl_stmt = select(CampaignLead).where(CampaignLead.lead_id == ld.id)
            cls_res = (await db.execute(cl_stmt)).scalars().all()
            for cl in cls_res:
                cl.status = "DNC"
                cl.call_result = "Do Not Call (DNC)"
                db.add(cl)

    # 3. Update Call record if provided
    if call_id:
        c_obj = await db.get(Call, call_id)
        if c_obj:
            c_obj.call_result = "Do Not Call (DNC)"
            c_obj.hangup_reason = "CUSTOMER_DND_REQUEST"
            db.add(c_obj)

    await db.commit()
    await db.refresh(record)
    logger.info(
        f"[DND_SAVED] Phone '{phone}' (normalized '{norm}') added to DND registry id={record.id} "
        f"business_id={business_id} lead_id={lead_id} campaign_id={campaign_id} call_id={call_id}"
    )
    return record


async def remove_from_dnd(
    db: AsyncSession,
    phone: str,
    business_id: Optional[str] = None,
) -> bool:
    """Explicit authorized removal of a phone number from DND suppression."""
    norm = canonical_phone_key(phone)
    if not norm:
        return False

    conds = [DoNotCallRecord.normalized_phone == norm]
    if business_id:
        conds.append(or_(DoNotCallRecord.business_id == business_id, DoNotCallRecord.business_id.is_(None)))

    stmt = select(DoNotCallRecord).where(and_(*conds))
    records = (await db.execute(stmt)).scalars().all()
    if not records:
        return False

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for r in records:
        r.is_active = False
        r.updated_at = now
        db.add(r)

    await db.commit()
    logger.info(f"[DND_REMOVED] Phone '{phone}' (normalized '{norm}') deactivated from DND registry")
    return True
