import logging
from typing import List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user, get_current_business_id
from app.db.database import get_db
from app.db.models import User, Client, LeadGroup

router = APIRouter(prefix="/api/clients", tags=["clients"])
logger = logging.getLogger(__name__)

# Pydantic Schemas

class ClientBase(BaseModel):
    name: str
    company_name: Optional[str] = ""
    contact_number: Optional[str] = ""
    email: Optional[str] = ""
    address: Optional[str] = ""
    status: Optional[str] = "Active"
    assigned_agent_id: Optional[str] = None

class ClientCreate(ClientBase):
    group_id: Optional[str] = None
    original_filename: Optional[str] = None

class ClientUpdate(ClientBase):
    name: Optional[str] = None
    group_id: Optional[str] = None
    original_filename: Optional[str] = None

class ClientResponse(ClientBase):
    id: str
    business_id: str
    assigned_agent_name: Optional[str] = None
    group_id: Optional[str] = None
    original_filename: Optional[str] = None
    created_at: str

    class Config:
        from_attributes = True

# Routes

@router.get("", response_model=List[ClientResponse])
async def list_clients(
    business_id: str = Depends(get_current_business_id),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db)
):
    # Admins and Supervisors can see all clients
    if user.role in ("ADMIN", "SUPER_ADMIN", "SUPERVISOR"):
        stmt = select(Client, User.full_name, User.email, LeadGroup.filename).outerjoin(User, Client.assigned_agent_id == User.id).outerjoin(LeadGroup, Client.group_id == LeadGroup.id).where(Client.business_id == business_id)
    else:
        # Voice Agents only see their assigned clients
        stmt = select(Client, User.full_name, User.email, LeadGroup.filename).outerjoin(User, Client.assigned_agent_id == User.id).outerjoin(LeadGroup, Client.group_id == LeadGroup.id).where(
            Client.business_id == business_id,
            Client.assigned_agent_id == user.id
        )
    
    result = await db.execute(stmt)
    rows = result.all()
    
    response = []
    for row in rows:
        client, agent_name, agent_email, filename = row
        agent_display_name = agent_name if agent_name else (agent_email if agent_email else "Unassigned")
        fname = filename if filename else client.original_filename
        client_dict = {
            "id": client.id,
            "business_id": client.business_id,
            "name": client.name,
            "company_name": client.company_name,
            "contact_number": client.contact_number,
            "email": client.email,
            "address": client.address,
            "status": client.status,
            "assigned_agent_id": client.assigned_agent_id,
            "assigned_agent_name": agent_display_name if client.assigned_agent_id else None,
            "group_id": client.group_id,
            "original_filename": fname,
            "created_at": client.created_at.isoformat()
        }
        response.append(client_dict)
    
    return response

@router.post("", response_model=ClientResponse)
async def create_client(
    payload: ClientCreate,
    business_id: str = Depends(get_current_business_id),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db)
):
    # Determine assignment based on role
    assigned_agent_id = payload.assigned_agent_id
    if assigned_agent_id == "":
        assigned_agent_id = None
        
    if user.role not in ("ADMIN", "SUPER_ADMIN", "SUPERVISOR"):
        # For non-admins (e.g. VOICE_AGENT), force assignment to themselves
        assigned_agent_id = user.id
        
    group_id = payload.group_id if hasattr(payload, 'group_id') and payload.group_id != "" else None
    
    new_client = Client(
        business_id=business_id,
        name=payload.name,
        company_name=payload.company_name,
        contact_number=payload.contact_number,
        email=payload.email,
        address=payload.address,
        status=payload.status,
        assigned_agent_id=assigned_agent_id,
        group_id=group_id,
        original_filename=payload.original_filename if hasattr(payload, 'original_filename') else None
    )
    
    db.add(new_client)
    await db.commit()
    await db.refresh(new_client)
    
    # Fetch agent name for response
    agent_display_name = None
    if assigned_agent_id:
        agent = await db.get(User, assigned_agent_id)
        if agent:
            agent_display_name = agent.full_name or agent.email
            
    client_dict = {
        "id": new_client.id,
        "business_id": new_client.business_id,
        "name": new_client.name,
        "company_name": new_client.company_name,
        "contact_number": new_client.contact_number,
        "email": new_client.email,
        "address": new_client.address,
        "status": new_client.status,
        "assigned_agent_id": new_client.assigned_agent_id,
        "assigned_agent_name": agent_display_name,
        "created_at": new_client.created_at.isoformat()
    }
    return client_dict

@router.put("/{client_id}", response_model=ClientResponse)
async def update_client(
    client_id: str,
    payload: ClientUpdate,
    business_id: str = Depends(get_current_business_id),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db)
):
    client = await db.get(Client, client_id)
    if not client or client.business_id != business_id:
        raise HTTPException(status_code=404, detail="Client not found")
        
    if user.role not in ("ADMIN", "SUPER_ADMIN", "SUPERVISOR"):
        # Voice agent can only edit their own clients
        if client.assigned_agent_id != user.id:
            raise HTTPException(status_code=403, detail="Not authorized to edit this client")
            
        # Optional: prevent voice agent from re-assigning
        payload.assigned_agent_id = client.assigned_agent_id

    update_data = payload.dict(exclude_unset=True)
    
    if update_data.get("assigned_agent_id") == "":
        update_data["assigned_agent_id"] = None
    if update_data.get("group_id") == "":
        update_data["group_id"] = None
        
    for key, value in update_data.items():
        setattr(client, key, value)
        
    await db.commit()
    await db.refresh(client)
    
    agent_display_name = None
    if client.assigned_agent_id:
        agent = await db.get(User, client.assigned_agent_id)
        if agent:
            agent_display_name = agent.full_name or agent.email
            
    client_dict = {
        "id": client.id,
        "business_id": client.business_id,
        "name": client.name,
        "company_name": client.company_name,
        "contact_number": client.contact_number,
        "email": client.email,
        "address": client.address,
        "status": client.status,
        "assigned_agent_id": client.assigned_agent_id,
        "assigned_agent_name": agent_display_name,
        "created_at": client.created_at.isoformat()
    }
    return client_dict

@router.delete("/{client_id}")
async def delete_client(
    client_id: str,
    business_id: str = Depends(get_current_business_id),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db)
):
    if user.role not in ("ADMIN", "SUPER_ADMIN", "SUPERVISOR"):
        raise HTTPException(status_code=403, detail="Not authorized to delete clients")
        
    client = await db.get(Client, client_id)
    if not client or client.business_id != business_id:
        raise HTTPException(status_code=404, detail="Client not found")
        
    await db.delete(client)
    await db.commit()
    
    return {"status": "success", "message": "Client deleted"}
