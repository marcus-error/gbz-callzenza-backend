from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import KnowledgeLibrary


class RetrievalService:

    @staticmethod
    async def search_knowledge(db: AsyncSession, query_str: str, agent_id: str = None, limit: int = 5) -> list[KnowledgeLibrary]:
        """Searches the persistent Knowledge Library for entries matching the query string, strictly scoped to agent_id."""
        if not query_str or not query_str.strip() or not agent_id:
            return []

        # Check if the query is a greeting
        clean_query = query_str.strip().lower().rstrip(".?!")
        is_greeting = False
        if clean_query != "hello / greeting / script start":
            greetings_list = ["hi", "hello", "hey", "greetings", "good morning", "good afternoon", "morning", "hai", "hello there"]
            is_greeting = clean_query in greetings_list

        if is_greeting:
            # Since the agent always greets first, the user's greeting is a response.
            # We should route greetings directly to the next script step (the purpose, index 1).
            stmt = select(KnowledgeLibrary).where(
                KnowledgeLibrary.agent_id == agent_id,
                KnowledgeLibrary.source_data != "Uploaded Emotion Guidelines"
            ).order_by(KnowledgeLibrary.created_at.asc(), KnowledgeLibrary.id.asc())
            result = await db.scalars(stmt)
            all_entries = list(result.all())
            if len(all_entries) > 1:
                return [all_entries[1]]
            elif all_entries:
                return [all_entries[0]]

        import math
        import json
        from loguru import logger
        from app.core.config import get_settings
        
        settings = get_settings()
        threshold = settings.rag_similarity_threshold

        # Helper to compute cosine similarity
        def cosine_similarity(v1, v2):
            if not v1 or not v2 or len(v1) != len(v2):
                return 0.0
            dot = sum(a * b for a, b in zip(v1, v2))
            norm_a = math.sqrt(sum(a * a for a in v1))
            norm_b = math.sqrt(sum(b * b for b in v2))
            if norm_a == 0.0 or norm_b == 0.0:
                return 0.0
            return dot / (norm_a * norm_b)

        def get_query_embedding_sync():
            from app.services.gemini_service import generate_embedding
            return generate_embedding(query_str.strip())

        query_vector = None
        try:
            import asyncio
            query_vector = await asyncio.to_thread(get_query_embedding_sync)
        except Exception as embed_exc:
            logger.error(f"Failed to generate query embedding: {embed_exc}")

        # Step 3: Fetch candidate entries strictly for this agent
        stmt_all = select(KnowledgeLibrary).where(
            KnowledgeLibrary.agent_id == agent_id,
            KnowledgeLibrary.source_data != "Uploaded Emotion Guidelines"
        )
        res_all = await db.scalars(stmt_all)
        all_entries = list(res_all.all())

        entries_with_embeds = [e for e in all_entries if e.embedding is not None]

        # Step 4: Perform vector similarity search if embeddings exist, else use keyword/exact fallback
        if query_vector and entries_with_embeds:
            scored_entries = []
            for entry in entries_with_embeds:
                try:
                    entry_vector = json.loads(entry.embedding)
                    score = cosine_similarity(query_vector, entry_vector)
                    scored_entries.append((entry, score))
                except Exception as parse_exc:
                    logger.error(f"Failed to parse embedding for knowledge entry {entry.id}: {parse_exc}")
                    scored_entries.append((entry, 0.0))

            # Rank by cosine similarity score
            scored_entries.sort(key=lambda x: x[1], reverse=True)

            # Log RAG Debug mode diagnostics
            logger.info("================= RAG DEBUG MODE =================")
            logger.info(f"USER QUESTION: {query_str}")
            logger.info(f"QUERY EMBEDDING: {'generated successfully' if query_vector else 'failed'}")
            logger.info(f"VECTOR COLLECTION: knowledge_library (Agent: {agent_id})")
            logger.info(f"TOTAL VECTORS IN COLLECTION: {len(all_entries)}")
            logger.info(f"TOP K: {limit}")
            logger.info("RETRIEVED CHUNKS:")
            for idx, (entry, score) in enumerate(scored_entries[:limit]):
                logger.info(f"  {idx+1}. score={score:.4f} source={entry.source_data} text='{entry.answer[:100]}...'")

            # Apply relevance similarity threshold filtering
            kept_entries = [(entry, score) for entry, score in scored_entries if score >= threshold]
            removed_entries = [(entry, score) for entry, score in scored_entries if score < threshold]

            logger.info(f"RAG_SIMILARITY_THRESHOLD: {threshold}")
            logger.info(f"FILTERED CHUNKS KEPT: {len(kept_entries)}")
            for idx, (entry, score) in enumerate(kept_entries[:limit]):
                logger.info(f"  - KEPT: score={score:.4f} text='{entry.answer[:60]}...'")
            logger.info(f"FILTERED CHUNKS REMOVED: {len(removed_entries)}")
            logger.info("==================================================")

            # Return top limit items matching threshold
            return [entry for entry, score in kept_entries[:limit]]

        # Fallback 1: Try exact/close matches first
        logger.info("RAG Vector search fallback: using keyword database matching.")
        search_term = query_str.strip().lower()
        stmt = select(KnowledgeLibrary).where(
            KnowledgeLibrary.question.ilike(search_term),
            KnowledgeLibrary.source_data != "Uploaded Emotion Guidelines"
        )
        if agent_id:
            stmt = stmt.where(KnowledgeLibrary.agent_id == agent_id)
            
        stmt = stmt.limit(limit)
        result = await db.scalars(stmt)
        entries = list(result.all())
        if entries:
            return entries

        # Fallback 2: Fall back to keyword token search if no exact match is found
        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "with", "by", "of", "from",
            "is", "are", "was", "were", "be", "been", "have", "has", "had", "do", "does", "did", "this",
            "that", "these", "those", "it", "they", "we", "you", "i", "he", "she", "me", "him", "her",
            "us", "them", "my", "your", "his", "its", "our", "their", "how", "what", "why", "where",
            "when", "who", "which"
        }
        tokens = [
            t.strip().lower() 
            for t in query_str.split() 
            if t.strip().lower() not in stop_words and len(t.strip()) >= 2
        ]
        
        if not tokens:
            return []
 
        or_conds = []
        for token in tokens:
            t_pattern = f"%{token}%"
            or_conds.append(KnowledgeLibrary.question.ilike(t_pattern))
            or_conds.append(KnowledgeLibrary.answer.ilike(t_pattern))
            
        token_stmt = select(KnowledgeLibrary).where(
            or_(*or_conds),
            KnowledgeLibrary.source_data != "Uploaded Emotion Guidelines"
        )
        if agent_id:
            token_stmt = token_stmt.where(KnowledgeLibrary.agent_id == agent_id)
            
        token_stmt = token_stmt.limit(limit)
        token_result = await db.scalars(token_stmt)
        return list(token_result.all())

    @staticmethod
    async def is_duplicate(db: AsyncSession, agent_id: str, question: str) -> bool:
        """Verifies if the exact question has already been indexed in the Knowledge Library for this agent."""
        stmt = select(KnowledgeLibrary).where(
            KnowledgeLibrary.agent_id == agent_id,
            KnowledgeLibrary.question == question.strip()
        )
        result = await db.scalars(stmt)
        return result.first() is not None

    @staticmethod
    def is_informational_query(text: str) -> bool:
        """Heuristic check to identify if a query is seeking factual information vs conversation/chitchat."""
        if not text or not text.strip():
            return False
        import re
        text_lower = text.lower().strip().rstrip(".?!")
        
        # Explicit greeting bypass using word boundaries (avoids 'hi' in 'membership')
        if re.search(r'\b(?:hello|hi|hey|greetings?|vanakkam|namaste)\b', text_lower):
            return False
            
        # If it is very short (1-2 words), it is likely chitchat or conversational confirmation (yes/no/sure/ok)
        if len(text_lower.split()) <= 2:
            return False
        
        # Common conversational phrases
        chitchat_patterns = [
            r'\bhow\s+are\s+you\b', r'\bhow\'s\s+it\s+going\b', r'\bhow\s+are\s+you\s+doing\b',
            r'\bi\s+am\s+good\b', r'\bi\'m\s+good\b', r'\bi\s+am\s+fine\b', r'\bi\'m\s+fine\b',
            r'\bdoing\s+well\b', r'\bnot\s+too\s+bad\b', r'\bgreat\s+thanks\b', r'\bthank\s+you\b',
            r'\bnice\s+to\s+meet\s+you\b', r'\bwho\s+is\s+this\b', r'\bwho\s+are\s+you\b',
            r'\bwhy\s+are\s+you\s+calling\b', r'\bwhat\s+is\s+this\s+call\s+about\b'
        ]
        if any(re.search(p, text_lower) for p in chitchat_patterns):
            return False
            
        # Question words / terms indicating informational query
        question_words = ["what", "when", "where", "why", "who", "how", "price", "cost", "fee", "fees", "refund", "founded", "hours", "headquarters", "plan", "service", "services"]
        if any(qw in text_lower.split() or qw in text_lower for qw in question_words):
            return True
            
        return True

