from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_business_id
from app.db.database import get_db
from app.db.models import Appointment
from app.schemas.appointment import AppointmentCreate, AppointmentOut

router = APIRouter(prefix="/api/appointments", tags=["appointments"])


@router.post("", response_model=AppointmentOut, status_code=201)
async def create_appointment(
    payload: AppointmentCreate,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Appointment:
    """Module 19/50 — books against the mock/real Google Calendar provider,
    then persists locally. See services/calendar_service.py."""
    from app.services.calendar_service import book_slot

    await book_slot(payload.scheduled_at, payload.notes)

    appt = Appointment(business_id=business_id, **payload.model_dump())
    db.add(appt)
    await db.commit()
    await db.refresh(appt)
    return appt


@router.get("", response_model=list[AppointmentOut])
async def list_appointments(
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> list[Appointment]:
    result = await db.scalars(
        select(Appointment).where(Appointment.business_id == business_id).order_by(Appointment.scheduled_at.asc())
    )
    return list(result)


@router.patch("/{appointment_id}/status", response_model=AppointmentOut)
async def update_status(
    appointment_id: str,
    status: str,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Appointment:
    appt = await db.get(Appointment, appointment_id)
    if not appt or appt.business_id != business_id:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appt.status = status
    db.add(appt)
    await db.commit()
    await db.refresh(appt)
    return appt
