from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import KnowledgeLibrary, KnowledgeSource, KnowledgeMetadata
from app.schemas.knowledge_library import KnowledgeCreate, KnowledgeOut
from app.services.retrieval_service import RetrievalService

router = APIRouter(prefix="/api/library", tags=["knowledge-library"])


@router.post("/knowledge", response_model=KnowledgeOut, status_code=status.HTTP_201_CREATED)
async def create_knowledge_entry(payload: KnowledgeCreate, db: AsyncSession = Depends(get_db)) -> KnowledgeLibrary:
    is_dup = await RetrievalService.is_duplicate(db, payload.agent_id, payload.question)
    if is_dup:
        raise HTTPException(status_code=400, detail="Duplicate knowledge question already exists")

    entry = KnowledgeLibrary(
        agent_id=payload.agent_id,
        session_id=payload.session_id,
        question=payload.question,
        answer=payload.answer,
        source_data=payload.source_data
    )
    db.add(entry)
    await db.flush()  # Generate entry.id

    # Create associated sources
    if payload.sources:
        for source in payload.sources:
            src = KnowledgeSource(
                knowledge_id=entry.id,
                source_type=source.get("source_type"),
                content=source.get("content")
            )
            db.add(src)

    # Create associated metadata
    if payload.metadata:
        for key, val in payload.metadata.items():
            meta = KnowledgeMetadata(
                knowledge_id=entry.id,
                meta_key=key,
                meta_value=str(val)
            )
            db.add(meta)

    await db.commit()
    await db.refresh(entry)
    return entry


@router.get("/knowledge", response_model=list[KnowledgeOut])
async def list_knowledge_entries(db: AsyncSession = Depends(get_db)) -> list[KnowledgeLibrary]:
    stmt = select(KnowledgeLibrary).order_by(KnowledgeLibrary.created_at.desc())
    result = await db.scalars(stmt)
    return list(result.all())


@router.get("/search", response_model=list[KnowledgeOut])
async def search_library(q: str = Query(..., min_length=1), db: AsyncSession = Depends(get_db)) -> list[KnowledgeLibrary]:
    return await RetrievalService.search_knowledge(db, q)


@router.get("/knowledge/{id}", response_model=KnowledgeOut)
async def get_knowledge_entry(id: str, db: AsyncSession = Depends(get_db)) -> KnowledgeLibrary:
    entry = await db.get(KnowledgeLibrary, id)
    if not entry:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    return entry


@router.delete("/knowledge/{id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_knowledge_entry(id: str, db: AsyncSession = Depends(get_db)) -> Response:
    entry = await db.get(KnowledgeLibrary, id)
    if not entry:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")

    await db.delete(entry)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
