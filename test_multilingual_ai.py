import asyncio
import os
import sys

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.db.database import AsyncSessionLocal
from app.services.rag_service import RAGService
from app.db.models import Lead, Conversation, AIAgent
from app.services.lead_engagement_service import process_incoming_message


async def run_multilingual_tests():
    print("=" * 80, flush=True)
    print("RUNNING CALLZENZA UNIVERSAL MULTILINGUAL AI & VOICE VERIFICATION TESTS", flush=True)
    print("=" * 80, flush=True)

    async with AsyncSessionLocal() as db:
        # Clean up legacy fallback cache entries from RAGCache
        from sqlalchemy import delete
        from app.db.models import RAGCache
        await db.execute(
            delete(RAGCache).where(
                (RAGCache.answer.ilike("%don't have information%")) | 
                (RAGCache.answer.ilike("%not available%"))
            )
        )
        await db.commit()
        # Test 1: General Knowledge Questions in Multiple Languages
        print("\n--- Test 1: General Knowledge in Various Languages & Transliteration ---")
        gen_queries = [
            ("English", "What is Python?"),
            ("Tanglish (Transliterated Tamil)", "Python na enna?"),
            ("Hinglish (Transliterated Hindi)", "Python kya hai?"),
            ("Telugu (Transliterated)", "Python ante enti?"),
            ("Spanish", "¿Qué es Python?"),
        ]

        for lang_name, query in gen_queries:
            res = await RAGService.answer_question(db=db, query=query)
            print(f"[{lang_name}] Query: '{query}'")
            print(f"Answer: {res['answer']}\n")
            assert "don't have information" not in res['answer'].lower(), f"General query '{query}' was rejected!"
            assert len(res['answer']) > 15, f"Response too short for '{query}'"

        await asyncio.sleep(0.5)

        # Test 2: CallZenza Project Knowledge in Multiple Languages
        print("\n--- Test 2: CallZenza Project Knowledge in Multiple Languages ---")
        proj_queries = [
            ("English", "What is CallZenza?"),
            ("Tanglish", "CallZenza la campaign create panna mudiyuma?"),
            ("Hinglish", "CallZenza mein campaign kaise create karte hain?"),
        ]

        for lang_name, query in proj_queries:
            res = await RAGService.answer_question(db=db, query=query)
            print(f"[{lang_name}] Query: '{query}'")
            print(f"Answer: {res['answer']}\n")
            assert len(res['answer']) > 10, f"No response generated for project query '{query}'"

        await asyncio.sleep(0.5)

        # Test 3: Hybrid Knowledge Questions (General Concept + CallZenza Implementation)
        print("\n--- Test 3: Hybrid Questions (General + Project Combined) ---")
        hybrid_queries = [
            ("Tanglish Hybrid", "RAG epdi work aagum and CallZenza la adha epdi use pannirukom?"),
            ("Hinglish Hybrid", "RAG kya hai aur CallZenza mein RAG kaise use hota hai?"),
        ]

        for lang_name, query in hybrid_queries:
            res = await RAGService.answer_question(db=db, query=query)
            print(f"[{lang_name}] Query: '{query}'")
            print(f"Answer: {res['answer']}\n")
            assert "rag" in res['answer'].lower() or "callzenza" in res['answer'].lower(), f"Hybrid answer missing key terms for '{query}'"

        await asyncio.sleep(0.5)

        # Test 4: Mid-Conversation Language Switching while Preserving Context
        print("\n--- Test 4: Mid-Conversation Language Switching ---")
        h1 = [{"sender": "user", "content": "Explain campaign creation in CallZenza in English."}]
        res_eng = await RAGService.answer_question(db=db, query="Explain campaign creation in CallZenza in English.", history=[])
        h1.append({"sender": "assistant", "content": res_eng['answer']})

        # Switch to Tanglish
        q_switch_ta = "Idha Tamil la explain pannu."
        res_ta = await RAGService.answer_question(db=db, query=q_switch_ta, history=h1)
        print(f"Original Turn (English): {res_eng['answer'][:80]}...")
        print(f"Language Switch Request: '{q_switch_ta}'")
        print(f"Switched Response (Tamil/Tanglish): {res_ta['answer']}\n")

        # Switch to Hindi
        h2 = h1 + [{"sender": "user", "content": q_switch_ta}, {"sender": "assistant", "content": res_ta['answer']}]
        q_switch_hi = "Ab Hindi mein batao."
        res_hi = await RAGService.answer_question(db=db, query=q_switch_hi, history=h2)
        print(f"Language Switch Request: '{q_switch_hi}'")
        print(f"Switched Response (Hindi): {res_hi['answer']}\n")

        await asyncio.sleep(0.5)

        # Test 5: Voice Pipeline Multilingual & General Question Handling
        print("\n--- Test 5: Voice Pipeline Interaction (process_incoming_message) ---")

        # Create temporary lead & conversation for voice test
        temp_lead = Lead(business_id="test_b", first_name="Voice", last_name="User", phone="+1234567890")
        db.add(temp_lead)
        await db.flush()

        temp_agent = AIAgent(business_id="test_b", name="Sales Assistant", system_prompt="You are a helpful sales assistant.")
        db.add(temp_agent)
        await db.flush()

        temp_conv = Conversation(business_id="test_b", lead_id=temp_lead.id, agent_id=temp_agent.id, channel="VOICE")
        db.add(temp_conv)
        await db.flush()

        voice_inputs = [
            ("Tanglish Spoken", "Python na enna?"),
            ("Hinglish Spoken", "CallZenza la campaign create panradhu kaise work karta hai?"),
            ("English Spoken", "What is artificial intelligence?"),
        ]

        for lang_tag, v_text in voice_inputs:
            v_res = await process_incoming_message(
                db=db,
                lead=temp_lead,
                conversation=temp_conv,
                agent=temp_agent,
                content=v_text,
                current_lang="auto"
            )
            reply = v_res["ai_message"].content if v_res["ai_message"] else "No reply"
            print(f"[{lang_tag}] Voice Spoken: '{v_text}'")
            print(f"Detected Lang: {v_res.get('detected_lang')}")
            print(f"AI Spoken Reply: {reply}\n")
            assert "couldn't find that information in the knowledge base" not in reply.lower(), f"Voice query '{v_text}' was strictly rejected!"

    print("=" * 80)
    print("ALL MULTILINGUAL & VOICE VERIFICATION TESTS PASSED SUCCESSFULLY!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_multilingual_tests())
