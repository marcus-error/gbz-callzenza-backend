from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func, distinct, delete
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List
import uuid

from app.core.deps import get_current_business_id, get_current_db_user, require_supervisor_or_admin
from app.db.database import get_db
from app.db.models import User, UserGroup, UserGroupMember, InboundNumber, AuditLog

router = APIRouter(prefix="/api/user-groups", tags=["user_groups"])


# Pydantic Schemas
class GroupMemberOut(BaseModel):
    id: str
    user_id: str
    email: str
    full_name: str
    role: str
    status: str
    is_active: bool
    role_in_group: str
    added_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserGroupCreatePayload(BaseModel):
    name: str
    description: Optional[str] = ""
    status: Optional[str] = "ACTIVE"
    member_ids: Optional[List[str]] = []


class UserGroupUpdatePayload(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    member_ids: Optional[List[str]] = None


class UserGroupOut(BaseModel):
    id: str
    business_id: str
    name: str
    description: str
    status: str
    member_count: int = 0
    assigned_inbound_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserGroupDetailOut(UserGroupOut):
    members: List[GroupMemberOut] = []


class AddMembersPayload(BaseModel):
    user_ids: List[str]
    role_in_group: Optional[str] = "MEMBER"


class GroupStatsOut(BaseModel):
    total_groups: int
    active_groups: int
    total_members: int
    available_agents: int


async def ensure_default_user_groups(db: AsyncSession, business_id: str):
    """
    Ensures that default user groups exist for the business and all created users
    are organized and reflected in user groups automatically.
    """
    if not business_id:
        return

    # Check existing groups in this business
    groups_stmt = select(UserGroup).where(UserGroup.business_id == business_id)
    existing_groups = list((await db.scalars(groups_stmt)).all())
    group_by_name = {g.name.strip().lower(): g for g in existing_groups}

    # Fetch all users belonging to this business
    users_stmt = select(User).where(User.business_id == business_id)
    users = list((await db.scalars(users_stmt)).all())
    if not users:
        return

    created_any_group = False

    # 1. Ensure "Voice Agents Team" group
    voice_group = group_by_name.get("voice agents team")
    if not voice_group:
        voice_group = UserGroup(
            id=str(uuid.uuid4()),
            business_id=business_id,
            name="Voice Agents Team",
            description="Dedicated voice handling agents for inbound routing and outbound campaigns",
            status="ACTIVE",
        )
        db.add(voice_group)
        await db.flush()
        group_by_name["voice agents team"] = voice_group
        created_any_group = True

    # 2. Ensure "Supervisors Team" group
    super_group = group_by_name.get("supervisors team") or group_by_name.get("supervisor team")
    if not super_group:
        super_group = UserGroup(
            id=str(uuid.uuid4()),
            business_id=business_id,
            name="Supervisors Team",
            description="Supervisors managing agent queues, call monitoring, and live barge-in",
            status="ACTIVE",
        )
        db.add(super_group)
        await db.flush()
        group_by_name["supervisors team"] = super_group
        created_any_group = True

    # 3. Ensure "General Operations & Support" group
    general_group = group_by_name.get("general operations & support") or group_by_name.get("general support")
    if not general_group:
        general_group = UserGroup(
            id=str(uuid.uuid4()),
            business_id=business_id,
            name="General Operations & Support",
            description="Support agents, operations staff, and user accounts",
            status="ACTIVE",
        )
        db.add(general_group)
        await db.flush()
        group_by_name["general operations & support"] = general_group
        created_any_group = True

    # Fetch all existing member user_ids in this business
    existing_member_user_ids = set((await db.scalars(
        select(UserGroupMember.user_id)
        .join(UserGroup, UserGroupMember.group_id == UserGroup.id)
        .where(UserGroup.business_id == business_id)
    )).all())

    # Map unassigned users into appropriate groups
    added_any_member = False
    for u in users:
        if u.id in existing_member_user_ids:
            continue
        
        target_group = general_group
        role_upper = (u.role or "USER").upper()
        if role_upper in ("VOICE_AGENT", "AGENT"):
            target_group = voice_group
        elif role_upper in ("SUPERVISOR", "ADMIN", "SUPER_ADMIN"):
            target_group = super_group

        ugm = UserGroupMember(
            id=str(uuid.uuid4()),
            group_id=target_group.id,
            user_id=u.id,
            role_in_group="LEAD" if role_upper in ("SUPERVISOR", "SUPER_ADMIN") else "MEMBER",
        )
        db.add(ugm)
        existing_member_user_ids.add(u.id)
        added_any_member = True

    if created_any_group or added_any_member:
        await db.commit()


@router.get("/stats/summary", response_model=GroupStatsOut)
async def get_user_group_stats(
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_supervisor_or_admin),
):
    await ensure_default_user_groups(db, business_id)

    # Total groups
    total_q = select(func.count(UserGroup.id)).where(UserGroup.business_id == business_id)
    total_groups = (await db.scalar(total_q)) or 0

    # Active groups
    active_q = select(func.count(UserGroup.id)).where(UserGroup.business_id == business_id, UserGroup.status == "ACTIVE")
    active_groups = (await db.scalar(active_q)) or 0

    # Total distinct members across all groups in this business
    members_q = (
        select(func.count(distinct(UserGroupMember.user_id)))
        .join(UserGroup, UserGroupMember.group_id == UserGroup.id)
        .where(UserGroup.business_id == business_id)
    )
    total_members = (await db.scalar(members_q)) or 0
    if total_members == 0:
        total_members = (await db.scalar(select(func.count(User.id)).where(User.business_id == business_id))) or 0

    # Available agents (active voice agents or users with status AVAILABLE/ACTIVE)
    available_q = (
        select(func.count(distinct(User.id)))
        .where(
            User.business_id == business_id,
            User.is_active == True,
            User.status.in_(["AVAILABLE", "ACTIVE"]),
            User.role.in_(["VOICE_AGENT", "AGENT", "USER"])
        )
    )
    available_agents = (await db.scalar(available_q)) or 0

    return GroupStatsOut(
        total_groups=total_groups,
        active_groups=active_groups,
        total_members=total_members,
        available_agents=available_agents,
    )


@router.get("", response_model=List[UserGroupOut])
async def list_user_groups(
    status_filter: Optional[str] = None,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_supervisor_or_admin),
):
    await ensure_default_user_groups(db, business_id)

    query = select(UserGroup).where(UserGroup.business_id == business_id)
    if status_filter:
        query = query.where(UserGroup.status == status_filter.upper())
    query = query.order_by(UserGroup.created_at.desc())

    groups = (await db.scalars(query)).all()
    results = []

    for grp in groups:
        # Get member count
        m_count = (await db.scalar(
            select(func.count(UserGroupMember.id)).where(UserGroupMember.group_id == grp.id)
        )) or 0

        # Get assigned inbound numbers count
        inbound_count = (await db.scalar(
            select(func.count(InboundNumber.id)).where(InboundNumber.assigned_group_id == grp.id)
        )) or 0

        results.append(UserGroupOut(
            id=grp.id,
            business_id=grp.business_id,
            name=grp.name,
            description=grp.description or "",
            status=grp.status,
            member_count=m_count,
            assigned_inbound_count=inbound_count,
            created_at=grp.created_at,
            updated_at=grp.updated_at,
        ))

    return results


@router.get("/{group_id}", response_model=UserGroupDetailOut)
async def get_user_group(
    group_id: str,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_supervisor_or_admin),
):
    grp = await db.get(UserGroup, group_id)
    if not grp or grp.business_id != business_id:
        raise HTTPException(status_code=404, detail="User Group not found")

    # Fetch members with User details
    members_q = (
        select(UserGroupMember, User)
        .join(User, UserGroupMember.user_id == User.id)
        .where(UserGroupMember.group_id == group_id)
        .order_by(UserGroupMember.added_at.desc())
    )
    members_res = (await db.execute(members_q)).all()

    members_list = []
    for ugm, u in members_res:
        members_list.append(GroupMemberOut(
            id=ugm.id,
            user_id=u.id,
            email=u.email,
            full_name=u.full_name or u.email.split("@")[0],
            role=u.role,
            status=u.status or "AVAILABLE",
            is_active=u.is_active,
            role_in_group=ugm.role_in_group,
            added_at=ugm.added_at,
        ))

    inbound_count = (await db.scalar(
        select(func.count(InboundNumber.id)).where(InboundNumber.assigned_group_id == grp.id)
    )) or 0

    return UserGroupDetailOut(
        id=grp.id,
        business_id=grp.business_id,
        name=grp.name,
        description=grp.description or "",
        status=grp.status,
        member_count=len(members_list),
        assigned_inbound_count=inbound_count,
        created_at=grp.created_at,
        updated_at=grp.updated_at,
        members=members_list,
    )


@router.post("", response_model=UserGroupDetailOut, status_code=status.HTTP_201_CREATED)
async def create_user_group(
    payload: UserGroupCreatePayload,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    name_clean = payload.name.strip()
    if not name_clean:
        raise HTTPException(status_code=400, detail="Group name is required")

    # Check duplicate group name for this business
    existing = await db.scalar(
        select(UserGroup).where(
            UserGroup.business_id == business_id,
            func.lower(UserGroup.name) == name_clean.lower(),
        )
    )
    if existing:
        raise HTTPException(status_code=400, detail="A group with this name already exists")

    new_group = UserGroup(
        id=str(uuid.uuid4()),
        business_id=business_id,
        name=name_clean,
        description=(payload.description or "").strip(),
        status=(payload.status or "ACTIVE").upper(),
    )
    db.add(new_group)
    await db.flush()

    members_list = []
    # Add initial members if provided
    if payload.member_ids:
        # Validate that users exist
        users_q = select(User).where(User.id.in_(payload.member_ids))
        valid_users = (await db.scalars(users_q)).all()
        for u in valid_users:
            ugm = UserGroupMember(
                id=str(uuid.uuid4()),
                group_id=new_group.id,
                user_id=u.id,
                role_in_group="MEMBER",
            )
            db.add(ugm)
            members_list.append(GroupMemberOut(
                id=ugm.id,
                user_id=u.id,
                email=u.email,
                full_name=u.full_name or u.email.split("@")[0],
                role=u.role,
                status=u.status or "AVAILABLE",
                is_active=u.is_active,
                role_in_group=ugm.role_in_group,
                added_at=ugm.added_at,
            ))

    # Audit log
    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id,
        user_id=current_user.id,
        action="GROUP_CREATED",
        entity="user_groups",
        entity_id=new_group.id,
        details={"name": new_group.name, "member_count": len(members_list)},
    )
    db.add(audit)

    await db.commit()
    await db.refresh(new_group)

    return UserGroupDetailOut(
        id=new_group.id,
        business_id=new_group.business_id,
        name=new_group.name,
        description=new_group.description or "",
        status=new_group.status,
        member_count=len(members_list),
        assigned_inbound_count=0,
        created_at=new_group.created_at,
        updated_at=new_group.updated_at,
        members=members_list,
    )


@router.put("/{group_id}", response_model=UserGroupDetailOut)
async def update_user_group(
    group_id: str,
    payload: UserGroupUpdatePayload,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    grp = await db.get(UserGroup, group_id)
    if not grp or grp.business_id != business_id:
        raise HTTPException(status_code=404, detail="User Group not found")

    if payload.name is not None:
        name_clean = payload.name.strip()
        if not name_clean:
            raise HTTPException(status_code=400, detail="Group name cannot be empty")
        
        # Check duplicate
        existing = await db.scalar(
            select(UserGroup).where(
                UserGroup.business_id == business_id,
                UserGroup.id != group_id,
                func.lower(UserGroup.name) == name_clean.lower(),
            )
        )
        if existing:
            raise HTTPException(status_code=400, detail="Another group with this name already exists")
        grp.name = name_clean

    if payload.description is not None:
        grp.description = payload.description.strip()

    if payload.status is not None:
        grp.status = payload.status.upper()

    # Update members if member_ids list is passed
    if payload.member_ids is not None:
        # Delete existing members
        await db.execute(delete(UserGroupMember).where(UserGroupMember.group_id == group_id))
        
        # Add new members
        if payload.member_ids:
            valid_users = (await db.scalars(select(User).where(User.id.in_(payload.member_ids)))).all()
            for u in valid_users:
                ugm = UserGroupMember(
                    id=str(uuid.uuid4()),
                    group_id=grp.id,
                    user_id=u.id,
                    role_in_group="MEMBER",
                )
                db.add(ugm)

    # Audit log
    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id,
        user_id=current_user.id,
        action="GROUP_UPDATED",
        entity="user_groups",
        entity_id=grp.id,
        details={"name": grp.name, "status": grp.status},
    )
    db.add(audit)

    await db.commit()
    await db.refresh(grp)

    # Return full detail
    return await get_user_group(grp.id, business_id, db, current_user)


@router.delete("/{group_id}")
async def delete_user_group(
    group_id: str,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    grp = await db.get(UserGroup, group_id)
    if not grp or grp.business_id != business_id:
        raise HTTPException(status_code=404, detail="User Group not found")

    # Check if assigned to any inbound numbers
    inbound_assigned = (await db.scalars(
        select(InboundNumber).where(InboundNumber.assigned_group_id == group_id)
    )).all()

    if inbound_assigned:
        # Safely unassign from inbound numbers so calls can fallback instead of foreign key failure
        for inb in inbound_assigned:
            inb.assigned_group_id = None
            db.add(inb)

    # Delete memberships first (CASCADE also handles it, but explicit delete guarantees clean removal)
    await db.execute(delete(UserGroupMember).where(UserGroupMember.group_id == group_id))
    
    # Delete the group
    await db.delete(grp)

    # Audit log
    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id,
        user_id=current_user.id,
        action="GROUP_DELETED",
        entity="user_groups",
        entity_id=group_id,
        details={"name": grp.name, "unassigned_inbound_count": len(inbound_assigned)},
    )
    db.add(audit)

    await db.commit()

    return {
        "success": True, 
        "message": f"User group '{grp.name}' deleted successfully",
        "unassigned_inbound_count": len(inbound_assigned),
    }


@router.get("/{group_id}/members", response_model=List[GroupMemberOut])
async def list_group_members(
    group_id: str,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_supervisor_or_admin),
):
    grp = await db.get(UserGroup, group_id)
    if not grp or grp.business_id != business_id:
        raise HTTPException(status_code=404, detail="User Group not found")

    members_q = (
        select(UserGroupMember, User)
        .join(User, UserGroupMember.user_id == User.id)
        .where(UserGroupMember.group_id == group_id)
        .order_by(UserGroupMember.added_at.desc())
    )
    res = (await db.execute(members_q)).all()

    return [
        GroupMemberOut(
            id=ugm.id,
            user_id=u.id,
            email=u.email,
            full_name=u.full_name or u.email.split("@")[0],
            role=u.role,
            status=u.status or "AVAILABLE",
            is_active=u.is_active,
            role_in_group=ugm.role_in_group,
            added_at=ugm.added_at,
        )
        for ugm, u in res
    ]


@router.post("/{group_id}/members", response_model=List[GroupMemberOut])
async def add_group_members(
    group_id: str,
    payload: AddMembersPayload,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    grp = await db.get(UserGroup, group_id)
    if not grp or grp.business_id != business_id:
        raise HTTPException(status_code=404, detail="User Group not found")

    if not payload.user_ids:
        raise HTTPException(status_code=400, detail="No user IDs provided")

    # Fetch existing memberships
    existing_user_ids = set((await db.scalars(
        select(UserGroupMember.user_id).where(UserGroupMember.group_id == group_id)
    )).all())

    # Filter out already added users
    new_user_ids = [uid for uid in payload.user_ids if uid not in existing_user_ids]
    if new_user_ids:
        valid_users = (await db.scalars(select(User).where(User.id.in_(new_user_ids)))).all()
        for u in valid_users:
            ugm = UserGroupMember(
                id=str(uuid.uuid4()),
                group_id=group_id,
                user_id=u.id,
                role_in_group=payload.role_in_group or "MEMBER",
            )
            db.add(ugm)

        audit = AuditLog(
            id=str(uuid.uuid4()),
            business_id=business_id,
            user_id=current_user.id,
            action="GROUP_MEMBER_ADDED",
            entity="user_groups",
            entity_id=group_id,
            details={"added_count": len(valid_users), "user_ids": [u.id for u in valid_users]},
        )
        db.add(audit)
        await db.commit()

    return await list_group_members(group_id, business_id, db, current_user)


@router.delete("/{group_id}/members/{user_id}")
async def remove_group_member(
    group_id: str,
    user_id: str,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    grp = await db.get(UserGroup, group_id)
    if not grp or grp.business_id != business_id:
        raise HTTPException(status_code=404, detail="User Group not found")

    membership = await db.scalar(
        select(UserGroupMember).where(
            UserGroupMember.group_id == group_id,
            UserGroupMember.user_id == user_id,
        )
    )
    if not membership:
        raise HTTPException(status_code=404, detail="User is not a member of this group")

    await db.delete(membership)

    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id,
        user_id=current_user.id,
        action="GROUP_MEMBER_REMOVED",
        entity="user_groups",
        entity_id=group_id,
        details={"removed_user_id": user_id},
    )
    db.add(audit)

    await db.commit()

    return {"success": True, "message": "Member removed from group successfully"}
