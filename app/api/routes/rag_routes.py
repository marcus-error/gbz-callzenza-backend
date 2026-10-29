import os
import shutil
import uuid
from typing import List, Optional, Dict, Any
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
from loguru import logger

from app.core.security import get_current_user, get_current_user_optional
from app.db.database import get_db
from app.db.models import User, KnowledgeDocument, KnowledgeChunk, RAGConfig
from app.services.document_service import DocumentProcessingService, UPLOAD_DIR
from app.services.rag_service import RAGService

router = APIRouter(prefix="/api/rag", tags=["Enterprise RAG Service"])


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class RAGQueryRequest(BaseModel):
    enterprise_id: str = Field(..., min_length=1, description="Enterprise Business ID")
    rag_id: str = Field(..., min_length=1, description="Target RAG Configuration ID")
    question: str = Field(..., min_length=1, description="User question / intent")
    history: Optional[List[Dict[str, str]]] = None

class RAGQueryResponse(BaseModel):
    answer: str
    enterprise_id: str
    rag_id: str
    sources: List[str] = []
    retrieved_context: List[Dict[str, Any]] = []
    latency_ms: int = 0
    has_context: bool = False

class RAGConfigCreatePayload(BaseModel):
    enterprise_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    description: str = ""
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    llm_model: str = "gemini-2.5-flash"
    llm_provider: str = "gemini"
    llm_api_base: str = ""
    top_k: int = 5
    chunk_size: int = 500
    chunk_overlap: int = 50
    system_prompt: str = "You are CallZenza AI assistant. Answer using the provided context."

class RAGConfigUpdatePayload(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    embedding_model: Optional[str] = None
    llm_model: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_api_base: Optional[str] = None
    top_k: Optional[int] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    system_prompt: Optional[str] = None

class RAGConfigOut(BaseModel):
    id: str
    enterprise_id: str
    name: str
    description: str
    status: str
    embedding_model: str
    llm_model: str
    llm_provider: str
    llm_api_base: str
    top_k: int
    chunk_size: int
    chunk_overlap: int
    system_prompt: str
    created_at: str


# ---------------------------------------------------------------------------
# RAG Configuration API Routes
# ---------------------------------------------------------------------------

@router.get("/config", response_model=List[RAGConfigOut])
async def list_rag_configs(
    enterprise_id: str,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """Lists all RAG configurations for a given enterprise."""
    await RAGService.validate_enterprise_rag_access(db, current_user, enterprise_id)
    stmt = select(RAGConfig).where(RAGConfig.business_id == enterprise_id)
    res = await db.scalars(stmt)
    configs = res.all()
    
    out = []
    for c in configs:
        out.append(RAGConfigOut(
            id=c.id,
            enterprise_id=c.business_id,
            name=c.name,
            description=c.description or "",
            status=c.status,
            embedding_model=c.embedding_model,
            llm_model=c.llm_model,
            llm_provider=c.llm_provider,
            llm_api_base=c.llm_api_base,
            top_k=c.top_k,
            chunk_size=c.chunk_size,
            chunk_overlap=c.chunk_overlap,
            created_at=c.created_at.isoformat() if c.created_at else ""
        ))
    return out


@router.post("/config", response_model=RAGConfigOut)
async def create_rag_config(
    payload: RAGConfigCreatePayload,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """Creates a new RAG configuration for an enterprise."""
    await RAGService.validate_enterprise_rag_access(db, current_user, payload.enterprise_id)
    
    config = RAGConfig(
        business_id=payload.enterprise_id,
        name=payload.name,
        description=payload.description,
        status="ACTIVE",
        embedding_model=payload.embedding_model,
        llm_model=payload.llm_model,
        llm_provider=payload.llm_provider,
        llm_api_base=payload.llm_api_base,
        top_k=payload.top_k,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)

    return RAGConfigOut(
        id=config.id,
        enterprise_id=config.business_id,
        name=config.name,
        description=config.description or "",
        status=config.status,
        embedding_model=config.embedding_model,
        llm_model=config.llm_model,
        llm_provider=config.llm_provider,
        llm_api_base=config.llm_api_base,
        top_k=config.top_k,
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        created_at=config.created_at.isoformat() if config.created_at else ""
    )


@router.put("/config/{rag_id}", response_model=RAGConfigOut)
async def update_rag_config(
    rag_id: str,
    payload: RAGConfigUpdatePayload,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """Updates an existing RAG configuration."""
    config = await db.get(RAGConfig, rag_id)
    if not config:
        raise HTTPException(status_code=404, detail="RAG Configuration not found")
    
    await RAGService.validate_enterprise_rag_access(db, current_user, config.business_id, rag_id)

    for field, val in payload.dict(exclude_unset=True).items():
        if val is not None:
            setattr(config, field, val)

    await db.commit()
    await db.refresh(config)

    return RAGConfigOut(
        id=config.id,
        enterprise_id=config.business_id,
        name=config.name,
        description=config.description or "",
        status=config.status,
        embedding_model=config.embedding_model,
        llm_model=config.llm_model,
        llm_provider=config.llm_provider,
        llm_api_base=config.llm_api_base,
        top_k=config.top_k,
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        created_at=config.created_at.isoformat() if config.created_at else ""
    )


@router.delete("/config/{rag_id}")
async def delete_rag_config(
    rag_id: str,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a RAG configuration."""
    config = await db.get(RAGConfig, rag_id)
    if not config:
        raise HTTPException(status_code=404, detail="RAG Configuration not found")
    
    await RAGService.validate_enterprise_rag_access(db, current_user, config.business_id, rag_id)
    
    await db.delete(config)
    await db.commit()
    return {"success": True, "message": f"RAG Configuration {rag_id} deleted"}


# ---------------------------------------------------------------------------
# Document Ingestion & Management API Routes
# ---------------------------------------------------------------------------

@router.post("/ingest")
async def ingest_document(
    enterprise_id: str = Form(...),
    rag_id: str = Form(...),
    title: Optional[str] = Form(None),
    file: UploadFile = File(...),
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Ingests a document into an enterprise's specific RAG instance.
    Enforces security access, extracts text, cleans text, splits into chunks,
    generates embeddings, and stores vectors with metadata.
    """
    await RAGService.validate_enterprise_rag_access(db, current_user, enterprise_id, rag_id)

    filename = file.filename or "uploaded_document"
    ext = filename.split(".")[-1].upper() if "." in filename else "TXT"

    if ext not in ("PDF", "DOCX", "DOC", "TXT", "CSV", "MD"):
        raise HTTPException(status_code=400, detail=f"Unsupported file format: {ext}")

    doc_id = str(uuid.uuid4())
    safe_name = f"{doc_id}_{filename}"
    file_path = str(UPLOAD_DIR / safe_name)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    file_size = os.path.getsize(file_path)
    user_id = current_user.get("sub") if current_user else None

    doc = KnowledgeDocument(
        id=doc_id,
        business_id=enterprise_id,
        rag_id=rag_id,
        title=title or filename,
        filename=filename,
        file_path=file_path,
        file_type=ext,
        file_size=file_size,
        status="UPLOADED",
        created_by_user_id=str(user_id) if user_id else None
    )
    db.add(doc)
    await db.commit()

    processed_doc = await DocumentProcessingService.process_document(db, doc_id)

    return {
        "success": True,
        "document_id": processed_doc.id,
        "enterprise_id": enterprise_id,
        "rag_id": rag_id,
        "title": processed_doc.title,
        "status": processed_doc.status,
        "chunk_count": processed_doc.chunk_count,
        "created_at": processed_doc.created_at.isoformat() if processed_doc.created_at else None
    }


@router.get("/documents")
async def list_rag_documents(
    enterprise_id: str,
    rag_id: str,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """Lists all documents associated with a specific enterprise and RAG ID."""
    await RAGService.validate_enterprise_rag_access(db, current_user, enterprise_id, rag_id)

    stmt = select(KnowledgeDocument).where(
        KnowledgeDocument.business_id == enterprise_id,
        KnowledgeDocument.rag_id == rag_id
    )
    res = await db.scalars(stmt)
    docs = res.all()

    return [
        {
            "id": d.id,
            "enterprise_id": d.business_id,
            "rag_id": d.rag_id,
            "title": d.title,
            "filename": d.filename,
            "file_type": d.file_type,
            "file_size": d.file_size,
            "status": d.status,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at.isoformat() if d.created_at else ""
        }
        for d in docs
    ]


@router.delete("/documents/{document_id}")
async def delete_rag_document(
    document_id: str,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """Deletes a document and purges its vector embeddings."""
    doc = await db.get(KnowledgeDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    await RAGService.validate_enterprise_rag_access(db, current_user, doc.business_id, doc.rag_id)
    
    success = await DocumentProcessingService.delete_document(db, document_id)
    return {"success": success, "document_id": document_id}


# ---------------------------------------------------------------------------
# RAG Query API Route
# ---------------------------------------------------------------------------

@router.post("/query", response_model=RAGQueryResponse)
async def query_rag(
    payload: RAGQueryRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db)
):
    """
    Executes the full RAG retrieval pipeline:
    Authentication -> Enterprise ID Validation -> RAG ID Validation -> Query Embedding -> Vector Search -> Top-K -> LangChain -> Answer.
    """
    res = await RAGService.query_rag_pipeline(
        db=db,
        enterprise_id=payload.enterprise_id,
        rag_id=payload.rag_id,
        question=payload.question,
        current_user=current_user,
        history=payload.history
    )
    return RAGQueryResponse(**res)
