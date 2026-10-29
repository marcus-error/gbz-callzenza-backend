from fastapi import APIRouter, Depends, HTTPException, Response, status, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
import pandas as pd
from pathlib import Path

from app.core.deps import get_current_business_id, get_current_db_user
from app.core.security import require_role
from app.db.database import get_db
from app.db.models import (
    User,
    LeadGroup,
    Lead,
    AIAgent,
    Conversation,
    Message,
    Call,
    CallMessage,
    Transcript,
    Appointment,
    Deal,
    CampaignLead,
    LeadAssignment,
    Campaign,
    Client,
)
from app.schemas.lead_group import LeadGroupCreate, LeadGroupOut
from app.schemas.lead import LeadOut
from app.services.import_service import normalize_phone_number, parse_leads_file

router = APIRouter(prefix="/api/lead-groups", tags=["lead-groups"])

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


async def sync_excel_file(db: AsyncSession, group_id: str):
    """Generates and updates the Excel file on the server for the specified lead group."""
    group = await db.get(LeadGroup, group_id)
    if not group:
        return
        
    stmt = select(Lead).where(Lead.group_id == group_id).order_by(Lead.created_at.asc())
    result = await db.scalars(stmt)
    leads = list(result.all())
    
    rows = []
    for lead in leads:
        rows.append({
            "First Name": lead.first_name,
            "Last Name": lead.last_name,
            "Email": lead.email,
            "Phone": lead.phone,
            "Company": lead.company,
            "Requirement": lead.requirement,
            "Budget": lead.budget,
            "Location": lead.location,
            "Call Status": lead.call_status,
            "Call Date & Time": lead.call_date_time.isoformat() if lead.call_date_time else "",
            "Call Duration": lead.call_duration if lead.call_duration is not None else "",
            "Twilio Call SID": lead.twilio_call_sid or "",
            "Call Result": lead.call_result or "",
        })
        
    try:
        df = pd.DataFrame(rows)
        file_path = UPLOAD_DIR / f"{group_id}.xlsx"
        df.to_excel(file_path, index=False)
    except Exception as e:
        from loguru import logger
        logger.warning(f"Could not save local excel file for group {group_id}: {e}")


@router.get("", response_model=list[LeadGroupOut])
async def list_groups(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns a list of all persistent lead groups for the active business."""
    try:
        user_role = (user.role or "USER").upper()
        if user_role == "SUPER_ADMIN":
            stmt = select(LeadGroup).order_by(LeadGroup.created_at.desc())
        elif user.business_id:
            stmt = select(LeadGroup).where(
                LeadGroup.business_id == user.business_id
            ).order_by(LeadGroup.created_at.desc())
        else:
            return []
        result = await db.scalars(stmt)
        return list(result.all())
    except Exception as exc:
        from loguru import logger
        logger.exception(f"Error listing lead groups: {exc}")
        return []


@router.post("", response_model=LeadGroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: LeadGroupCreate,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> LeadGroup:
    """Creates a LeadGroup and saves all associated leads with group_id relationship."""
    business_id = user.business_id or ""
    agent = None
    if payload.agent_id:
        agent = await db.get(AIAgent, payload.agent_id)
        if not agent or agent.business_id != business_id:
            raise HTTPException(status_code=404, detail="Agent not found")
    else:
        agent = await db.scalar(select(AIAgent).where(AIAgent.business_id == business_id, AIAgent.status == "ACTIVE"))

    # Create LeadGroup
    group = LeadGroup(
        business_id=business_id,
        user_id=user.id,
        filename=payload.filename,
        lead_count=len(payload.leads),
        status="IDLE"
    )
    db.add(group)
    await db.flush()  # Generate group.id

    # Create leads with group_id
    for row in payload.leads:
        first_name = row.get("first_name", "").strip()
        last_name = row.get("last_name", "").strip()
        email = row.get("email", "").strip()
        phone = normalize_phone_number(row.get("phone", ""))
        company = row.get("company", "").strip()
        requirement = row.get("requirement", "").strip()
        budget = row.get("budget", "").strip()
        location = row.get("location", "").strip()

        if not (phone or email):
            continue

        lead = Lead(
            business_id=business_id,
            group_id=group.id,
            source="EXCEL_IMPORT",
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            company=company,
            requirement=requirement,
            budget=budget,
            location=location,
            agent_id=agent.id if agent else None,
            call_status="NEW",
            call_result="—",
        )
        db.add(lead)

    await db.commit()
    await db.refresh(group)
    await sync_excel_file(db, group.id)
    return group


@router.get("/{id}/leads", response_model=list[LeadOut])
async def get_group_leads(
    id: str,
    user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> list[Lead]:
    """Returns all leads belonging to a specific lead group."""
    group = await db.get(LeadGroup, id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    user_role = (user.role or "").strip().upper()
    if user_role != "SUPER_ADMIN":
        if group.business_id and group.business_id != business_id:
            raise HTTPException(status_code=403, detail="Not authorized to access this group")
        if user_role not in ("ADMIN", "BUSINESS_OWNER", "SUPERVISOR") and group.user_id != user.id:
            raise HTTPException(status_code=403, detail="Not authorized to access this group")

    stmt = select(Lead).where(Lead.group_id == id).order_by(Lead.created_at.asc())
    result = await db.scalars(stmt)
    leads_list = list(result.all())

    # Auto-resolve call outcome from recent calls if not set on lead
    for l in leads_list:
        if not l.call_result or l.call_result == "—":
            c_stmt = select(Call).where(Call.lead_id == l.id).order_by(Call.created_at.desc())
            latest_call = (await db.execute(c_stmt)).scalars().first()
            if latest_call:
                if latest_call.call_result and latest_call.call_result != "—":
                    l.call_result = latest_call.call_result
                elif latest_call.status:
                    l.call_result = latest_call.status
                if latest_call.status:
                    l.call_status = latest_call.status

    return leads_list


@router.get("/{id}/download")
async def download_group_file(
    id: str,
    user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """Serves the latest synchronized Excel spreadsheet for download."""
    group = await db.get(LeadGroup, id)
    if not group:
        raise HTTPException(status_code=404, detail="Lead group not found")

    user_role = (user.role or "").strip().upper()
    if user_role != "SUPER_ADMIN":
        if group.business_id and group.business_id != business_id:
            raise HTTPException(status_code=403, detail="Not authorized to download this group")
        if user_role not in ("ADMIN", "BUSINESS_OWNER", "SUPERVISOR") and group.user_id != user.id:
            raise HTTPException(status_code=403, detail="Not authorized to download this group")
        
    file_path = UPLOAD_DIR / f"{id}.xlsx"
    if not file_path.exists():
        await sync_excel_file(db, id)
        
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Excel file not found on server")
        
    return FileResponse(
        path=str(file_path),
        filename=group.filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_group(
    id: str,
    current_user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Deletes a lead group and cascades deletion to all associated leads. Admin only."""
    group = await db.get(LeadGroup, id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    user_role = (current_user.role or "").strip().upper()
    if user_role not in ("SUPER_ADMIN", "BUSINESS_OWNER", "ADMIN", "SUPERVISOR"):
        if group.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to delete this group")

    # In a multi-tenant setup, verify that the admin belongs to the same business (unless SUPER_ADMIN)
    if user_role != "SUPER_ADMIN":
        if group.business_id and business_id and group.business_id != business_id:
            raise HTTPException(status_code=403, detail="Not authorized to delete this group")

    # Nullify references in Campaigns and Clients to prevent foreign key issues
    await db.execute(update(Campaign).where(Campaign.lead_group_id == id).values(lead_group_id=None))
    await db.execute(update(Client).where(Client.group_id == id).values(group_id=None))

    # Delete physical excel file
    file_path = UPLOAD_DIR / f"{id}.xlsx"
    try:
        if file_path.exists():
            file_path.unlink()
    except Exception:
        pass

    # Get all lead IDs in this group to clean up associated records
    lead_ids_stmt = select(Lead.id).where(Lead.group_id == id)
    lead_ids = list((await db.scalars(lead_ids_stmt)).all())

    if lead_ids:
        # 1. Delete CampaignLeads
        await db.execute(delete(CampaignLead).where(CampaignLead.lead_id.in_(lead_ids)))

        # 2. Delete Calls (CallMessage/Transcript reference Call)
        call_ids_stmt = select(Call.id).where(Call.lead_id.in_(lead_ids))
        call_ids = list((await db.scalars(call_ids_stmt)).all())
        if call_ids:
            await db.execute(delete(CallMessage).where(CallMessage.call_id.in_(call_ids)))
            await db.execute(delete(Transcript).where(Transcript.call_id.in_(call_ids)))
            await db.execute(delete(Call).where(Call.id.in_(call_ids)))

        # 3. Delete Conversations (Message references Conversation)
        conv_ids_stmt = select(Conversation.id).where(Conversation.lead_id.in_(lead_ids))
        conv_ids = list((await db.scalars(conv_ids_stmt)).all())
        if conv_ids:
            await db.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
            await db.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))

        # 4. Delete other lead references
        await db.execute(delete(Appointment).where(Appointment.lead_id.in_(lead_ids)))
        await db.execute(delete(Deal).where(Deal.lead_id.in_(lead_ids)))
        await db.execute(delete(LeadAssignment).where(LeadAssignment.lead_id.in_(lead_ids)))

        # 5. Finally delete the leads
        await db.execute(delete(Lead).where(Lead.id.in_(lead_ids)))

    await db.delete(group)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class LeadGroupUpdate(BaseModel):
    filename: str


@router.patch("/{id}", response_model=LeadGroupOut)
async def update_group(
    id: str,
    payload: LeadGroupUpdate,
    user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> LeadGroup:
    group = await db.get(LeadGroup, id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    
    user_role = (user.role or "").strip().upper()
    if user_role != "SUPER_ADMIN":
        if (
            group.business_id
            and business_id
            and group.business_id != business_id
            and group.user_id != user.id
            and group.business_id != ""
        ):
            raise HTTPException(status_code=403, detail="Not authorized to update this group")
    
    group.filename = payload.filename
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


@router.post("/import", response_model=LeadGroupOut, status_code=201)
async def import_group(
    file: UploadFile = File(...),
    agent_id: str | None = Form(default=None),
    user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> LeadGroup:
    content = await file.read()
    try:
        rows = parse_leads_file(content, file.filename or "leads.xlsx")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}") from exc

    if agent_id:
        agent = await db.get(AIAgent, agent_id)
        if not agent or agent.business_id != business_id:
            raise HTTPException(status_code=404, detail="Agent not found")
    else:
        agent = await db.scalar(select(AIAgent).where(AIAgent.business_id == business_id, AIAgent.status == "ACTIVE"))

    business_id = business_id or user.business_id or "c3dce06c-8330-4724-ba33-b5282b3c3596"
    try:
        group = LeadGroup(
            business_id=business_id,
            user_id=user.id,
            filename=file.filename or "leads.xlsx",
            lead_count=0,
            status="IDLE",
        )
        db.add(group)
        await db.flush()

        valid_leads_count = 0
        seen_contacts = set()
        for i, row in enumerate(rows):
            first_name = row.get("first_name", "").strip()
            last_name = row.get("last_name", "").strip()
            email = row.get("email", "").strip().lower()
            phone = normalize_phone_number(row.get("phone", ""))
            company = row.get("company", "").strip()
            requirement = row.get("requirement", "").strip()
            budget = row.get("budget", "").strip()
            location = row.get("location", "").strip()

            if not (first_name or phone or email):
                continue

            contact_key = (phone, email) if (phone or email) else (first_name,)
            if contact_key in seen_contacts:
                continue
            seen_contacts.add(contact_key)

            if not first_name:
                first_name = phone or email or f"Lead {i + 1}"

            lead = Lead(
                business_id=business_id,
                group_id=group.id,
                source="EXCEL_IMPORT",
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                company=company,
                requirement=requirement,
                budget=budget,
                location=location,
                status="Ready",
                agent_id=agent.id if agent else None,
                call_status="NEW",
                call_result="—",
            )
            db.add(lead)
            valid_leads_count += 1

        group.lead_count = valid_leads_count
        await db.commit()
        await db.refresh(group)
        await sync_excel_file(db, group.id)
        return group
    except Exception as exc:
        await db.rollback()
        from loguru import logger
        logger.error(f"Error in import_group: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to import lead group: {str(exc)}")

