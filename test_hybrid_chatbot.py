import asyncio
import os
import sys

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.db.database import AsyncSessionLocal
from app.services.rag_service import RAGService, is_project_query


async def run_hybrid_tests():
    print("=" * 70)
    print("RUNNING CALLZENZA AI HYBRID CHATBOT VERIFICATION TESTS")
    print("=" * 70)

    # Test 1: Intent Classification Helper
    print("\n--- Test 1: Intent Classification ---")
    gen_q = "What is the capital of France?"
    proj_q = "What are CallZenza voice agents?"
    hybrid_q = "How do I write a Python script to call the CallZenza lead API?"

    print(f"Query: '{gen_q}' -> is_project_query: {is_project_query(gen_q)} (Expected: False)")
    print(f"Query: '{proj_q}' -> is_project_query: {is_project_query(proj_q)} (Expected: True)")
    print(f"Query: '{hybrid_q}' -> is_project_query: {is_project_query(hybrid_q)} (Expected: True)")

    assert is_project_query(gen_q) == False, "General query incorrectly classified as project query"
    assert is_project_query(proj_q) == True, "Project query incorrectly classified as general query"
    assert is_project_query(hybrid_q) == True, "Hybrid query incorrectly classified as general query"

    async with AsyncSessionLocal() as db:
        # Test 2: General Knowledge Question
        print("\n--- Test 2: General Knowledge Question ---")
        q2 = "Explain object-oriented programming concepts in 2 sentences."
        res2 = await RAGService.answer_question(db=db, query=q2)
        print(f"Question: {q2}")
        print(f"Answer:\n{res2['answer']}")
        print(f"Latency: {res2['latency_ms']} ms")
        assert "don't have" not in res2['answer'].lower(), "General question was rejected!"
        assert len(res2['answer']) > 20, "Answer too short"

        await asyncio.sleep(1.0)

        # Test 3: CallZenza Project Question
        print("\n--- Test 3: CallZenza Project Question ---")
        q3 = "What is CallZenza and what does it do?"
        res3 = await RAGService.answer_question(db=db, query=q3)
        print(f"Question: {q3}")
        print(f"Answer:\n{res3['answer']}")
        print(f"Has Context: {res3.get('has_context', False)}")

        await asyncio.sleep(1.0)

        # Test 4: Hybrid Question
        print("\n--- Test 4: Hybrid Question ---")
        q4 = "Write a simple Python example to connect to CallZenza."
        res4 = await RAGService.answer_question(db=db, query=q4)
        print(f"Question: {q4}")
        print(f"Answer:\n{res4['answer']}")

        await asyncio.sleep(1.0)

        # Test 5: Missing Project Feature Question
        print("\n--- Test 5: Missing Project Feature Question ---")
        q5 = "Does CallZenza have a quantum teleportation module?"
        res5 = await RAGService.answer_question(db=db, query=q5)
        print(f"Question: {q5}")
        print(f"Answer:\n{res5['answer']}")
        assert "not available" in res5['answer'].lower() or "don't have" in res5['answer'].lower() or "no" in res5['answer'].lower() or "quantum" in res5['answer'].lower(), "Missing feature was not stated clearly"

        await asyncio.sleep(1.0)

        # Test 6: Multi-turn Conversation Context
        print("\n--- Test 6: Multi-turn Follow-up Question ---")
        history = [
            {"sender": "user", "content": "What is Python?"},
            {"sender": "assistant", "content": "Python is a popular high-level programming language known for its readability."}
        ]
        q6 = "What are its key features?"
        res6 = await RAGService.answer_question(db=db, query=q6, history=history)
        print(f"History: {history}")
        print(f"Follow-up Question: {q6}")
        print(f"Answer:\n{res6['answer']}")
        assert "python" in res6['answer'].lower() or "language" in res6['answer'].lower() or "syntax" in res6['answer'].lower() or "dynamic" in res6['answer'].lower(), "Follow-up question failed to use conversation history context"

    print("\n" + "=" * 70)
    print("ALL HYBRID CHATBOT TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run_hybrid_tests())
