from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_business_id, get_current_db_user
from app.db.database import get_db
from app.db.models import AIAgent, Lead, User, Business, Expert, Campaign, CampaignLead, LeadAssignment, LeadGroup, Call
from app.schemas.import_ import ImportSummary
from app.schemas.lead import LeadCreate, LeadOut, LeadUpdate
from app.services.import_service import parse_leads_file, normalize_phone_number

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.post("", response_model=LeadOut, status_code=201)
async def create_lead(
    payload: LeadCreate,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        business_id = user.business_id
        if not business_id:
            biz_row = (await db.execute(select(Business))).scalars().first()
            business_id = biz_row.id if biz_row else "c3dce06c-8330-4724-ba33-b5282b3c3596"
            user.business_id = business_id
            
        created_by = "ADMIN" if (user.role or "").upper() in ("ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER") else "USER"
            
        group_id = None
        if (user.role or "").upper() not in ("ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER"):
            try:
                group_stmt = select(LeadGroup).where(LeadGroup.user_id == user.id, LeadGroup.filename == "Manual Leads")
                group = (await db.execute(group_stmt)).scalar_one_or_none()
                if not group:
                    group = LeadGroup(
                        business_id=business_id,
                        user_id=user.id,
                        filename="Manual Leads",
                        lead_count=0
                    )
                    db.add(group)
                    await db.flush()
                group_id = group.id
            except Exception as grp_err:
                from loguru import logger
                logger.warning(f"Note creating manual group: {grp_err}")
                group_id = None

        lead = Lead(business_id=business_id, created_by_role=created_by, group_id=group_id, **payload.model_dump())
        db.add(lead)
        
        if group_id:
            try:
                group = await db.get(LeadGroup, group_id)
                if group:
                    group.lead_count = (group.lead_count or 0) + 1
                    db.add(group)
            except Exception:
                pass

        await db.commit()
        await db.refresh(lead)
        return _lead_to_dict(lead)
    except Exception as exc:
        await db.rollback()
        from loguru import logger
        logger.error(f"Error in create_lead: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to create lead: {str(exc)}")


def _lead_to_dict(lead: Lead, filename: str | None = None, agent_name: str | None = None) -> dict:
    resolved_agent = agent_name or lead.agent_name or lead.assigned_agent_name or ""
    return {
        "id": lead.id,
        "business_id": lead.business_id or "",
        "group_id": lead.group_id,
        "source": lead.source or "MANUAL",
        "created_by_role": lead.created_by_role or "USER",
        "first_name": lead.first_name or "",
        "last_name": lead.last_name or "",
        "email": lead.email or "",
        "phone": lead.phone or "",
        "company": lead.company or "",
        "job_title": lead.job_title or "",
        "requirement": lead.requirement or "",
        "budget": lead.budget or "",
        "timeline": lead.timeline or "",
        "location": lead.location or "",
        "status": lead.status or "NEW",
        "score": lead.score or 0,
        "qualification_status": lead.qualification_status or "UNQUALIFIED",
        "ai_summary": lead.ai_summary or "",
        "call_status": lead.call_status,
        "call_date_time": lead.call_date_time,
        "call_duration": lead.call_duration or 0,
        "twilio_call_sid": lead.twilio_call_sid,
        "call_result": lead.call_result,
        "assigned_expert_id": lead.assigned_expert_id,
        "agent_id": lead.agent_id,
        "assigned_agent_name": resolved_agent,
        "agent_name": resolved_agent,
        "original_filename": filename if filename else ("leads.csv" if lead.source == "EXCEL_IMPORT" else None),
        "created_at": lead.created_at,
        "updated_at": lead.updated_at,
    }


@router.get("", response_model=list[LeadOut])
async def list_leads(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=500, le=1000),
    offset: int = Query(default=0, ge=0),
):
    try:
        user_role = (user.role or "USER").upper()
        target_bid = user.business_id
        if user_role == "SUPER_ADMIN":
            stmt = select(Lead, LeadGroup.filename, Expert.name).outerjoin(
                LeadGroup, Lead.group_id == LeadGroup.id
            ).outerjoin(
                Expert, Lead.assigned_expert_id == Expert.id
            )
        elif not target_bid:
            return []
        elif user_role in ("ADMIN", "SUPERVISOR", "BUSINESS_OWNER", "MEMBER", "USER"):
            stmt = select(Lead, LeadGroup.filename, Expert.name).outerjoin(
                LeadGroup, Lead.group_id == LeadGroup.id
            ).outerjoin(
                Expert, Lead.assigned_expert_id == Expert.id
            ).where(
                Lead.business_id == target_bid
            )
        else:
            # For VOICE_AGENT or AGENT: fetch ONLY leads directly assigned or transferred to this specific agent
            expert = await db.scalar(select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email)))
            assigned_lead_ids = []
            if expert:
                # 1. Direct assigned leads (transferred or individually assigned)
                ids1 = (await db.execute(select(Lead.id).where(Lead.assigned_expert_id == expert.id))).scalars().all()

                # 2. Specifically transferred/assigned campaign leads
                ids2 = (await db.execute(
                    select(CampaignLead.lead_id).where(CampaignLead.assigned_expert_id == expert.id)
                )).scalars().all()

                # 3. Transferred lead assignments
                ids3 = (await db.execute(select(LeadAssignment.lead_id).where(LeadAssignment.expert_id == expert.id))).scalars().all()

                assigned_lead_ids = list(set(list(ids1) + list(ids2) + list(ids3)))

            filters = [Lead.business_id == target_bid]
            if assigned_lead_ids:
                filters.append(or_(Lead.id.in_(assigned_lead_ids), LeadGroup.user_id == user.id))
            else:
                filters.append(LeadGroup.user_id == user.id)

            stmt = select(Lead, LeadGroup.filename, Expert.name).outerjoin(
                LeadGroup, Lead.group_id == LeadGroup.id
            ).outerjoin(
                Expert, Lead.assigned_expert_id == Expert.id
            ).where(
                and_(*filters)
            )

        if isinstance(status, str) and status:
            stmt = stmt.where(Lead.status == status)
        if isinstance(search, str) and search:
            like = f"%{search}%"
            stmt = stmt.where(
                (Lead.first_name.ilike(like))
                | (Lead.last_name.ilike(like))
                | (Lead.email.ilike(like))
                | (Lead.company.ilike(like))
                | (LeadGroup.filename.ilike(like))
            )
        safe_offset = offset if isinstance(offset, int) else (getattr(offset, "default", 0) or 0)
        safe_limit = limit if isinstance(limit, int) else (getattr(limit, "default", 500) or 500)
        stmt = stmt.order_by(Lead.created_at.desc()).offset(safe_offset).limit(safe_limit)
        results = (await db.execute(stmt)).all()
        
        output_leads = []
        lead_ids_without_expert = [row[0].id for row in results if len(row) > 2 and not row[2] and row[0].id]
        call_agent_map = {}
        if lead_ids_without_expert:
            from app.db.models import Call
            call_stmt = (
                select(Call.lead_id, User.full_name)
                .join(User, Call.agent_id == User.id)
                .where(Call.lead_id.in_(lead_ids_without_expert))
                .order_by(Call.ended_at.desc())
            )
            call_rows = (await db.execute(call_stmt)).all()
            for l_id, u_name in call_rows:
                if l_id not in call_agent_map and u_name:
                    call_agent_map[l_id] = u_name

        for row in results:
            lead_obj = row[0]
            filename = row[1]
            expert_name = row[2] if len(row) > 2 else None
            resolved_agent = expert_name or call_agent_map.get(lead_obj.id) or lead_obj.agent_name or ""
            output_leads.append(_lead_to_dict(lead_obj, filename, resolved_agent))

        return output_leads
    except Exception as exc:
        from loguru import logger
        logger.exception(f"Error fetching leads: {exc}")
        return []


@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(
    lead_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(Lead, LeadGroup.filename, Expert.name)
        .outerjoin(LeadGroup, Lead.group_id == LeadGroup.id)
        .outerjoin(Expert, Lead.assigned_expert_id == Expert.id)
        .where(Lead.id == lead_id)
    )).first()

    if not row:
        # Check if lead_id was actually a Call ID or call session
        call = await db.get(Call, lead_id)
        if not call:
            stmt_call = select(Call).where(Call.id == lead_id)
            call = (await db.execute(stmt_call)).scalars().first()
        if call and call.lead_id:
            row = (await db.execute(
                select(Lead, LeadGroup.filename, Expert.name)
                .outerjoin(LeadGroup, Lead.group_id == LeadGroup.id)
                .outerjoin(Expert, Lead.assigned_expert_id == Expert.id)
                .where(Lead.id == call.lead_id)
            )).first()
        elif call:
            c_name = call.client_name or "Prospect"
            first_n = c_name.split()[0] if c_name else "Prospect"
            last_n = " ".join(c_name.split()[1:]) if len(c_name.split()) > 1 else ""
            return {
                "id": call.id,
                "first_name": first_n,
                "last_name": last_n,
                "name": c_name,
                "phone": call.client_phone or call.to_number or "",
                "email": "",
                "company": "",
                "status": "CALLED",
                "score": 0,
                "qualification_status": "UNQUALIFIED",
                "ai_summary": "",
                "call_status": call.status,
                "call_date_time": call.created_at,
                "call_duration": call.duration_seconds or 0,
                "twilio_call_sid": getattr(call, "call_sid", None),
                "call_result": call.disposition,
                "assigned_expert_id": call.agent_id,
                "agent_id": call.agent_id,
                "assigned_agent_name": "",
                "agent_name": "",
                "original_filename": None,
                "created_at": call.created_at,
                "updated_at": call.updated_at,
            }

    if not row:
        raise HTTPException(status_code=404, detail="Lead not found")
    lead_obj = row[0]
    filename = row[1]
    expert_name = row[2] if len(row) > 2 else None
    user_role = (user.role or "USER").upper()
    if user_role != "SUPER_ADMIN":
        if not user.business_id or (lead_obj.business_id and lead_obj.business_id != user.business_id):
            raise HTTPException(status_code=404, detail="Lead not found")
    return _lead_to_dict(lead_obj, filename, expert_name)


@router.patch("/{lead_id}", response_model=LeadOut)
async def update_lead(
    lead_id: str,
    payload: LeadUpdate,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Lead:
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if (user.role or "").upper() != "SUPER_ADMIN" and lead.business_id != user.business_id:
        raise HTTPException(status_code=404, detail="Lead not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(lead, field, value)
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    if lead.group_id:
        from app.api.routes.lead_groups import sync_excel_file
        await sync_excel_file(db, lead.group_id)
    return lead


@router.delete("/{lead_id}", status_code=204)
async def delete_lead(
    lead_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if (user.role or "").upper() != "SUPER_ADMIN" and lead.business_id != user.business_id:
        raise HTTPException(status_code=404, detail="Lead not found")
    group_id = lead.group_id
    await db.delete(lead)
    await db.commit()
    if group_id:
        try:
            from app.api.routes.lead_groups import sync_excel_file
            await sync_excel_file(db, group_id)
        except Exception:
            pass


class BulkDeleteLeadsRequest(BaseModel):
    lead_ids: list[str]


@router.post("/bulk-delete", status_code=204)
async def bulk_delete_leads(
    payload: BulkDeleteLeadsRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not payload.lead_ids:
        return
    stmt = select(Lead).where(Lead.id.in_(payload.lead_ids))
    if (user.role or "").upper() != "SUPER_ADMIN":
        stmt = stmt.where(Lead.business_id == user.business_id)
    res = await db.execute(stmt)
    leads_to_delete = res.scalars().all()
    if not leads_to_delete:
        return

    group_ids = set()
    for lead in leads_to_delete:
        if lead.group_id:
            group_ids.add(lead.group_id)
        await db.delete(lead)
    await db.commit()

    if group_ids:
        from app.api.routes.lead_groups import sync_excel_file
        for gid in group_ids:
            try:
                await sync_excel_file(db, gid)
            except Exception:
                pass


def extract_lead_fields_from_dict(row: dict) -> dict:
    """Smartly resolves standard lead fields from any CSV/dictionary row with flexible header aliases."""
    def _find_val(aliases: list[str]) -> str:
        for k, v in row.items():
            if not k or v is None:
                continue
            clean_k = str(k).strip().lower().replace("_", "").replace(" ", "").replace("-", "")
            for alias in aliases:
                clean_alias = alias.strip().lower().replace("_", "").replace(" ", "").replace("-", "")
                if clean_k == clean_alias:
                    return str(v).strip()
        return ""

    first_name = _find_val(["first_name", "first name", "firstname", "given_name", "given name", "fname", "first"])
    last_name = _find_val(["last_name", "last name", "lastname", "surname", "lname", "last"])
    full_name = _find_val(["full_name", "full name", "fullname", "lead_name", "lead name", "leadname", "customer_name", "customer name", "customername", "client_name", "client name", "clientname", "name", "contact_name", "contact name", "customer", "lead"])

    phone = _find_val(["phone", "phone_number", "phone number", "phonenumber", "mobile", "mobile_number", "mobile number", "mobilenumber", "contact", "contact_number", "contact number", "cell", "telephone", "whatsapp", "number", "phone_no", "phone no", "mobile_no", "mobile no", "contact_no", "contact no", "cust_phone", "customer_phone", "num"])
    email = _find_val(["email", "email_address", "email address", "emailaddress", "e-mail", "mail", "emailid", "email_id"])
    company = _find_val(["company", "company_name", "company name", "companyname", "organization", "org", "business", "business_name", "business name", "client_company"])
    job_title = _find_val(["job_title", "job title", "jobtitle", "title", "designation", "role", "position"])
    requirement = _find_val(["requirement", "requirements", "notes", "remarks", "remark", "interest", "product_interest", "product interest", "inquiry", "message", "description"])
    budget = _find_val(["budget", "monthly_budget", "monthly budget", "amount", "price"])
    timeline = _find_val(["timeline", "timeframe", "schedule"])
    location = _find_val(["location", "city", "place", "town", "address", "state", "country", "pincode", "zipcode"])

    if not first_name and not last_name and full_name:
        parts = full_name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
    elif first_name and not last_name:
        parts = first_name.split(" ", 1)
        if len(parts) > 1:
            first_name = parts[0]
            last_name = parts[1]

    if first_name and last_name and first_name.lower().endswith(last_name.lower()):
        first_name = first_name[:len(first_name) - len(last_name)].strip()

    return {
        "first_name": first_name,
        "last_name": last_name,
        "phone": normalize_phone_number(phone),
        "email": email,
        "company": company,
        "job_title": job_title,
        "requirement": requirement,
        "budget": budget,
        "timeline": timeline,
        "location": location,
    }


@router.post("/import", response_model=ImportSummary, status_code=201)
async def import_leads(
    file: UploadFile = File(...),
    agent_id: str | None = Form(default=None),
    auto_call: bool = Form(default=True),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
    background_tasks: BackgroundTasks = None,
) -> ImportSummary:
    """
    Upload an Excel/CSV sheet of leads. Every valid row becomes a
    real Lead record (source=EXCEL_IMPORT), attached to a LeadGroup with the
    original filename. Supports deduplication against the database.
    """
    business_id = user.business_id
    if not business_id and (user.role or "").upper() != "SUPER_ADMIN":
        raise HTTPException(status_code=400, detail="User does not have an active organization assigned.")

    upload_filename = file.filename or "leads.xlsx"
    lower_filename = upload_filename.lower()

    # 1. File Extension Validation
    if not (lower_filename.endswith(".csv") or lower_filename.endswith(".xlsx") or lower_filename.endswith(".xls")):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Please upload a valid CSV or Excel file (.csv, .xlsx, .xls)."
        )

    content = await file.read()
    if not content or len(content) == 0:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    # 2. File Parsing
    try:
        rows = parse_leads_file(content, upload_filename)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}") from exc

    if not rows:
        raise HTTPException(status_code=400, detail="No readable lead rows found in the uploaded file.")

    try:
        # 3. Agent Lookup
        agent = None
        if agent_id:
            agent = await db.get(AIAgent, agent_id)
            if not agent or (agent.business_id and agent.business_id != business_id):
                agent = None
        if not agent:
            agent = await db.scalar(select(AIAgent).where(AIAgent.business_id == business_id, AIAgent.status == "ACTIVE"))

        # 4. Fetch Existing DB Contacts for Deduplication
        existing_phones = set()
        existing_emails = set()
        try:
            existing_phones_res = await db.scalars(select(Lead.phone).where(Lead.business_id == business_id, Lead.phone != ""))
            existing_emails_res = await db.scalars(select(Lead.email).where(Lead.business_id == business_id, Lead.email != ""))
            existing_phones = set(p for p in existing_phones_res.all() if p)
            existing_emails = set(e.lower() for e in existing_emails_res.all() if e)
        except Exception:
            pass

        # 5. Create LeadGroup for the batch
        group = LeadGroup(
            business_id=business_id,
            user_id=user.id,
            filename=upload_filename,
            lead_count=0,
            status="COMPLETED"
        )
        db.add(group)
        await db.flush()

        errors: list[str] = []
        created_leads: list[Lead] = []
        seen_batch_contacts = set()
        invalid_rows = 0
        duplicate_rows = 0

        for i, row in enumerate(rows):
            row_num = row.get("_row_number", i + 2)
            ext = extract_lead_fields_from_dict(row)
            first_name = ext["first_name"] or str(row.get("first_name") or "").strip()
            last_name = ext["last_name"] or str(row.get("last_name") or "").strip()
            email = (ext["email"] or str(row.get("email") or "").strip()).lower()

            raw_phone = ext["phone"] or row.get("phone", "") or row.get("mobile_number", "")
            phone = normalize_phone_number(raw_phone) if raw_phone else ""

            if not (phone or email or first_name or last_name):
                invalid_rows += 1
                errors.append(f"Row {row_num}: Skipped — Missing First Name and Contact info.")
                continue

            if not first_name:
                first_name = "Unnamed Lead"

            if raw_phone and not phone:
                phone = str(raw_phone).strip()

            is_batch_dup = (phone and phone in seen_batch_contacts) or (email and email in seen_batch_contacts)
            if is_batch_dup:
                duplicate_rows += 1
                dup_identifier = phone if (phone and phone in seen_batch_contacts) else email
                errors.append(f"Row {row_num}: Duplicate lead in file skipped ({dup_identifier}).")
                continue

            if phone:
                seen_batch_contacts.add(phone)
            if email:
                seen_batch_contacts.add(email)

            req_parts = []
            if ext["requirement"]: req_parts.append(ext["requirement"])
            if row.get("alternate_number"): req_parts.append(f"Alternate: {row.get('alternate_number')}")
            if row.get("address"): req_parts.append(f"Address: {row.get('address')}")
            if row.get("remarks"): req_parts.append(f"{row.get('remarks')}")
            if row.get("requirement") and row.get("requirement") not in req_parts: req_parts.append(f"{row.get('requirement')}")

            requirement = " | ".join(req_parts).strip(" |") or "Interested in campaign offering"
            place = str(row.get("place") or "").strip() or str(row.get("location") or "").strip()
            state = str(row.get("state") or "").strip()
            pincode = str(row.get("pincode") or "").strip()
            location = ext["location"] or ", ".join(filter(None, [place, state, pincode])) or "Unknown"

            try:
                lead = Lead(
                    business_id=business_id,
                    group_id=group.id,
                    source="EXCEL_IMPORT",
                    created_by_role="ADMIN" if (user.role or "").upper() in ("ADMIN", "SUPER_ADMIN") else "USER",
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    phone=phone,
                    company=ext["company"] or row.get("company", "").strip(),
                    job_title=ext["job_title"] or row.get("job_title", "").strip(),
                    requirement=requirement,
                    budget=ext["budget"] or row.get("budget", "").strip(),
                    timeline=ext["timeline"] or row.get("timeline", "").strip(),
                    location=location,
                    status=row.get("lead_status", "Ready") or "Ready",
                    agent_id=agent.id if agent else None,
                    call_status="NEW",
                    call_result="—",
                )
                db.add(lead)
                created_leads.append(lead)
            except Exception as err:
                invalid_rows += 1
                errors.append(f"Row {row_num}: Error creating lead — {err}")

        group.lead_count = len(created_leads)
        await db.commit()

        total_leads_stmt = select(func.count(Lead.id)).where(Lead.business_id == business_id)
        final_count = (await db.execute(total_leads_stmt)).scalar() or len(created_leads)

        return ImportSummary(
            total_rows=len(rows),
            successfully_imported=len(created_leads),
            duplicate_rows=duplicate_rows,
            invalid_rows=invalid_rows,
            final_lead_count=final_count,
            leads_created=len(created_leads),
            calls_queued=0,
            skipped_rows=invalid_rows + duplicate_rows,
            lead_group_id=group.id if group else None,
            errors=errors,
        )
    except Exception as exc:
        await db.rollback()
        from loguru import logger
        logger.error(f"Error in import_leads: {exc}")
        raise HTTPException(status_code=500, detail=f"Lead import failed: {str(exc)}")


from pydantic import BaseModel

class MappedImportPayload(BaseModel):
    leads: list[dict]
    agent_id: str | None = None
    campaign_id: str | None = None
    filename: str | None = None


@router.post("/preview")
async def preview_leads(
    file: UploadFile = File(...),
) -> dict:
    """Parses uploaded Excel/CSV and returns columns and first 10 rows for mapping UI."""
    content = await file.read()
    filename = file.filename or "leads.xlsx"
    lower = filename.lower()
    
    import io
    import pandas as pd
    try:
        if lower.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str).fillna("")
        else:
            df = pd.read_excel(io.BytesIO(content), dtype=str).fillna("")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}")
        
    headers = [str(col) for col in df.columns]
    preview_rows = []
    for _, row in df.iterrows():
        preview_rows.append({str(k): str(v) for k, v in row.to_dict().items()})
        
    return {
        "headers": headers,
        "rows": preview_rows
    }


@router.post("/import-mapped", response_model=ImportSummary, status_code=201)
async def import_mapped_leads(
    payload: MappedImportPayload,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> ImportSummary:
    """Imports manually mapped or auto-extracted rows directly into PostgreSQL."""
    agent = None
    if payload.agent_id:
        agent = await db.get(AIAgent, payload.agent_id)
        if not agent or agent.business_id != business_id:
            raise HTTPException(status_code=404, detail="Agent not found")
    else:
        agent = await db.scalar(select(AIAgent).where(AIAgent.business_id == business_id, AIAgent.status == "ACTIVE"))

    created_leads = []
    errors = []
    
    group_id = None
    if payload.filename:
        lead_group = LeadGroup(
            business_id=business_id,
            filename=payload.filename,
            lead_count=0,
            status="COMPLETED",
        )
        db.add(lead_group)
        await db.flush()
        group_id = lead_group.id

    for i, row in enumerate(payload.leads):
        ext = extract_lead_fields_from_dict(row)
        first_name = ext["first_name"]
        last_name = ext["last_name"]
        email = ext["email"]
        phone = ext["phone"]

        if not (phone or email or first_name or last_name):
            errors.append(f"Row {i + 1}: skipped — no contact or name info")
            continue
            
        lead = Lead(
            business_id=business_id,
            group_id=group_id,
            source="EXCEL_IMPORT",
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            company=ext["company"],
            job_title=ext["job_title"],
            requirement=ext["requirement"] or "Interested in campaign offering",
            budget=ext["budget"],
            timeline=ext["timeline"],
            location=ext["location"],
            agent_id=agent.id if agent else None,
        )
        db.add(lead)
        created_leads.append(lead)

    campaign = None
    if payload.campaign_id:
        campaign = await db.get(Campaign, payload.campaign_id)
        if campaign and group_id:
            campaign.lead_group_id = group_id
            db.add(campaign)

    if group_id:
        group_obj = await db.get(LeadGroup, group_id)
        if group_obj:
            group_obj.lead_count = len(created_leads)

    for lead in created_leads:
        await db.flush()
        if campaign:
            cl = CampaignLead(
                campaign_id=campaign.id,
                lead_id=lead.id,
                assigned_expert_id=campaign.voice_agent_id,
                status="PENDING"
            )
            db.add(cl)
        
    await db.commit()
    for lead in created_leads:
        await db.refresh(lead)
        
    return ImportSummary(
        total_rows=len(payload.leads),
        leads_created=len(created_leads),
        calls_queued=0,
        skipped_rows=len(payload.leads) - len(created_leads),
        lead_group_id=group_id,
        errors=errors,
    )

