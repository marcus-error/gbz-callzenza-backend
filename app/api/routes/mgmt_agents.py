from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import uuid

from app.db.database import get_db
from app.core.deps import get_current_db_user
from app.db.models import MgmtAgent, MgmtVoiceAgent, MgmtAIAgent, User, MgmtAIAgentUser, MgmtVoiceAgentUser
from app.schemas.mgmt_agent import (
    MgmtAgentCreate,
    MgmtAgentUpdate,
    MgmtAgentOut,
    MgmtVoiceAgentCreate,
    MgmtVoiceAgentUpdate,
    MgmtVoiceAgentOut,
    MgmtAIAgentCreate,
    MgmtAIAgentUpdate,
    MgmtAIAgentOut,
    MgmtVoiceAgentAssign,
    MgmtAIAgentAssign,
    AgentChildren,
    VoiceAgentGroup,
    AIAgentGroup
)

from app.core.deps import get_current_db_user, require_supervisor_or_admin

router = APIRouter(prefix="/api/mgmt-agents", tags=["Management Agents"])

@router.post("", response_model=MgmtAgentOut)
async def create_agent(agent_in: MgmtAgentCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_supervisor_or_admin)):
    if not agent_in.name.strip():
        raise HTTPException(status_code=400, detail="Agent Name (ID) must not be empty")
    exist_res = await db.execute(select(MgmtAgent).where(MgmtAgent.name == agent_in.name))
    if exist_res.scalars().first():
        raise HTTPException(status_code=400, detail="Agent ID / Name already exists")
    
    # Assign user_id based on role
    final_user_id = agent_in.user_id if current_user.role in ("ADMIN", "SUPER_ADMIN") else current_user.id
    if not final_user_id:
        final_user_id = current_user.id

    login_user_id = None
    if agent_in.type in ("VOICE", "SUPERVISOR") and agent_in.email:
        if not agent_in.password or not agent_in.password.strip():
            raise HTTPException(status_code=400, detail="Password is required when creating a Voice Agent or Supervisor account")
        normalized_email = agent_in.email.strip().lower()
        user_res = await db.execute(select(User).where(User.email == normalized_email))
        if user_res.scalars().first():
            raise HTTPException(status_code=400, detail="Email already registered")
        
        from app.core.security import hash_password
        login_user_id = str(uuid.uuid4())
        hashed = hash_password(agent_in.password.strip())
        new_user = User(
            id=login_user_id,
            email=normalized_email,
            hashed_password=hashed,
            plain_password=agent_in.password.strip(),
            role="SUPERVISOR" if agent_in.type == "SUPERVISOR" else "VOICE_AGENT",
            full_name=agent_in.name,
            is_active=True,
            email_verified=True,
            business_id=current_user.business_id,
        )
        db.add(new_user)
        await db.flush()

    db_agent = MgmtAgent(
        id=str(uuid.uuid4()),
        name=agent_in.name,
        type=agent_in.type,
        user_id=final_user_id,
        login_user_id=login_user_id,
        business_id=current_user.business_id,
    )
    db.add(db_agent)
    await db.commit()
    await db.refresh(db_agent)
    return format_agent_out(db_agent, [], [])

@router.get("", response_model=List[MgmtAgentOut])
async def get_agents(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    query = select(MgmtAgent)
    if current_user.business_id:
        query = query.where(MgmtAgent.business_id == current_user.business_id)
    if current_user.role not in ("ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER"):
        query = (
            query
            .outerjoin(MgmtVoiceAgent, MgmtVoiceAgent.agent_id == MgmtAgent.id)
            .outerjoin(MgmtVoiceAgentUser, MgmtVoiceAgentUser.voice_agent_id == MgmtVoiceAgent.id)
            .outerjoin(MgmtAIAgent, MgmtAIAgent.agent_id == MgmtAgent.id)
            .outerjoin(MgmtAIAgentUser, MgmtAIAgentUser.ai_agent_id == MgmtAIAgent.id)
            .where(
                (MgmtAgent.user_id == current_user.id) |
                (MgmtVoiceAgentUser.user_id == current_user.id) |
                (MgmtAIAgentUser.user_id == current_user.id)
            )
            .distinct()
        )
    agents_res = await db.execute(query)
    agents = agents_res.scalars().all()
    result = []
    for ag in agents:
        v_res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.agent_id == ag.id))
        v_agents = v_res.scalars().all()
        a_res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.agent_id == ag.id))
        a_agents = a_res.scalars().all()
        result.append(format_agent_out(ag, v_agents, a_agents))
    return result

@router.get("/{id}", response_model=MgmtAgentOut)
async def get_agent(id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    query = select(MgmtAgent).where(MgmtAgent.id == id)
    if current_user.business_id:
        query = query.where(MgmtAgent.business_id == current_user.business_id)
    if current_user.role not in ("ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER"):
        query = (
            query
            .outerjoin(MgmtVoiceAgent, MgmtVoiceAgent.agent_id == MgmtAgent.id)
            .outerjoin(MgmtVoiceAgentUser, MgmtVoiceAgentUser.voice_agent_id == MgmtVoiceAgent.id)
            .outerjoin(MgmtAIAgent, MgmtAIAgent.agent_id == MgmtAgent.id)
            .outerjoin(MgmtAIAgentUser, MgmtAIAgentUser.ai_agent_id == MgmtAIAgent.id)
            .where(
                (MgmtAgent.user_id == current_user.id) |
                (MgmtVoiceAgentUser.user_id == current_user.id) |
                (MgmtAIAgentUser.user_id == current_user.id)
            )
            .distinct()
        )
    res = await db.execute(query)
    ag = res.scalars().first()
    if not ag:
        raise HTTPException(status_code=404, detail="Agent not found or access denied")
    v_res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.agent_id == id))
    v_agents = v_res.scalars().all()
    a_res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.agent_id == id))
    a_agents = a_res.scalars().all()
    return format_agent_out(ag, v_agents, a_agents)

@router.put("/{id}", response_model=MgmtAgentOut)
async def update_agent(id: str, agent_in: MgmtAgentUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == id))
    ag = res.scalars().first()
    if not ag:
        raise HTTPException(status_code=404, detail="Agent not found")
    if current_user.business_id and ag.business_id and ag.business_id != current_user.business_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this agent")
    if current_user.role not in ("ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER") and ag.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to update this agent")
    if agent_in.name is not None:
        if not agent_in.name.strip():
            raise HTTPException(status_code=400, detail="Agent Name (ID) must not be empty")
        exist_res = await db.execute(select(MgmtAgent).where(MgmtAgent.name == agent_in.name, MgmtAgent.id != id))
        if exist_res.scalars().first():
            raise HTTPException(status_code=400, detail="Agent ID / Name already exists")
        ag.name = agent_in.name
    if agent_in.type is not None:
        ag.type = agent_in.type
    await db.commit()
    await db.refresh(ag)
    v_res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.agent_id == id))
    v_agents = v_res.scalars().all()
    a_res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.agent_id == id))
    a_agents = a_res.scalars().all()
    return format_agent_out(ag, v_agents, a_agents)

@router.delete("/{id}")
async def delete_agent(id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == id))
    ag = res.scalars().first()
    if not ag:
        raise HTTPException(status_code=404, detail="Agent not found")
    if current_user.business_id and ag.business_id and ag.business_id != current_user.business_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this agent")
    if current_user.role not in ("ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER") and ag.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this agent")
    await db.delete(ag)
    await db.commit()
    return {"success": True, "message": "Agent deleted successfully"}

# Voice Agent / Voice IDs APIs

@router.post("/{agent_id}/voice-ids", response_model=MgmtVoiceAgentOut)
async def create_voice_agent(agent_id: str, v_in: MgmtVoiceAgentCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    if not v_in.voice_id.strip():
        raise HTTPException(status_code=400, detail="Voice ID must not be empty")
    ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == agent_id))
    ag = ag_res.scalars().first()
    if not ag:
        raise HTTPException(status_code=404, detail="Parent Agent not found")
    if current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this agent")
    # Check for duplicates
    exist_res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.agent_id == agent_id, MgmtVoiceAgent.voice_id == v_in.voice_id))
    existing = exist_res.scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="Voice ID already exists under this Agent")
    db_v = MgmtVoiceAgent(id=str(uuid.uuid4()), agent_id=agent_id, voice_id=v_in.voice_id)
    db.add(db_v)
    await db.commit()
    await db.refresh(db_v)
    return db_v

@router.get("/{agent_id}/voice-ids", response_model=List[MgmtVoiceAgentOut])
async def get_voice_ids(agent_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    # Check parent agent access (owner or assigned to child)
    if current_user.role in ("ADMIN", "SUPER_ADMIN"):
        ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == agent_id))
    else:
        ag_res = await db.execute(
            select(MgmtAgent)
            .outerjoin(MgmtVoiceAgent, MgmtVoiceAgent.agent_id == MgmtAgent.id)
            .outerjoin(MgmtVoiceAgentUser, MgmtVoiceAgentUser.voice_agent_id == MgmtVoiceAgent.id)
            .outerjoin(MgmtAIAgent, MgmtAIAgent.agent_id == MgmtAgent.id)
            .outerjoin(MgmtAIAgentUser, MgmtAIAgentUser.ai_agent_id == MgmtAIAgent.id)
            .where(
                MgmtAgent.id == agent_id,
                ((MgmtAgent.user_id == current_user.id) |
                (MgmtVoiceAgentUser.user_id == current_user.id) |
                (MgmtAIAgentUser.user_id == current_user.id))
            )
            .distinct()
        )
    ag = ag_res.scalars().first()
    if not ag:
        raise HTTPException(status_code=404, detail="Parent Agent not found or access denied")
    
    res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.agent_id == agent_id))
    v_agents = res.scalars().all()
    
    out_list = []
    for v in v_agents:
        # fetch assigned users
        u_res = await db.execute(
            select(User).join(MgmtVoiceAgentUser, User.id == MgmtVoiceAgentUser.user_id)
            .where(MgmtVoiceAgentUser.voice_agent_id == v.id)
        )
        users = u_res.scalars().all()
        assigned = [{"id": u.id, "name": u.full_name, "email": u.email} for u in users]
        
        # If normal user, skip agents not assigned to them (unless they own parent)
        if current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id:
            if current_user.id not in [u.id for u in users]:
                continue

        out_list.append({
            "id": v.id,
            "agent_id": v.agent_id,
            "voice_id": v.voice_id,
            "assigned_users": assigned
        })
    return out_list

@router.post("/voice-ids/{id}/users")
async def assign_users_to_voice_id(id: str, payload: MgmtVoiceAgentAssign, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    v_res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.id == id))
    db_v = v_res.scalars().first()
    if not db_v:
        raise HTTPException(status_code=404, detail="Voice ID record not found")
        
    ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == db_v.agent_id))
    ag = ag_res.scalars().first()
    if not ag or (current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to update this Voice ID")
        
    # Clear existing assignments
    await db.execute(
        select(MgmtVoiceAgentUser).where(MgmtVoiceAgentUser.voice_agent_id == id)
    ) # just a select, wait, need a delete query
    # use a more robust way to delete
    del_res = await db.execute(select(MgmtVoiceAgentUser).where(MgmtVoiceAgentUser.voice_agent_id == id))
    for item in del_res.scalars().all():
        await db.delete(item)
        
    # Add new assignments
    for uid in payload.user_ids:
        db.add(MgmtVoiceAgentUser(id=str(uuid.uuid4()), voice_agent_id=id, user_id=uid))
        
    await db.commit()
    return {"success": True, "message": "Users assigned successfully"}

@router.put("/voice-ids/{id}", response_model=MgmtVoiceAgentOut)
async def update_voice_id(id: str, v_in: MgmtVoiceAgentUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    if not v_in.voice_id.strip():
        raise HTTPException(status_code=400, detail="Voice ID must not be empty")
    v_res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.id == id))
    db_v = v_res.scalars().first()
    if not db_v:
        raise HTTPException(status_code=404, detail="Voice ID record not found")
    
    ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == db_v.agent_id))
    ag = ag_res.scalars().first()
    if not ag or (current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to update this Voice ID")
    
    # Check for duplicates
    exist_res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.agent_id == db_v.agent_id, MgmtVoiceAgent.voice_id == v_in.voice_id, MgmtVoiceAgent.id != id))
    existing = exist_res.scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="Voice ID already exists under this Agent")
    
    db_v.voice_id = v_in.voice_id
    await db.commit()
    await db.refresh(db_v)
    return db_v

@router.delete("/voice-ids/{id}")
async def delete_voice_id(id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    v_res = await db.execute(select(MgmtVoiceAgent).where(MgmtVoiceAgent.id == id))
    db_v = v_res.scalars().first()
    if not db_v:
        raise HTTPException(status_code=404, detail="Voice ID record not found")
        
    ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == db_v.agent_id))
    ag = ag_res.scalars().first()
    if not ag or (current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to delete this Voice ID")
        
    await db.delete(db_v)
    await db.commit()
    return {"success": True, "message": "Voice ID deleted successfully"}

# AI Agent Names APIs

@router.post("/{agent_id}/ai-agents", response_model=MgmtAIAgentOut)
async def create_ai_agent(agent_id: str, a_in: MgmtAIAgentCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    if not a_in.name.strip():
        raise HTTPException(status_code=400, detail="AI Agent name must not be empty")
    ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == agent_id))
    ag = ag_res.scalars().first()
    if not ag:
        raise HTTPException(status_code=404, detail="Parent Agent not found")
    if current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this agent")
    # Check for duplicates
    exist_res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.agent_id == agent_id, MgmtAIAgent.name == a_in.name))
    existing = exist_res.scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="AI Agent name already exists under this Agent")
    db_a = MgmtAIAgent(id=str(uuid.uuid4()), agent_id=agent_id, name=a_in.name)
    db.add(db_a)
    await db.commit()
    await db.refresh(db_a)
    return db_a

@router.get("/{agent_id}/ai-agents", response_model=List[MgmtAIAgentOut])
async def get_ai_agent_names(agent_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    if current_user.role in ("ADMIN", "SUPER_ADMIN"):
        ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == agent_id))
    else:
        ag_res = await db.execute(
            select(MgmtAgent)
            .outerjoin(MgmtVoiceAgent, MgmtVoiceAgent.agent_id == MgmtAgent.id)
            .outerjoin(MgmtVoiceAgentUser, MgmtVoiceAgentUser.voice_agent_id == MgmtVoiceAgent.id)
            .outerjoin(MgmtAIAgent, MgmtAIAgent.agent_id == MgmtAgent.id)
            .outerjoin(MgmtAIAgentUser, MgmtAIAgentUser.ai_agent_id == MgmtAIAgent.id)
            .where(
                MgmtAgent.id == agent_id,
                ((MgmtAgent.user_id == current_user.id) |
                (MgmtVoiceAgentUser.user_id == current_user.id) |
                (MgmtAIAgentUser.user_id == current_user.id))
            )
            .distinct()
        )
    ag = ag_res.scalars().first()
    if not ag:
        raise HTTPException(status_code=404, detail="Parent Agent not found or access denied")
    
    res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.agent_id == agent_id))
    a_agents = res.scalars().all()
    
    out_list = []
    for a in a_agents:
        # fetch assigned users
        u_res = await db.execute(
            select(User).join(MgmtAIAgentUser, User.id == MgmtAIAgentUser.user_id)
            .where(MgmtAIAgentUser.ai_agent_id == a.id)
        )
        users = u_res.scalars().all()
        assigned = [{"id": u.id, "name": u.full_name, "email": u.email} for u in users]
        
        # If normal user, skip agents not assigned to them (unless they own parent)
        if current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id:
            if current_user.id not in [u.id for u in users]:
                continue
                
        out_list.append({
            "id": a.id,
            "agent_id": a.agent_id,
            "name": a.name,
            "assigned_users": assigned
        })
    return out_list

@router.post("/ai-agents/{id}/users")
async def assign_users_to_ai_agent(id: str, payload: MgmtAIAgentAssign, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    a_res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.id == id))
    db_a = a_res.scalars().first()
    if not db_a:
        raise HTTPException(status_code=404, detail="AI Agent record not found")
        
    ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == db_a.agent_id))
    ag = ag_res.scalars().first()
    if not ag or (current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to update this AI Agent")
        
    # Clear existing assignments
    del_res = await db.execute(select(MgmtAIAgentUser).where(MgmtAIAgentUser.ai_agent_id == id))
    for item in del_res.scalars().all():
        await db.delete(item)
        
    # Add new assignments
    for uid in payload.user_ids:
        db.add(MgmtAIAgentUser(id=str(uuid.uuid4()), ai_agent_id=id, user_id=uid))
        
    await db.commit()
    return {"success": True, "message": "Users assigned successfully"}

@router.put("/ai-agents/{id}", response_model=MgmtAIAgentOut)
async def update_ai_agent_name(id: str, a_in: MgmtAIAgentUpdate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    if not a_in.name.strip():
        raise HTTPException(status_code=400, detail="AI Agent name must not be empty")
    a_res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.id == id))
    db_a = a_res.scalars().first()
    if not db_a:
        raise HTTPException(status_code=404, detail="AI Agent name record not found")
        
    ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == db_a.agent_id))
    ag = ag_res.scalars().first()
    if not ag or (current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to update this AI Agent")
    
    # Check for duplicates
    exist_res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.agent_id == db_a.agent_id, MgmtAIAgent.name == a_in.name, MgmtAIAgent.id != id))
    existing = exist_res.scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="AI Agent name already exists under this Agent")
    
    db_a.name = a_in.name
    await db.commit()
    await db.refresh(db_a)
    return db_a

@router.delete("/ai-agents/{id}")
async def delete_ai_agent_name(id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_db_user)):
    a_res = await db.execute(select(MgmtAIAgent).where(MgmtAIAgent.id == id))
    db_a = a_res.scalars().first()
    if not db_a:
        raise HTTPException(status_code=404, detail="AI Agent name record not found")
        
    ag_res = await db.execute(select(MgmtAgent).where(MgmtAgent.id == db_a.agent_id))
    ag = ag_res.scalars().first()
    if not ag or (current_user.role not in ("ADMIN", "SUPER_ADMIN") and ag.user_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized to delete this AI Agent")
        
    await db.delete(db_a)
    await db.commit()
    return {"success": True, "message": "AI Agent name deleted successfully"}

def format_agent_out(ag: MgmtAgent, v_agents: List[MgmtVoiceAgent], a_agents: List[MgmtAIAgent]) -> MgmtAgentOut:
    voice_ids = [v.voice_id for v in v_agents]
    ai_names = [a.name for a in a_agents]
    
    v_group = VoiceAgentGroup(id=str(ag.id), voice_ids=voice_ids)
    a_group = AIAgentGroup(id=str(ag.id), names=ai_names)
    
    children = AgentChildren(
        voice_agents=[v_group] if voice_ids else [],
        ai_agents=[a_group] if ai_names else []
    )
    
    return MgmtAgentOut(
        id=str(ag.id),
        name=ag.name,
        type=ag.type,
        user_id=ag.user_id,
        children=children
    )
