import os
import re
import json
import csv
import io
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update
from loguru import logger

from app.db.models import KnowledgeDocument, KnowledgeChunk
from app.services.gemini_service import generate_embedding


UPLOAD_DIR = Path("app/static/uploads/documents")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


class DocumentProcessingService:
    """Service to handle document ingestion, parsing, chunking, and indexing for CallZenza RAG."""

    @staticmethod
    def clean_text(text: str) -> str:
        """Removes excessive whitespace and standardizes newlines."""
        if not text:
            return ""
        # Replace multiple whitespace/newlines
        text = re.sub(r"\r\n|\r", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    @staticmethod
    def extract_text_from_pdf(file_path: str) -> List[Tuple[int, str]]:
        """Extracts text page by page from PDF file using PyMuPDF (fitz) or pypdf."""
        pages_content = []
        # Try PyMuPDF (fitz) first
        try:
            import fitz
            doc = fitz.open(file_path)
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                text = page.get_text() or ""
                clean = DocumentProcessingService.clean_text(text)
                if clean:
                    pages_content.append((page_idx + 1, clean))
            doc.close()
            if pages_content:
                return pages_content
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"PyMuPDF failed to parse PDF {file_path}: {e}")

        # Fallback to pypdf / PyPDF2
        try:
            import pypdf
            reader = pypdf.PdfReader(file_path)
            for page_idx, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                clean = DocumentProcessingService.clean_text(text)
                if clean:
                    pages_content.append((page_idx + 1, clean))
            if pages_content:
                return pages_content
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"pypdf failed to parse PDF {file_path}: {e}")

        try:
            import PyPDF2
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page_idx, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    clean = DocumentProcessingService.clean_text(text)
                    if clean:
                        pages_content.append((page_idx + 1, clean))
            if pages_content:
                return pages_content
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"PyPDF2 failed to parse PDF {file_path}: {e}")

        if not pages_content:
            raise ValueError("Could not extract readable text from this PDF file.")
        return pages_content

    @staticmethod
    def extract_text_from_docx(file_path: str) -> str:
        """Extracts text from DOCX file using python-docx or built-in zipfile XML extraction."""
        try:
            import docx
            doc = docx.Document(file_path)
            full_text = []
            for para in doc.paragraphs:
                if para.text.strip():
                    full_text.append(para.text.strip())
            for table in doc.tables:
                for row in table.rows:
                    row_text = " | ".join([cell.text.strip() for cell in row.cells if cell.text.strip()])
                    if row_text:
                        full_text.append(row_text)
            if full_text:
                return "\n\n".join(full_text)
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"python-docx failed: {e}")

        # Pure Python fallback using zipfile and XML
        try:
            import zipfile
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(file_path) as z:
                xml_content = z.read("word/document.xml")
                tree = ET.fromstring(xml_content)
                texts = [node.text for node in tree.iter() if node.tag.endswith("}t") and node.text]
                if texts:
                    return " ".join(texts)
        except Exception as e:
            logger.error(f"Failed to parse DOCX {file_path}: {e}")
            raise ValueError(f"Could not read DOCX file: {str(e)}")

        return ""

    @staticmethod
    def extract_text_from_csv(file_path: str) -> str:
        """Extracts text from CSV file formatting rows into readable text blocks."""
        try:
            rows = []
            with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                headers = []
                for idx, row in enumerate(reader):
                    if idx == 0:
                        headers = [h.strip() for h in row]
                        continue
                    if not any(row):
                        continue
                    row_desc = []
                    for h_idx, val in enumerate(row):
                        header_name = headers[h_idx] if h_idx < len(headers) else f"Column {h_idx+1}"
                        if val.strip():
                            row_desc.append(f"{header_name}: {val.strip()}")
                    if row_desc:
                        rows.append(" | ".join(row_desc))
            return "\n".join(rows)
        except Exception as e:
            logger.error(f"Failed to parse CSV {file_path}: {e}")
            raise ValueError(f"Could not read CSV file: {str(e)}")

    @staticmethod
    def extract_text(file_path: str, file_type: str) -> List[Tuple[int, str]]:
        """Extracts text from any supported file type, returning list of (page_num, text)."""
        ft = file_type.upper()
        if ft == "PDF":
            return DocumentProcessingService.extract_text_from_pdf(file_path)
        elif ft in ("DOCX", "DOC"):
            txt = DocumentProcessingService.extract_text_from_docx(file_path)
            return [(1, DocumentProcessingService.clean_text(txt))]
        elif ft == "CSV":
            txt = DocumentProcessingService.extract_text_from_csv(file_path)
            return [(1, DocumentProcessingService.clean_text(txt))]
        else: # TXT, MD, MANUAL
            with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
            return [(1, DocumentProcessingService.clean_text(txt))]

    @staticmethod
    def chunk_text(
        text: str, 
        chunk_size: int = 800, 
        overlap: int = 150
    ) -> List[str]:
        """
        Splits text into overlapping chunks respecting sentence/paragraph boundaries where possible.
        """
        if not text:
            return []
        
        # If text is already small enough
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        # Split by paragraphs or sentences
        paragraphs = text.split("\n\n")
        current_chunk = ""

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            if len(current_chunk) + len(para) + 2 <= chunk_size:
                current_chunk = f"{current_chunk}\n\n{para}" if current_chunk else para
            else:
                if current_chunk:
                    chunks.append(current_chunk)
                # If paragraph itself is larger than chunk_size, split by sentences/window
                if len(para) > chunk_size:
                    start = 0
                    while start < len(para):
                        end = start + chunk_size
                        chunk_slice = para[start:end]
                        chunks.append(chunk_slice)
                        start += (chunk_size - overlap)
                    current_chunk = ""
                else:
                    current_chunk = para

        if current_chunk:
            chunks.append(current_chunk)

        return [c.strip() for c in chunks if c.strip()]

    @classmethod
    async def process_document(
        cls, 
        db: AsyncSession, 
        document_id: str
    ) -> KnowledgeDocument:
        """Processes an uploaded or manual document: parses text, creates chunks, and generates embeddings."""
        doc = await db.get(KnowledgeDocument, document_id)
        if not doc:
            raise ValueError(f"Document {document_id} not found")

        doc.status = "PROCESSING"
        doc.error_message = None
        await db.commit()

        try:
            # Delete any existing chunks for this doc (for reprocessing)
            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id))
            await db.commit()

            # Extract text
            page_items = cls.extract_text(doc.file_path, doc.file_type)
            if not page_items:
                raise ValueError("No readable text could be extracted from this document.")

            created_chunks = []
            chunk_index = 0
            faiss_chunk_payloads = []

            # Determine enterprise_id and rag_id
            enterprise_id = doc.business_id or "default_enterprise"
            rag_id = doc.rag_id or "default_rag"

            from app.rag.langchain_chain import split_document_text
            from app.rag.embedding_service import generate_text_embedding
            from app.rag.vector_store import FAISSVectorStoreManager

            for page_num, page_text in page_items:
                text_chunks = split_document_text(page_text, chunk_size=500, chunk_overlap=50)
                for chunk_text in text_chunks:
                    if not chunk_text.strip():
                        continue
                    
                    emb_vector = generate_text_embedding(chunk_text)
                    emb_json = json.dumps(emb_vector)

                    meta = {
                        "enterprise_id": enterprise_id,
                        "rag_id": rag_id,
                        "page": page_num,
                        "document_id": doc.id,
                        "document_title": doc.title,
                        "filename": doc.filename,
                        "file_type": doc.file_type
                    }

                    chunk = KnowledgeChunk(
                        document_id=doc.id,
                        business_id=doc.business_id,
                        agent_id=doc.agent_id or (str(doc.agent_key) if doc.agent_key is not None else None),
                        rag_id=doc.rag_id,
                        agent_key=doc.agent_key,
                        version_id=doc.version_id or "v1",
                        is_active=doc.is_active if doc.is_active is not None else True,
                        chunk_index=chunk_index,
                        text=chunk_text,
                        embedding=emb_json,
                        meta_info=meta,
                        token_count=len(chunk_text.split())
                    )
                    db.add(chunk)
                    created_chunks.append(chunk)

                    faiss_chunk_payloads.append({
                        "chunk_id": chunk.id,
                        "chunk_index": chunk_index,
                        "text": chunk_text,
                        "embedding": emb_vector,
                        "source": doc.filename or doc.title,
                        "created_at": doc.created_at.isoformat() if doc.created_at else None
                    })
                    chunk_index += 1

            await db.commit()

            # Index into FAISS Store with enterprise_id & rag_id metadata isolation
            FAISSVectorStoreManager.add_chunks(
                enterprise_id=enterprise_id,
                rag_id=rag_id,
                document_id=doc.id,
                document_name=doc.filename or doc.title,
                chunks=faiss_chunk_payloads
            )

            doc.status = "COMPLETED"
            doc.chunk_count = len(created_chunks)
            doc.error_message = None
            await db.commit()
            await db.refresh(doc)
            logger.info(f"Successfully processed document '{doc.title}' with {doc.chunk_count} chunks.")
            return doc

        except Exception as e:
            logger.error(f"Document processing failed for {document_id}: {e}")
            doc.status = "FAILED"
            doc.error_message = str(e)
            await db.commit()
            await db.refresh(doc)
            return doc

    @classmethod
    async def create_manual_document(
        cls,
        db: AsyncSession,
        title: str,
        content: str,
        business_id: Optional[str] = None,
        rag_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_key: Optional[int] = None
    ) -> KnowledgeDocument:
        """Creates and immediately processes a manual knowledge base entry."""
        import uuid
        doc_id = str(uuid.uuid4())
        safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip() or "manual_entry"
        file_path = str(UPLOAD_DIR / f"{doc_id}_{safe_title}.txt")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        doc = KnowledgeDocument(
            id=doc_id,
            business_id=business_id,
            rag_id=rag_id,
            agent_key=agent_key,
            title=title,
            filename=f"{safe_title}.txt",
            file_path=file_path,
            file_type="MANUAL",
            file_size=len(content.encode("utf-8")),
            status="UPLOADED",
            created_by_user_id=user_id
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)

        # Process right away
        return await cls.process_document(db, doc.id)

    @classmethod
    async def delete_document(
        cls,
        db: AsyncSession,
        document_id: str
    ) -> bool:
        """Deletes a document, its physical file, associated FAISS vectors, and DB chunks."""
        doc = await db.get(KnowledgeDocument, document_id)
        if not doc:
            return False

        # Remove vectors from FAISS Store
        enterprise_id = doc.business_id or "default_enterprise"
        rag_id = doc.rag_id or "default_rag"
        from app.rag.vector_store import FAISSVectorStoreManager
        FAISSVectorStoreManager.delete_document_vectors(enterprise_id, rag_id, document_id)

        # Delete physical file
        if doc.file_path and os.path.exists(doc.file_path):
            try:
                os.remove(doc.file_path)
            except Exception as e:
                logger.warning(f"Could not remove physical file {doc.file_path}: {e}")

        await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id))
        await db.delete(doc)
        await db.commit()
        return True

    @classmethod
    async def append_auto_learned_qa(
        cls,
        db: AsyncSession,
        agent_key: int,
        question: str,
        answer: str,
        business_id: Optional[str] = None
    ) -> KnowledgeDocument:
        """Appends a new Q&A pair to the SINGLE consolidated auto-learned document for an agent, deduplicating any legacy docs."""
        if not answer or "don't have information" in answer.lower() or "not available" in answer.lower():
            return None

        import uuid
        doc_title = f"Auto-Learned Knowledge Base (Agent {agent_key})"
        
        # Search for all existing auto-learned documents for this agent key
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.agent_key == agent_key,
            (KnowledgeDocument.title.ilike("%Auto-Learned%") | (KnowledgeDocument.title == doc_title))
        ).order_by(KnowledgeDocument.created_at.asc())
        
        existing_docs = (await db.scalars(stmt)).all()
        
        new_entry = f"Question: {question.strip()}\n\nAnswer: {answer.strip()}"

        if not existing_docs:
            doc = await cls.create_manual_document(
                db=db,
                title=doc_title,
                content=new_entry,
                business_id=business_id,
                agent_key=agent_key
            )
            return doc
        else:
            existing_doc = existing_docs[0]
            existing_doc.title = doc_title
            
            # Clean up extra duplicate documents if any exist
            if len(existing_docs) > 1:
                for duplicate in existing_docs[1:]:
                    await cls.delete_document(db, duplicate.id)

            existing_content = ""
            if existing_doc.file_path and os.path.exists(existing_doc.file_path):
                try:
                    with open(existing_doc.file_path, "r", encoding="utf-8", errors="ignore") as f:
                        existing_content = f.read()
                except Exception as e:
                    logger.warning(f"Could not read existing auto-learned doc file: {e}")

            if f"question: {question.strip().lower()}" in existing_content.lower():
                return existing_doc

            full_content = f"{existing_content.strip()}\n\n---\n\n{new_entry}".strip()
            
            with open(existing_doc.file_path, "w", encoding="utf-8") as f:
                f.write(full_content)
                
            existing_doc.file_size = len(full_content.encode("utf-8"))
            existing_doc.status = "PROCESSING"
            await db.commit()

            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == existing_doc.id))
            await db.commit()

            chunks_text = cls.chunk_text(full_content, chunk_size=800, overlap=120)
            created_chunks = []
            for idx, chunk_text in enumerate(chunks_text):
                if not chunk_text.strip():
                    continue
                emb_vector = generate_embedding(chunk_text)
                emb_json = json.dumps(emb_vector)

                chunk = KnowledgeChunk(
                    id=str(uuid.uuid4()),
                    document_id=existing_doc.id,
                    business_id=existing_doc.business_id,
                    agent_key=agent_key,
                    chunk_index=idx,
                    text=chunk_text,
                    token_count=len(chunk_text.split()),
                    embedding=emb_json,
                    meta_info={"source": "auto_learned", "agent_key": agent_key}
                )
                db.add(chunk)
                created_chunks.append(chunk)

            existing_doc.chunk_count = len(created_chunks)
            existing_doc.status = "COMPLETED"
            existing_doc.error_message = None
            await db.commit()
            return existing_doc
