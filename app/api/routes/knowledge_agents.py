"""
Knowledge Agents API Routes.
Manages scoped Agent RAG libraries, universal script uploads, and conversational Q&A via AgentEngine.
"""
from __future__ import annotations

import json
import re
import time
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.database import get_db
from app.db.models import Agent, AIAgent, AgentSession, KnowledgeLibrary, Lead
from app.schemas.knowledge_agent import AgentCreate, AgentOut, AskRequest, AskResponse, ScriptUploadPayload
from app.services.ai.agent_engine import AgentEngine
from app.services.ai.rag.embedding_service import generate_text_embedding

router = APIRouter(prefix="/api/knowledge-agents", tags=["knowledge-agents"])


def generate_chunk_embedding(text: str) -> str | None:
    """Generates a text embedding and returns it as a JSON-serialized float list."""
    if not text or not text.strip():
        return None
    vec = generate_text_embedding(text)
    if vec:
        return json.dumps(vec)
    return None


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(payload: AgentCreate, db: AsyncSession = Depends(get_db)) -> Agent:
    agent = Agent(
        name=payload.name,
        description=payload.description or "",
        system_prompt=payload.system_prompt or ""
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return agent


@router.get("", response_model=list[AgentOut])
async def list_agents(db: AsyncSession = Depends(get_db)) -> list[Agent]:
    # Sync all AIAgents to Agent table to ensure they show up in RAG and satisfy FK constraints
    ai_agents_result = await db.scalars(select(AIAgent))
    ai_agents = ai_agents_result.all()
    for ai_agent in ai_agents:
        existing = await db.get(Agent, ai_agent.id)
        if not existing:
            new_agent = Agent(
                id=ai_agent.id,
                name=ai_agent.name,
                description=ai_agent.description or "",
                system_prompt=ai_agent.system_prompt or "",
                created_at=ai_agent.created_at
            )
            db.add(new_agent)
    
    await db.commit()

    stmt = select(Agent).order_by(Agent.created_at.desc())
    result = await db.scalars(stmt)
    return list(result.all())


@router.get("/{id}", response_model=AgentOut)
async def get_agent(id: str, db: AsyncSession = Depends(get_db)) -> Agent:
    agent = await db.get(Agent, id)
    if not agent:
        ai_agent = await db.get(AIAgent, id)
        if ai_agent:
            return Agent(
                id=ai_agent.id,
                name=ai_agent.name,
                description=ai_agent.description or "",
                system_prompt=ai_agent.system_prompt or "",
                created_at=ai_agent.created_at
            )
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.post("/{id}/ask", response_model=AskResponse)
async def ask_agent(
    id: str,
    payload: AskRequest,
    db: AsyncSession = Depends(get_db)
):
    agent = await db.get(Agent, id)
    if not agent:
        ai_agent = await db.get(AIAgent, id)
        if not ai_agent:
            raise HTTPException(status_code=404, detail="Agent not found")

    session_id = payload.session_id
    if not session_id or not session_id.strip():
        session = AgentSession(agent_id=id)
        db.add(session)
        await db.flush()
        session_id = session.id

    lead = None
    if payload.lead_id:
        lead = await db.get(Lead, payload.lead_id)

    # If greeting turn and lead exists, run personalized intro
    if payload.question == "hello / greeting / script start" and lead:
        from app.services.lead_engagement_service import generate_personalized_greeting
        from app.utils.tamil_sanitizer import extract_spoken_dialogue
        ai_ag = await db.get(AIAgent, id)
        raw_opening = await generate_personalized_greeting(db, lead, ai_ag)
        response = extract_spoken_dialogue(raw_opening, is_greeting=True)
        return AskResponse(
            answer=response,
            session_id=session_id,
            retrieved_context=""
        )

    # Process through central AgentEngine
    engine_res = await AgentEngine.process_user_query(
        db=db,
        agent_id=id,
        message=payload.question,
        session_id=session_id,
        conversation_id=session_id,
    )

    # Record turn to database
    try:
        turn_entry = KnowledgeLibrary(
            agent_id=id,
            session_id=session_id,
            question=payload.question,
            answer=engine_res.response,
            source_data="Dialogue State Tracking"
        )
        db.add(turn_entry)
        await db.commit()
    except Exception as e:
        logger.warning(f"[KnowledgeAgents] Could not save turn history: {e}")

    retrieved_text = "\n".join([
        f"Information: {c.get('text', '')}" for c in engine_res.retrieved_context if c.get("text")
    ])

    return AskResponse(
        answer=engine_res.response,
        session_id=session_id,
        retrieved_context=retrieved_text
    )


@router.post("/{id}/upload-script")
async def upload_agent_script(
    id: str,
    payload: ScriptUploadPayload,
    is_emotion: bool = False,
    db: AsyncSession = Depends(get_db)
):
    """
    Universally parses and chunks any script or knowledge document uploaded for this Agent.
    Strictly isolated per (business_id, agent_id, version_id) with automated versioning.
    """
    agent = await db.get(Agent, id)
    ai_ag = await db.get(AIAgent, id)
    if not agent and not ai_ag:
        raise HTTPException(status_code=404, detail="Agent not found")

    business_id = getattr(ai_ag, "business_id", None) or getattr(agent, "business_id", None)
    text_content = payload.script_text.strip()
    if not text_content:
        raise HTTPException(status_code=400, detail="Script content is empty")

    filename = payload.filename or "uploaded_script"
    version_id = f"v_{int(time.time())}"
    logger.info(f"--- Ingesting Script for Agent {id} (File: {filename}, Version: {version_id}, Length: {len(text_content)}) ---")

    # Versioning: Mark all existing active chunks for this agent as inactive before indexing new active version
    try:
        from sqlalchemy import update
        deactivate_stmt = (
            update(KnowledgeLibrary)
            .where(KnowledgeLibrary.agent_id == id)
            .values(is_active=False)
        )
        await db.execute(deactivate_stmt)
    except Exception as e:
        logger.warning(f"Could not deactivate old versions for agent {id}: {e}")

    entries = []
    parsed_qa = []

    # Strategy 1: Explicit Q: / A: pairings
    qa_blocks = re.split(r'(?i)\n*(?:question|q)\s*:', text_content)
    if len(qa_blocks) > 1:
        for block in qa_blocks:
            block = block.strip()
            if not block:
                continue
            parts = re.split(r'(?i)\n*(?:answer|a)\s*:', block, maxsplit=1)
            if len(parts) == 2:
                q = parts[0].strip()
                a = parts[1].strip()
                if q and a:
                    parsed_qa.append((q, a))

    # Strategy 2: Role-based dialogues (Customer / Agent turns)
    if not parsed_qa:
        lines = [l.strip() for l in re.split(r'(?i)(?=(?:customer|prospect|user|client|caller|receiver|agent|bot|rep|assistant|representative)\s*:)', text_content) if l.strip()]
        role_pattern = re.compile(r'^(customer|prospect|user|client|caller|receiver|agent|bot|rep|assistant|representative)\s*:\s*(.*)$', re.IGNORECASE)
        
        parsed_lines = []
        for line in lines:
            m = role_pattern.match(line)
            if m:
                role = m.group(1).lower()
                text = m.group(2).strip()
                parsed_lines.append({"role": role, "text": text})
            else:
                if parsed_lines:
                    parsed_lines[-1]["text"] += "\n" + line

        if parsed_lines:
            for i, turn in enumerate(parsed_lines):
                role = turn["role"]
                text = turn["text"]
                if role in ["customer", "prospect", "user", "client", "caller", "receiver"]:
                    next_agent_text = ""
                    for next_turn in parsed_lines[i+1:]:
                        if next_turn["role"] in ["agent", "bot", "rep", "assistant", "representative"]:
                            next_agent_text = next_turn["text"]
                            break
                        elif next_turn["role"] in ["customer", "prospect", "user", "client", "caller", "receiver"]:
                            break
                    if text and next_agent_text:
                        parsed_qa.append((text, next_agent_text))
                elif i == 0 and role in ["agent", "bot", "rep", "assistant", "representative"]:
                    parsed_qa.append(("hello / greeting / script start", text))

    # Store parsed Q&A entries
    if parsed_qa:
        for q, a in parsed_qa:
            emb = generate_chunk_embedding(f"Question: {q}\nAnswer: {a}")
            entry = KnowledgeLibrary(
                business_id=business_id,
                agent_id=id,
                version_id=version_id,
                is_active=True,
                question=q,
                answer=a,
                embedding=emb,
                source_data="Uploaded Script (Q&A / Dialogue)"
            )
            db.add(entry)
            entries.append(entry)
    else:
        # Strategy 3: Paragraph / Section Chunks
        paragraphs = [p.strip() for p in text_content.split("\n\n") if p.strip()]
        if len(paragraphs) <= 1:
            paragraphs = [p.strip() for p in text_content.split("\n") if p.strip()]
            
        for i, paragraph in enumerate(paragraphs):
            snippet = paragraph.split("\n")[0][:80]
            if len(paragraph) > 80:
                snippet += "..."
            label = "Emotion guideline" if is_emotion else "Script chunk"
            question_placeholder = f"{label} {i+1}: {snippet}"
            emb = None if is_emotion else generate_chunk_embedding(paragraph)
            entry = KnowledgeLibrary(
                business_id=business_id,
                agent_id=id,
                version_id=version_id,
                is_active=True,
                question=question_placeholder,
                answer=paragraph,
                embedding=emb,
                source_data="Uploaded Emotion Guidelines" if is_emotion else "Uploaded Script (Chunk)"
            )
            db.add(entry)
            entries.append(entry)

    await db.commit()
    logger.info(f"Ingested {len(entries)} chunks for Agent {id} (version {version_id})")
    return {"status": "success", "count": len(entries), "version_id": version_id}



@router.delete("/{id}/clear-library", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def clear_agent_library(id: str, is_emotion: bool = False, db: AsyncSession = Depends(get_db)) -> Response:
    stmt = delete(KnowledgeLibrary).where(KnowledgeLibrary.agent_id == id)
    if is_emotion:
        stmt = stmt.where(KnowledgeLibrary.source_data == "Uploaded Emotion Guidelines")
    else:
        stmt = stmt.where(KnowledgeLibrary.source_data != "Uploaded Emotion Guidelines")
    await db.execute(stmt)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
