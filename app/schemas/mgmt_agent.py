from typing import List, Optional
from pydantic import BaseModel, ConfigDict

# Voice Agent / Voice ID Schemas
class MgmtVoiceAgentCreate(BaseModel):
    voice_id: str

class MgmtVoiceAgentUpdate(BaseModel):
    voice_id: str

class UserBasicOut(BaseModel):
    id: str
    name: str
    email: str

class MgmtVoiceAgentOut(BaseModel):
    id: str
    agent_id: str
    voice_id: str
    assigned_users: List[UserBasicOut] = []
    model_config = ConfigDict(from_attributes=True)

class MgmtVoiceAgentAssign(BaseModel):
    user_ids: List[str]

# AI Agent Name Schemas
class MgmtAIAgentCreate(BaseModel):
    name: str

class MgmtAIAgentUpdate(BaseModel):
    name: str

class MgmtAIAgentOut(BaseModel):
    id: str
    agent_id: str
    name: str
    assigned_users: List[UserBasicOut] = []
    model_config = ConfigDict(from_attributes=True)

class MgmtAIAgentAssign(BaseModel):
    user_ids: List[str]

# Root Agent Schemas
class MgmtAgentCreate(BaseModel):
    name: str
    type: str
    user_id: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None

class MgmtAgentUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None

# Nested Response Schema classes
class VoiceAgentGroup(BaseModel):
    id: str
    voice_ids: List[str]

class AIAgentGroup(BaseModel):
    id: str
    names: List[str]

class AgentChildren(BaseModel):
    voice_agents: List[VoiceAgentGroup] = []
    ai_agents: List[AIAgentGroup] = []

class MgmtAgentOut(BaseModel):
    id: str
    name: str
    type: str
    user_id: Optional[str] = None
    children: AgentChildren
    model_config = ConfigDict(from_attributes=True)
