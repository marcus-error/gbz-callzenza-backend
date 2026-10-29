import json
from typing import Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from loguru import logger

from app.db.models import User, Lead, AgentAssignmentQueue

# ---------------------------------------------------------------------------
# Tool Declarations (Schemas for Gemini API)
# ---------------------------------------------------------------------------

TOOLS_DECLARATION = [
    {
        "name": "get_available_human_agents",
        "description": "Returns the list of currently active human agents who are available to take a call or chat.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "transfer_call_to_agent",
        "description": "Transfers the current ongoing interaction to an available human agent. Use this when the user explicitly requests to speak to a human or agent.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "agent_id": {
                    "type": "STRING",
                    "description": "The ID of the human agent to transfer to. Leave empty if transferring to any available agent."
                },
                "reason": {
                    "type": "STRING",
                    "description": "The reason for the transfer."
                }
            },
        }
    },
    {
        "name": "get_current_leads_count",
        "description": "Returns the current number of total leads in the CallZenza system. Use this when asked for lead statistics.",
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "create_new_lead",
        "description": "Generates a new lead in the CallZenza system with the provided customer information.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "first_name": {"type": "STRING"},
                "last_name": {"type": "STRING"},
                "phone": {"type": "STRING"},
                "email": {"type": "STRING"},
                "requirement": {"type": "STRING", "description": "Customer's requirements or notes"},
            },
            "required": ["first_name", "phone"]
        }
    }
]

# ---------------------------------------------------------------------------
# Tool Executors
# ---------------------------------------------------------------------------

async def get_available_human_agents(db: AsyncSession, business_id: str = None) -> str:
    stmt = select(User).where(User.role == "AGENT", User.status == "AVAILABLE")
    if business_id:
        stmt = stmt.where(User.business_id == business_id)
    
    result = await db.scalars(stmt)
    agents = result.all()
    
    if not agents:
        return json.dumps({"status": "success", "message": "No human agents are currently available.", "agents": []})
        
    agents_list = [{"id": a.id, "name": a.full_name or a.email} for a in agents]
    return json.dumps({"status": "success", "message": f"Found {len(agents_list)} available agents.", "agents": agents_list})


async def transfer_call_to_agent(db: AsyncSession, business_id: str = None, agent_id: str = "", reason: str = "") -> str:
    # Check if a specific agent was requested
    if agent_id:
        stmt = select(User).where(User.id == agent_id, User.status == "AVAILABLE")
        res = await db.scalars(stmt)
        agent = res.first()
        if not agent:
            return json.dumps({"status": "error", "message": "The requested agent is not available."})
    else:
        # Find any available agent
        stmt = select(User).where(User.role == "AGENT", User.status == "AVAILABLE")
        if business_id:
            stmt = stmt.where(User.business_id == business_id)
        res = await db.scalars(stmt)
        agent = res.first()
        if not agent:
            # Fallback: Create a lead instead of dropping
            return json.dumps({
                "status": "partial_success", 
                "message": "No agents are available. I will need to create a lead for them to call you back. Please provide your name and phone number."
            })
            
    # Mocking the transfer operation since CallZenza relies on an external Voice/SIP provider for actual transfers.
    # In a real scenario, this would trigger CallService.transfer().
    return json.dumps({
        "status": "success", 
        "message": f"Call transfer initiated successfully to {agent.full_name or agent.email}.",
        "transferred_to_agent": agent.full_name or agent.email
    })


async def get_current_leads_count(db: AsyncSession, business_id: str = None) -> str:
    stmt = select(func.count(Lead.id))
    if business_id:
        stmt = stmt.where(Lead.business_id == business_id)
        
    count = await db.scalar(stmt)
    return json.dumps({"status": "success", "total_leads": count})


async def create_new_lead(db: AsyncSession, business_id: str = None, first_name: str = "", last_name: str = "", phone: str = "", email: str = "", requirement: str = "") -> str:
    if not first_name or not phone:
        return json.dumps({"status": "error", "message": "First name and phone number are required to create a lead."})
        
    try:
        new_lead = Lead(
            business_id=business_id,
            created_by_role="SYSTEM_AI",
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            email=email,
            requirement=requirement,
            source="AI_CHATBOT",
            status="NEW"
        )
        db.add(new_lead)
        await db.commit()
        await db.refresh(new_lead)
        return json.dumps({
            "status": "success", 
            "message": "Lead created successfully.", 
            "lead_id": new_lead.id
        })
    except Exception as e:
        logger.error(f"Error creating lead via AI: {e}")
        return json.dumps({"status": "error", "message": str(e)})


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def dispatch_tool_call(db: AsyncSession, tool_name: str, arguments: Dict[str, Any], business_id: str = None) -> str:
    """Executes the requested tool and returns the JSON string result."""
    logger.info(f"AI requested tool execution: {tool_name} with args: {arguments}")
    
    try:
        if tool_name == "get_available_human_agents":
            return await get_available_human_agents(db, business_id)
            
        elif tool_name == "transfer_call_to_agent":
            return await transfer_call_to_agent(
                db, 
                business_id=business_id,
                agent_id=arguments.get("agent_id", ""),
                reason=arguments.get("reason", "")
            )
            
        elif tool_name == "get_current_leads_count":
            return await get_current_leads_count(db, business_id)
            
        elif tool_name == "create_new_lead":
            return await create_new_lead(
                db,
                business_id=business_id,
                first_name=arguments.get("first_name", ""),
                last_name=arguments.get("last_name", ""),
                phone=arguments.get("phone", ""),
                email=arguments.get("email", ""),
                requirement=arguments.get("requirement", "")
            )
            
        else:
            return json.dumps({"status": "error", "message": f"Unknown tool: {tool_name}"})
            
    except Exception as e:
        logger.error(f"Tool execution failed for {tool_name}: {e}")
        return json.dumps({"status": "error", "message": f"Internal error executing {tool_name}: {e}"})
