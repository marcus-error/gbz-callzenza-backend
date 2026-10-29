import os
import shutil
import uuid
from typing import List, Optional, Dict, Any
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, delete, update
from loguru import logger

from app.core.security import get_current_user, get_current_user_optional, require_role
from app.db.database import get_db
from app.db.models import (
    User, 
    KnowledgeDocument, 
    KnowledgeChunk, 
    ChatConversation, 
    ChatMessage, 
    ChatbotConfig,
    RAGAgent,
    RAGCache,
    RAGMiss
)
from app.services.document_service import DocumentProcessingService, UPLOAD_DIR
from app.services.rag_service import RAGService


router = APIRouter(tags=["AI Chatbot & RAG"])

# ---------------------------------------------------------------------------
# Default Department Agents Config
# ---------------------------------------------------------------------------

DEFAULT_DEPARTMENTS = [
    { "key": 900001, "name": "CEO", "icon": "💼", "archetype": "executive", "title": "Executive Intelligence", "desc": "Strategic oversight & leadership" },
    { "key": 900002, "name": "MARKETING", "icon": "📢", "archetype": "growth", "title": "Growth & Outreach", "desc": "Campaigns & brand intelligence" },
    { "key": 900003, "name": "SALE", "icon": "💰", "archetype": "sales", "title": "Conversion Engine", "desc": "Sales objection & script expert" },
    { "key": 900004, "name": "SUPPORT", "icon": "🛡️", "archetype": "support", "title": "Resolution Layer", "desc": "Customer issue & ticket resolver" },
    { "key": 900005, "name": "FINANCE", "icon": "💵", "archetype": "finance", "title": "Fiscal Operations", "desc": "Billing, invoices & refund rules" },
    { "key": 900006, "name": "ENGINEERING", "icon": "🛠️", "archetype": "technical", "title": "Core Tech & Infra", "desc": "System architecture & API specs" },
    { "key": 900007, "name": "OPERATION", "icon": "⚙️", "archetype": "ops", "title": "Workflow Orchestration", "desc": "Daily operations & dispatch" },
    { "key": 900008, "name": "LEGAL", "icon": "⚖️", "archetype": "compliance", "title": "Regulatory & Governance", "desc": "Compliance & contract policies" },
    { "key": 900009, "name": "STRATEGY", "icon": "🎯", "archetype": "strategic", "title": "Market & Vision", "desc": "Competitor & strategic roadmap" },
    { "key": 900010, "name": "APPOINTMENT SCHEDULING", "icon": "📅", "archetype": "booking", "title": "Calendar & Dispatch", "desc": "Calendar & booking workflows" },
]


async def ensure_default_departments(db: AsyncSession):
    """Auto-seeds any missing default department agents."""
    res = await db.scalars(select(RAGAgent.agent_key))
    existing_keys = set(res.all())
    added = False
    for dept in DEFAULT_DEPARTMENTS:
        if dept["key"] not in existing_keys:
            agent = RAGAgent(
                agent_key=dept["key"],
                name=dept["name"],
                archetype_id=dept["archetype"],
                archetype_title=dept["title"],
                archetype_desc=dept["desc"],
                archetype_icon=dept["icon"],
                is_active=True
            )
            db.add(agent)
            added = True
    if added:
        await db.commit()


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User's query")
    conversation_id: Optional[str] = None
    agent_key: Optional[int] = None
    is_global: bool = False

class ChatResponse(BaseModel):
    conversation_id: str
    message: str
    sender: str = "assistant"
    retrieved_context: List[Dict[str, Any]] = []
    latency_ms: int = 0
    pdf_url: Optional[str] = None
    created_at: str

class ConversationOut(BaseModel):
    id: str
    title: str
    message_count: int = 0
    last_message: Optional[str] = None
    pdf_url: Optional[str] = None
    created_at: str
    updated_at: str

class ManualDocumentCreate(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    agent_key: Optional[int] = None

class ConfigUpdate(BaseModel):
    model_name: Optional[str] = None
    temperature: Optional[float] = None
    top_k: Optional[int] = None
    similarity_threshold: Optional[float] = None
    system_prompt: Optional[str] = None
    fallback_message: Optional[str] = None

class CreateAgentPayload(BaseModel):
    name: str = Field(..., min_length=1)
    archetype_id: str = "custom"
    archetype_title: str = ""
    archetype_desc: str = ""
    archetype_icon: str = "🤖"
    system_prompt: str = ""

class CreateCachePayload(BaseModel):
    agent_key: int
    question: str
    answer: str
    source: str = "manual"

class TestRAGRequest(BaseModel):
    query: str = Field(..., min_length=1)
    agent_key: Optional[int] = None


# ---------------------------------------------------------------------------
# User & Agent Chatbot Endpoints (Authenticated)
# ---------------------------------------------------------------------------

@router.post("/api/chatbot/chat", response_model=ChatResponse)
async def chat_with_bot(
    payload: ChatRequest,
    current_user: Optional[dict] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Submits a question to the CallZenza AI Chatbot, performs RAG retrieval,
    generates grounded response, and saves both messages to the conversation.
    """
    raw_sub = (current_user and current_user.get("sub"))
    user_id = None
    if raw_sub:
        user_row = await db.get(User, str(raw_sub))
        if user_row:
            user_id = user_row.id

    # 1. Resolve or create conversation
    conv_id = payload.conversation_id
    conversation = None
    if conv_id:
        conversation = await db.get(ChatConversation, conv_id)
        if conversation and user_id and conversation.user_id and conversation.user_id != user_id:
            conversation = None

    if not conversation:
        title_snippet = payload.message.strip()[:40]
        if len(payload.message.strip()) > 40:
            title_snippet += "..."
        conversation = ChatConversation(
            user_id=user_id,
            title=title_snippet or "New Conversation",
            is_global=payload.is_global
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)

    # 2. Save User Message
    user_msg = ChatMessage(
        conversation_id=conversation.id,
        sender="user",
        content=payload.message.strip()
    )
    db.add(user_msg)
    await db.commit()

    # 3. Perform RAG + Gemini generation (scoped to agent_key if provided)
    rag_result = await RAGService.answer_question(
        db=db,
        query=payload.message.strip(),
        business_id=conversation.business_id,
        agent_key=payload.agent_key or 900001,
        is_global=payload.is_global,
        conversation_id=conversation.id
    )

    # 4. Save Assistant Message
    assistant_msg = ChatMessage(
        conversation_id=conversation.id,
        sender="assistant",
        content=rag_result["answer"],
        retrieved_context=rag_result["retrieved_context"],
        latency_ms=rag_result["latency_ms"]
    )
    db.add(assistant_msg)
    await db.commit()
    await db.refresh(assistant_msg)

    # Update conversation title if it was default
    if conversation.title == "New Conversation":
        conversation.title = payload.message.strip()[:40] + ("..." if len(payload.message.strip()) > 40 else "")
        await db.commit()

    # 5. Generate / Update Conversation Session PDF with complete history
    pdf_url = None
    try:
        from app.services.pdf_service import ConversationPDFService
        all_msgs_res = await db.scalars(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.created_at.asc())
        )
        all_msgs = all_msgs_res.all()
        msg_dicts = [
            {
                "sender": m.sender,
                "content": m.content,
                "created_at": m.created_at.isoformat()
            }
            for m in all_msgs
        ]
        pdf_url = ConversationPDFService.generate_pdf(
            conversation_id=conversation.id,
            messages=msg_dicts,
            title="CALLZENZA / MURI AI CONVERSATION"
        )
        conversation.pdf_path = pdf_url
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to generate conversation PDF: {e}")
        pdf_url = f"/api/chatbot/conversations/{conversation.id}/pdf"

    return ChatResponse(
        conversation_id=conversation.id,
        message=assistant_msg.content,
        sender=assistant_msg.sender,
        retrieved_context=assistant_msg.retrieved_context or [],
        latency_ms=assistant_msg.latency_ms,
        pdf_url=pdf_url,
        created_at=assistant_msg.created_at.isoformat()
    )


@router.get("/api/chatbot/agents")
async def list_available_agents(
    current_user: Optional[dict] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """Lists all active Department Workspaces and Custom Agents for users to select."""
    await ensure_default_departments(db)
    stmt = select(RAGAgent).where(RAGAgent.is_active == True).order_by(RAGAgent.agent_key.asc())
    result = await db.scalars(stmt)
    agents = result.all()
    
    out = []
    for a in agents:
        out.append({
            "agent_key": a.agent_key,
            "name": a.name,
            "archetype_id": a.archetype_id,
            "archetype_title": a.archetype_title,
            "archetype_desc": a.archetype_desc,
            "archetype_icon": a.archetype_icon,
        })
    return out


@router.get("/api/chatbot/conversations", response_model=List[ConversationOut])
async def list_conversations(
    current_user: Optional[dict] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """Returns all conversations for the authenticated user."""
    if not current_user:
        return []
    user_id = str(current_user.get("sub") or "")
    stmt = (
        select(ChatConversation)
        .where(ChatConversation.user_id == user_id)
        .where(ChatConversation.is_global == False)
        .order_by(desc(ChatConversation.updated_at))
    )
    result = await db.scalars(stmt)
    conversations = result.all()

    out = []
    for conv in conversations:
        msg_stmt = (
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conv.id)
            .order_by(desc(ChatMessage.created_at))
        )
        msg_res = await db.scalars(msg_stmt)
        messages = msg_res.all()
        last_text = messages[0].content if messages else None

        out.append(ConversationOut(
            id=conv.id,
            title=conv.title,
            message_count=len(messages),
            last_message=last_text,
            created_at=conv.created_at.isoformat(),
            updated_at=conv.updated_at.isoformat()
        ))
    return out


@router.get("/api/chatbot/conversations/{id}")
async def get_conversation(
    id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Returns conversation details and its ordered messages."""
    user_id = str(current_user.get("sub") or "")
    user_role = str(current_user.get("role") or "").upper()
    
    conversation = await db.get(ChatConversation, id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation.user_id != user_id and user_role not in ("ADMIN", "SUPER_ADMIN"):
        raise HTTPException(status_code=403, detail="Access denied to this conversation")

    msg_stmt = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == id)
        .order_by(ChatMessage.created_at.asc())
    )
    msg_res = await db.scalars(msg_stmt)
    messages = msg_res.all()

    pdf_url = f"/api/chatbot/conversations/{conversation.id}/pdf"

    # Build Q&A history pairs for side panel
    qa_history = []
    curr_q = None
    curr_q_time = None
    for m in messages:
        if m.sender == "user":
            if curr_q:
                qa_history.append({"question": curr_q, "answer": "No answer recorded", "q_time": curr_q_time, "a_time": ""})
            curr_q = m.content
            curr_q_time = m.created_at.isoformat()
        elif m.sender == "assistant" and curr_q:
            qa_history.append({
                "question": curr_q,
                "answer": m.content,
                "q_time": curr_q_time,
                "a_time": m.created_at.isoformat()
            })
            curr_q = None
            curr_q_time = None

    if curr_q:
        qa_history.append({"question": curr_q, "answer": "Processing...", "q_time": curr_q_time, "a_time": ""})

    return {
        "id": conversation.id,
        "title": conversation.title,
        "pdf_url": pdf_url,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
        "qa_history": qa_history,
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "content": m.content,
                "retrieved_context": m.retrieved_context or [],
                "latency_ms": m.latency_ms,
                "created_at": m.created_at.isoformat()
            }
            for m in messages
        ]
    }


@router.get("/api/chatbot/conversations/{id}/pdf")
async def get_conversation_pdf(
    id: str,
    db: AsyncSession = Depends(get_db)
):
    """Serves or dynamically generates the PDF document for a conversation session."""
    from fastapi.responses import FileResponse
    from app.services.pdf_service import PDF_DIR, ConversationPDFService

    pdf_file = PDF_DIR / f"conversation_{id}.pdf"

    if not pdf_file.exists():
        conversation = await db.get(ChatConversation, id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation session not found.")

        msg_stmt = (
            select(ChatMessage)
            .where(ChatMessage.conversation_id == id)
            .order_by(ChatMessage.created_at.asc())
        )
        msg_res = await db.scalars(msg_stmt)
        all_msgs = msg_res.all()
        msg_dicts = [
            {
                "sender": m.sender,
                "content": m.content,
                "created_at": m.created_at.isoformat()
            }
            for m in all_msgs
        ]
        ConversationPDFService.generate_pdf(id, msg_dicts)

    if not pdf_file.exists():
        raise HTTPException(status_code=404, detail="PDF document could not be generated for this session.")

    return FileResponse(
        path=str(pdf_file),
        media_type="application/pdf",
        filename=f"conversation_{id}.pdf"
    )


@router.delete("/api/chatbot/conversations/{id}")
async def delete_conversation(
    id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a conversation."""
    user_id = str(current_user.get("sub") or "")
    user_role = str(current_user.get("role") or "").upper()

    conversation = await db.get(ChatConversation, id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if conversation.user_id != user_id and user_role not in ("ADMIN", "SUPER_ADMIN"):
        raise HTTPException(status_code=403, detail="Access denied")

    await db.delete(conversation)
    await db.commit()
    return {"success": True, "message": "Conversation deleted"}


# ---------------------------------------------------------------------------
# Admin RAG Agents & Department Workspace Endpoints
# ---------------------------------------------------------------------------

ADMIN_ROLES = ["ADMIN", "SUPER_ADMIN", "admin"]


@router.get("/api/admin/chatbot/agents")
async def list_rag_agents(
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Lists all Department Workspaces and Custom Agents with their document counts."""
    await ensure_default_departments(db)
    
    stmt = select(RAGAgent).order_by(RAGAgent.agent_key.asc())
    result = await db.scalars(stmt)
    agents = result.all()

    out = []
    for a in agents:
        # Count documents for this agent
        doc_count = await db.scalar(
            select(func.count(KnowledgeDocument.id)).where(KnowledgeDocument.agent_key == a.agent_key)
        ) or 0
        out.append({
            "id": a.id,
            "agent_key": a.agent_key,
            "name": a.name,
            "archetype_id": a.archetype_id,
            "archetype_title": a.archetype_title,
            "archetype_desc": a.archetype_desc,
            "archetype_icon": a.archetype_icon,
            "is_active": a.is_active,
            "system_prompt": a.system_prompt,
            "pdfs_count": doc_count,
            "created_at": a.created_at.isoformat()
        })
    return out


@router.post("/api/admin/chatbot/agents")
async def create_custom_agent(
    payload: CreateAgentPayload,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Creates a new Custom AI Agent."""
    # Find next agent_key
    max_key = await db.scalar(select(func.max(RAGAgent.agent_key))) or 900010
    new_key = max(max_key + 1, 900011)

    agent = RAGAgent(
        agent_key=new_key,
        name=payload.name,
        archetype_id=payload.archetype_id,
        archetype_title=payload.archetype_title or payload.name,
        archetype_desc=payload.archetype_desc or "Custom deployed agent",
        archetype_icon=payload.archetype_icon or "🤖",
        system_prompt=payload.system_prompt,
        is_active=True
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return {
        "success": True,
        "message": f"Custom agent '{agent.name}' deployed successfully.",
        "agent": {
            "id": agent.id,
            "agent_key": agent.agent_key,
            "name": agent.name,
            "archetype_id": agent.archetype_id,
            "archetype_title": agent.archetype_title,
            "archetype_desc": agent.archetype_desc,
            "archetype_icon": agent.archetype_icon,
            "is_active": agent.is_active
        }
    }


@router.patch("/api/admin/chatbot/agents/{agent_key}/toggle")
async def toggle_agent_active(
    agent_key: int,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Toggles active state for a department or custom agent."""
    res = await db.execute(select(RAGAgent).where(RAGAgent.agent_key == agent_key))
    agent = res.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.is_active = not agent.is_active
    await db.commit()
    return {"success": True, "agent_key": agent.agent_key, "is_active": agent.is_active}


@router.delete("/api/admin/chatbot/agents/{agent_key}")
async def delete_custom_agent(
    agent_key: int,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a custom agent and all associated documents."""
    if agent_key <= 900010:
        raise HTTPException(status_code=400, detail="Default department agents cannot be deleted.")
    
    res = await db.execute(select(RAGAgent).where(RAGAgent.agent_key == agent_key))
    agent = res.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Delete docs
    docs_res = await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.agent_key == agent_key))
    for doc in docs_res.scalars().all():
        await DocumentProcessingService.delete_document(db, doc.id)

    await db.delete(agent)
    await db.commit()
    return {"success": True, "message": f"Agent {agent_key} deleted."}


@router.get("/api/admin/chatbot/library/{agent_key}")
async def list_agent_library(
    agent_key: int,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Lists all documents uploaded for a specific agent key."""
    stmt = (
        select(KnowledgeDocument)
        .where(KnowledgeDocument.agent_key == agent_key)
        .order_by(desc(KnowledgeDocument.created_at))
    )
    res = await db.scalars(stmt)
    docs = list(res.all())

    # Automatically clean up legacy duplicate auto-learned documents
    auto_docs = [d for d in docs if "Auto-Learned" in (d.title or "")]
    if len(auto_docs) > 1:
        primary_doc = auto_docs[0]
        primary_doc.title = f"Auto-Learned Knowledge Base (Agent {agent_key})"
        for duplicate in auto_docs[1:]:
            await DocumentProcessingService.delete_document(db, duplicate.id)
        
        # Refresh document list
        res = await db.scalars(stmt)
        docs = list(res.all())
    return {
        "files": [
            {
                "id": d.id,
                "filename": d.filename or d.title,
                "title": d.title,
                "file_type": d.file_type,
                "file_size": d.file_size,
                "status": d.status,
                "chunk_count": d.chunk_count,
                "error_message": d.error_message,
                "created_at": d.created_at.isoformat()
            }
            for d in docs
        ]
    }


@router.post("/api/admin/chatbot/library/upload-multiple")
async def upload_multiple_for_agent(
    pdfs: List[UploadFile] = File(...),
    agentKey: int = Form(...),
    current_user: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Uploads multiple files and scopes them directly to the specified agentKey."""
    user_id = str(current_user.get("sub") or "")
    processed_count = 0

    for file in pdfs:
        filename = file.filename or "uploaded_doc"
        ext = Path(filename).suffix.lower()
        file_type_map = {".pdf": "PDF", ".docx": "DOCX", ".doc": "DOCX", ".txt": "TXT", ".csv": "CSV", ".md": "MD"}
        if ext not in file_type_map:
            continue

        doc_id = str(uuid.uuid4())
        safe_filename = f"{doc_id}_{filename}"
        file_path = str(UPLOAD_DIR / safe_filename)

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        file_size = os.path.getsize(file_path)

        doc = KnowledgeDocument(
            id=doc_id,
            agent_key=agentKey,
            title=Path(filename).stem,
            filename=filename,
            file_path=file_path,
            file_type=file_type_map[ext],
            file_size=file_size,
            status="UPLOADED",
            created_by_user_id=user_id
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)

        await DocumentProcessingService.process_document(db, doc.id)
        processed_count += 1

    return {"success": True, "message": f"Uploaded and indexed {processed_count} files for agent {agentKey}."}


@router.get("/api/admin/chatbot/rag-misses/{agent_key}")
async def list_rag_misses(
    agent_key: int,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Returns gap questions where RAG lacked context for this agent."""
    stmt = (
        select(RAGMiss)
        .where(RAGMiss.agent_key == agent_key, RAGMiss.resolved == False)
        .order_by(desc(RAGMiss.created_at))
    )
    res = await db.scalars(stmt)
    misses = res.all()
    return {
        "misses": [
            {
                "id": m.id,
                "agent_key": m.agent_key,
                "question": m.question,
                "created_at": m.created_at.isoformat()
            }
            for m in misses
        ]
    }


@router.post("/api/admin/chatbot/rag-misses/{miss_id}/resolve")
async def resolve_rag_miss(
    miss_id: str,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Marks a gap question as resolved and auto-generates a learned Q&A cache entry."""
    miss = await db.get(RAGMiss, miss_id)
    if miss:
        miss.resolved = True
        miss.resolved_at = func.now()
        await db.commit()

        try:
            from app.services.gemini_service import generate_answer
            ans = await generate_answer(
                query=miss.question,
                retrieved_chunks=[],
                system_prompt="Answer this question factually and concisely.",
                db=db
            )
            if ans and miss.agent_key:
                # Add to RAGCache
                cache_q = miss.question.strip()[:500]
                existing_cache = await db.scalar(
                    select(RAGCache).where(
                        RAGCache.agent_key == miss.agent_key,
                        RAGCache.question.ilike(f"%{cache_q}%")
                    )
                )
                if not existing_cache:
                    new_cache = RAGCache(
                        agent_key=miss.agent_key,
                        question=cache_q,
                        answer=ans,
                        source="resolved_gap"
                    )
                    db.add(new_cache)

                # Append to SINGLE consolidated Auto-Learned Document for Agent Library
                await DocumentProcessingService.append_auto_learned_qa(
                    db=db,
                    agent_key=miss.agent_key,
                    question=miss.question,
                    answer=ans
                )
        except Exception as e:
            logger.warning(f"Could not auto-generate answer for resolved miss {miss_id}: {e}")

    return {"success": True}


@router.get("/api/admin/chatbot/rag-cache/{agent_key}")
async def list_rag_cache(
    agent_key: int,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Returns auto-learned / cached Q&A entries for this agent."""
    stmt = (
        select(RAGCache)
        .where(RAGCache.agent_key == agent_key)
        .order_by(desc(RAGCache.created_at))
    )
    res = await db.scalars(stmt)
    caches = res.all()
    return {
        "cache": [
            {
                "id": c.id,
                "agent_key": c.agent_key,
                "question": c.question,
                "answer": c.answer,
                "source": c.source,
                "created_at": c.created_at.isoformat()
            }
            for c in caches
        ]
    }


@router.post("/api/admin/chatbot/rag-cache")
async def add_rag_cache(
    payload: CreateCachePayload,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Manually adds a learned Q&A entry into the cache."""
    cache_entry = RAGCache(
        agent_key=payload.agent_key,
        question=payload.question,
        answer=payload.answer,
        source=payload.source
    )
    db.add(cache_entry)
    await db.commit()
    return {"success": True, "message": "Learned Q&A entry saved."}


class UpdateCachePayload(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None

@router.patch("/api/admin/chatbot/rag-cache/{id}")
async def update_rag_cache(
    id: str,
    payload: UpdateCachePayload,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Updates a cached Q&A entry question and/or answer."""
    entry = await db.get(RAGCache, id)
    if not entry:
        raise HTTPException(status_code=404, detail="Cache entry not found")
    if payload.question is not None and payload.question.strip():
        entry.question = payload.question.strip()
    if payload.answer is not None and payload.answer.strip():
        entry.answer = payload.answer.strip()
    await db.commit()
    return {"success": True, "message": "Learned Q&A entry updated."}


@router.delete("/api/admin/chatbot/rag-cache/{id}")
async def delete_rag_cache(
    id: str,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a cached Q&A entry."""
    entry = await db.get(RAGCache, id)
    if entry:
        await db.delete(entry)
        await db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# Global Document & RAG Management Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/admin/chatbot/documents")
async def list_admin_documents(
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Lists all knowledge base documents with processing status and stats."""
    stmt = select(KnowledgeDocument).order_by(desc(KnowledgeDocument.created_at))
    result = await db.scalars(stmt)
    documents = result.all()

    return [
        {
            "id": doc.id,
            "title": doc.title,
            "filename": doc.filename,
            "file_type": doc.file_type,
            "file_size": doc.file_size,
            "status": doc.status,
            "agent_key": doc.agent_key,
            "department": doc.department,
            "chunk_count": doc.chunk_count,
            "error_message": doc.error_message,
            "created_at": doc.created_at.isoformat(),
            "updated_at": doc.updated_at.isoformat()
        }
        for doc in documents
    ]


@router.post("/api/admin/chatbot/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    agent_key: Optional[int] = Form(None),
    current_user: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Uploads a supported document (PDF, DOCX, TXT, CSV) and indexes it."""
    user_id = str(current_user.get("sub") or "")
    filename = file.filename or "uploaded_file"
    ext = Path(filename).suffix.lower()
    
    file_type_map = {".pdf": "PDF", ".docx": "DOCX", ".doc": "DOCX", ".txt": "TXT", ".csv": "CSV", ".md": "MD"}
    if ext not in file_type_map:
        raise HTTPException(
            status_code=400, 
            detail=f"Unsupported file format '{ext}'. Supported formats: PDF, DOCX, TXT, CSV, MD."
        )

    doc_id = str(uuid.uuid4())
    safe_filename = f"{doc_id}_{filename}"
    file_path = str(UPLOAD_DIR / safe_filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    file_size = os.path.getsize(file_path)

    doc_title = title.strip() if title and title.strip() else Path(filename).stem

    doc = KnowledgeDocument(
        id=doc_id,
        agent_key=agent_key,
        title=doc_title,
        filename=filename,
        file_path=file_path,
        file_type=file_type_map[ext],
        file_size=file_size,
        status="UPLOADED",
        created_by_user_id=user_id
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    processed_doc = await DocumentProcessingService.process_document(db, doc.id)

    return {
        "success": True,
        "message": f"Document '{doc_title}' uploaded and indexed successfully.",
        "document": {
            "id": processed_doc.id,
            "title": processed_doc.title,
            "status": processed_doc.status,
            "chunk_count": processed_doc.chunk_count,
            "error_message": processed_doc.error_message
        }
    }


@router.post("/api/admin/chatbot/documents/manual")
async def create_manual_knowledge(
    payload: ManualDocumentCreate,
    current_user: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Creates a manual knowledge base entry and processes it into searchable chunks."""
    user_id = str(current_user.get("sub") or "")
    doc = await DocumentProcessingService.create_manual_document(
        db=db,
        title=payload.title,
        content=payload.content,
        user_id=user_id
    )
    if payload.agent_key:
        doc.agent_key = payload.agent_key
        # also update chunk agent keys
        await db.execute(update(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id).values(agent_key=payload.agent_key))
        await db.commit()

    return {
        "success": True,
        "message": f"Knowledge entry '{payload.title}' created and indexed.",
        "document": {
            "id": doc.id,
            "title": doc.title,
            "status": doc.status,
            "chunk_count": doc.chunk_count,
            "error_message": doc.error_message
        }
    }


@router.get("/api/admin/chatbot/documents/{id}")
async def get_admin_document_details(
    id: str,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Returns document metadata and its generated text chunks."""
    doc = await db.get(KnowledgeDocument, id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    chunk_stmt = select(KnowledgeChunk).where(KnowledgeChunk.document_id == id).order_by(KnowledgeChunk.chunk_index.asc())
    chunk_res = await db.scalars(chunk_stmt)
    chunks = chunk_res.all()

    return {
        "id": doc.id,
        "title": doc.title,
        "filename": doc.filename,
        "file_type": doc.file_type,
        "file_size": doc.file_size,
        "status": doc.status,
        "error_message": doc.error_message,
        "chunk_count": doc.chunk_count,
        "created_at": doc.created_at.isoformat(),
        "chunks": [
            {
                "id": c.id,
                "chunk_index": c.chunk_index,
                "text": c.text,
                "token_count": c.token_count,
                "page": (c.meta_info or {}).get("page", 1),
                "has_embedding": bool(c.embedding)
            }
            for c in chunks
        ]
    }


class UpdateDocPayload(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


class UpdateDocContentPayload(BaseModel):
    title: Optional[str] = None
    content: str


class UpdateChunkPayload(BaseModel):
    text: str


@router.get("/api/admin/chatbot/documents/{id}/download")
async def download_admin_document(
    id: str,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Downloads the document file."""
    from fastapi.responses import FileResponse
    doc = await db.get(KnowledgeDocument, id)
    if not doc or not doc.file_path or not os.path.exists(doc.file_path):
        raise HTTPException(status_code=404, detail="Document file not found")
    
    return FileResponse(
        path=doc.file_path,
        filename=doc.filename or f"{doc.title}.pdf",
        media_type="application/octet-stream"
    )


@router.get("/api/admin/chatbot/documents/{id}/content")
async def get_admin_document_content(
    id: str,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Returns the full text content of a document."""
    doc = await db.get(KnowledgeDocument, id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    content = ""
    if doc.file_path and os.path.exists(doc.file_path) and doc.file_path.endswith((".txt", ".md", ".csv")):
        try:
            with open(doc.file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            pass

    if not content:
        chunks_res = await db.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == id)
            .order_by(KnowledgeChunk.chunk_index)
        )
        content = "\n\n".join([c.text for c in chunks_res.all() if c.text])

    return {"id": doc.id, "title": doc.title, "content": content}


@router.put("/api/admin/chatbot/documents/{id}/content")
async def update_admin_document_content(
    id: str,
    payload: UpdateDocContentPayload,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Updates full document text content, re-chunks, and re-generates vector embeddings."""
    import json
    from app.services.gemini_service import generate_embedding

    doc = await db.get(KnowledgeDocument, id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    
    if payload.title and payload.title.strip():
        doc.title = payload.title.strip()

    new_content = payload.content.strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="Content cannot be empty")

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in doc.title).strip() or "document"
    file_path = str(UPLOAD_DIR / f"{doc.id}_{safe_title}.txt")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    
    doc.file_path = file_path
    if doc.file_type not in ["PDF", "DOCX"]:
        doc.file_type = "TXT"
    doc.file_size = len(new_content.encode("utf-8"))
    doc.status = "PROCESSING"
    await db.commit()

    # Delete existing chunks
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == id))
    await db.commit()

    # Re-chunk and re-embed
    chunks_text = DocumentProcessingService.chunk_text(new_content, chunk_size=800, overlap=120)
    created_chunks = []
    for idx, chunk_text in enumerate(chunks_text):
        if not chunk_text.strip():
            continue
        emb_vector = generate_embedding(chunk_text)
        emb_json = json.dumps(emb_vector)

        chunk = KnowledgeChunk(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            business_id=doc.business_id,
            agent_key=doc.agent_key,
            chunk_index=idx,
            text=chunk_text,
            token_count=len(chunk_text.split()),
            embedding=emb_json,
            meta_info={"source": "manual_edit", "edited": True}
        )
        db.add(chunk)
        created_chunks.append(chunk)

    doc.chunk_count = len(created_chunks)
    doc.status = "COMPLETED"
    doc.error_message = None
    await db.commit()
    await db.refresh(doc)

    return {
        "success": True,
        "message": f"Document '{doc.title}' content updated and {doc.chunk_count} vector chunks re-embedded successfully.",
        "chunk_count": doc.chunk_count
    }


@router.patch("/api/admin/chatbot/chunks/{chunk_id}")
async def update_admin_chunk(
    chunk_id: str,
    payload: UpdateChunkPayload,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Updates a single chunk text and re-generates its vector embedding."""
    import json
    from app.services.gemini_service import generate_embedding

    chunk = await db.get(KnowledgeChunk, chunk_id)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    
    new_text = payload.text.strip()
    if not new_text:
        raise HTTPException(status_code=400, detail="Chunk text cannot be empty")

    emb_vector = generate_embedding(new_text)
    chunk.text = new_text
    chunk.token_count = len(new_text.split())
    chunk.embedding = json.dumps(emb_vector)
    await db.commit()

    return {"success": True, "message": "Chunk updated and re-embedded successfully."}


@router.patch("/api/admin/chatbot/documents/{id}")
async def update_admin_document_title(
    id: str,
    payload: UpdateDocPayload,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Updates document title and/or content."""
    doc = await db.get(KnowledgeDocument, id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if payload.title is not None and payload.title.strip():
        doc.title = payload.title.strip()
        await db.commit()
    return {"success": True, "message": "Document updated."}


@router.delete("/api/admin/chatbot/documents/{id}")
async def delete_admin_document(
    id: str,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a document and all associated chunks."""
    success = await DocumentProcessingService.delete_document(db, id)
    if not success:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"success": True, "message": "Document and chunks successfully removed."}


@router.post("/api/admin/chatbot/documents/{id}/reprocess")
async def reprocess_admin_document(
    id: str,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Re-runs text extraction, chunking, and embedding generation for a document."""
    doc = await DocumentProcessingService.process_document(db, id)
    return {
        "success": True,
        "message": f"Document '{doc.title}' reprocessed successfully.",
        "status": doc.status,
        "chunk_count": doc.chunk_count,
        "error_message": doc.error_message
    }


@router.get("/api/admin/chatbot/chunks")
async def list_admin_chunks(
    q: Optional[str] = Query(None, description="Search term"),
    document_id: Optional[str] = Query(None),
    agent_key: Optional[int] = Query(None),
    limit: int = Query(50, le=200),
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Inspects text chunks across the knowledge base."""
    stmt = select(KnowledgeChunk).order_by(KnowledgeChunk.created_at.desc())
    if document_id:
        stmt = stmt.where(KnowledgeChunk.document_id == document_id)
    if agent_key:
        stmt = stmt.where(KnowledgeChunk.agent_key == agent_key)
    if q:
        stmt = stmt.where(KnowledgeChunk.text.ilike(f"%{q}%"))
    stmt = stmt.limit(limit)

    result = await db.scalars(stmt)
    chunks = result.all()

    return [
        {
            "id": c.id,
            "document_id": c.document_id,
            "agent_key": c.agent_key,
            "chunk_index": c.chunk_index,
            "text": c.text,
            "token_count": c.token_count,
            "meta_info": c.meta_info,
            "created_at": c.created_at.isoformat()
        }
        for c in chunks
    ]

class ChunkUpdatePayload(BaseModel):
    text: str

@router.patch("/api/admin/chatbot/chunks/{id}")
async def update_admin_chunk(
    id: str,
    payload: ChunkUpdatePayload,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Updates the text content of a knowledge chunk and recomputes its embedding."""
    chunk = await db.get(KnowledgeChunk, id)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")

    from app.services.gemini_service import GeminiService
    # Generate new embedding for the updated text
    embedding = await GeminiService.generate_embedding(payload.text)
    
    chunk.text = payload.text
    chunk.embedding = embedding
    chunk.token_count = len(payload.text.split())

    await db.commit()
    return {"success": True, "message": "Chunk updated successfully"}

@router.delete("/api/admin/chatbot/chunks/{id}")
async def delete_admin_chunk(
    id: str,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a specific knowledge chunk."""
    chunk = await db.get(KnowledgeChunk, id)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")

    # Decrease the chunk count of the associated document
    doc = await db.get(KnowledgeDocument, chunk.document_id)
    if doc and doc.chunk_count > 0:
        doc.chunk_count -= 1

    await db.delete(chunk)
    await db.commit()
    return {"success": True, "message": "Chunk deleted successfully"}


@router.get("/api/admin/chatbot/config")
async def get_admin_config(
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Retrieves current Chatbot and RAG configuration."""
    config = await RAGService.get_config(db)
    return {
        "id": config.id,
        "model_name": config.model_name,
        "temperature": config.temperature,
        "top_k": config.top_k,
        "similarity_threshold": config.similarity_threshold,
        "system_prompt": config.system_prompt,
        "fallback_message": config.fallback_message
    }


@router.put("/api/admin/chatbot/config")
async def update_admin_config(
    payload: ConfigUpdate,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Updates Chatbot and RAG configuration."""
    config = await RAGService.get_config(db)
    if payload.model_name is not None:
        config.model_name = payload.model_name
    if payload.temperature is not None:
        config.temperature = payload.temperature
    if payload.top_k is not None:
        config.top_k = payload.top_k
    if payload.similarity_threshold is not None:
        config.similarity_threshold = payload.similarity_threshold
    if payload.system_prompt is not None:
        config.system_prompt = payload.system_prompt
    if payload.fallback_message is not None:
        config.fallback_message = payload.fallback_message

    await db.commit()
    await db.refresh(config)
    return {
        "success": True,
        "message": "Chatbot configuration updated successfully.",
        "config": {
            "id": config.id,
            "model_name": config.model_name,
            "temperature": config.temperature,
            "top_k": config.top_k,
            "similarity_threshold": config.similarity_threshold,
            "system_prompt": config.system_prompt,
            "fallback_message": config.fallback_message
        }
    }


@router.get("/api/admin/chatbot/history")
async def list_admin_chat_history(
    limit: int = Query(50, le=100),
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Admin inspection of conversations across all users."""
    conv_stmt = select(ChatConversation).order_by(desc(ChatConversation.updated_at)).limit(limit)
    conv_res = await db.scalars(conv_stmt)
    conversations = conv_res.all()

    history = []
    for conv in conversations:
        user = await db.get(User, conv.user_id)
        msg_stmt = select(ChatMessage).where(ChatMessage.conversation_id == conv.id).order_by(ChatMessage.created_at.asc())
        msg_res = await db.scalars(msg_stmt)
        messages = msg_res.all()

        history.append({
            "id": conv.id,
            "title": conv.title,
            "user_id": conv.user_id,
            "user_name": user.full_name if user else "Unknown User",
            "user_email": user.email if user else "",
            "user_role": user.role if user else "USER",
            "message_count": len(messages),
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
            "messages": [
                {
                    "id": m.id,
                    "sender": m.sender,
                    "content": m.content,
                    "retrieved_context": m.retrieved_context or [],
                    "latency_ms": m.latency_ms,
                    "created_at": m.created_at.isoformat()
                }
                for m in messages
            ]
        })

    return history


@router.get("/api/admin/chatbot/analytics")
async def get_admin_analytics(
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Returns analytics for the CallZenza AI Chatbot & RAG knowledge base."""
    total_docs = await db.scalar(select(func.count(KnowledgeDocument.id))) or 0
    completed_docs = await db.scalar(select(func.count(KnowledgeDocument.id)).where(KnowledgeDocument.status == "COMPLETED")) or 0
    total_chunks = await db.scalar(select(func.count(KnowledgeChunk.id))) or 0
    total_convs = await db.scalar(select(func.count(ChatConversation.id))) or 0
    total_msgs = await db.scalar(select(func.count(ChatMessage.id))) or 0
    user_queries = await db.scalar(select(func.count(ChatMessage.id)).where(ChatMessage.sender == "user")) or 0
    active_users = await db.scalar(select(func.count(func.distinct(ChatConversation.user_id)))) or 0
    total_agents = await db.scalar(select(func.count(RAGAgent.id))) or 0

    return {
        "total_documents": total_docs,
        "completed_documents": completed_docs,
        "total_chunks": total_chunks,
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "total_user_queries": user_queries,
        "active_users": active_users,
        "total_agents": total_agents
    }


@router.post("/api/admin/chatbot/test-rag")
async def test_rag_sandbox(
    payload: TestRAGRequest,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Sandbox endpoint for administrators to test vector retrieval and answer generation."""
    rag_result = await RAGService.answer_question(db, payload.query, agent_key=payload.agent_key)
    return rag_result


# ---------------------------------------------------------------------------
# Bulk Delete Endpoints (Topic Level Clear All)
# ---------------------------------------------------------------------------

@router.delete("/api/admin/chatbot/documents/delete-all")
async def delete_all_admin_documents(
    agent_key: Optional[int] = Query(None),
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes all knowledge documents and associated chunks, optionally filtered by agent_key."""
    stmt = select(KnowledgeDocument)
    if agent_key:
        stmt = stmt.where(KnowledgeDocument.agent_key == agent_key)
    res = await db.scalars(stmt)
    docs = res.all()
    count = len(docs)
    for doc in docs:
        await DocumentProcessingService.delete_document(db, doc.id)
    return {"success": True, "message": f"Successfully deleted {count} document(s) and their associated chunks.", "count": count}


@router.delete("/api/admin/chatbot/chunks/delete-all")
async def delete_all_admin_chunks(
    agent_key: Optional[int] = Query(None),
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes all knowledge chunks, optionally filtered by agent_key."""
    stmt = delete(KnowledgeChunk)
    if agent_key:
        stmt = stmt.where(KnowledgeChunk.agent_key == agent_key)
        await db.execute(update(KnowledgeDocument).where(KnowledgeDocument.agent_key == agent_key).values(chunk_count=0))
    else:
        await db.execute(update(KnowledgeDocument).values(chunk_count=0))
    
    res = await db.execute(stmt)
    await db.commit()
    return {"success": True, "message": "Successfully deleted all chunks.", "count": res.rowcount}


@router.delete("/api/admin/chatbot/history/delete-all")
async def delete_all_admin_history(
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes all user conversations and chat messages."""
    await db.execute(delete(ChatMessage))
    res = await db.execute(delete(ChatConversation))
    await db.commit()
    return {"success": True, "message": "Successfully cleared all chat history.", "count": res.rowcount}


@router.delete("/api/admin/chatbot/rag-misses/{agent_key}/delete-all")
async def delete_all_admin_rag_misses(
    agent_key: int,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes all gap questions for a specific agent workspace."""
    res = await db.execute(delete(RAGMiss).where(RAGMiss.agent_key == agent_key))
    await db.commit()
    return {"success": True, "message": "Successfully cleared all gap questions."}


@router.delete("/api/admin/chatbot/rag-cache/{agent_key}/delete-all")
async def delete_all_admin_rag_cache(
    agent_key: int,
    _: dict = Depends(require_role(*ADMIN_ROLES)),
    db: AsyncSession = Depends(get_db)
):
    """Deletes all learned cache entries for a specific agent workspace."""
    res = await db.execute(delete(RAGCache).where(RAGCache.agent_key == agent_key))
    await db.commit()
    return {"success": True, "message": "Successfully cleared all QA cache entries."}

