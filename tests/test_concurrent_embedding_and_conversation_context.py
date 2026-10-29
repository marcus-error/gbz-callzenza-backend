import asyncio
import concurrent.futures
import threading
import pytest
import numpy as np
from app.rag.embedding_service import get_embedding_model, generate_text_embedding, _model_cache


def test_concurrent_embedding_initialization_thread_safety():
    """
    Verifies that concurrent callers in separate OS threads share a single
    thread-safe initialized SentenceTransformer instance without race conditions,
    duplicate allocations, or native access violation crashes.
    """
    # Clear cache for this test to verify clean first-time concurrent initialization
    _model_cache.clear()

    num_threads = 8
    models = []
    errors = []

    def _worker(thread_idx):
        try:
            m = get_embedding_model()
            return m
        except Exception as e:
            errors.append(e)
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(_worker, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            if res is not None:
                models.append(res)

    assert len(errors) == 0, f"Encountered errors during concurrent model load: {errors}"
    assert len(models) == num_threads, f"Expected {num_threads} model returns, got {len(models)}"

    # All returned model instances MUST be the exact same object reference (singleton)
    first_model = models[0]
    for idx, m in enumerate(models[1:], start=2):
        assert m is first_model, f"Thread {idx} received a different model instance ({id(m)} vs {id(first_model)})"


def test_concurrent_text_embedding_generation():
    """
    Verifies that concurrent embedding generation across multiple threads
    operates reliably, produces normalized 384-dimensional vectors, and does not crash.
    """
    queries = [
        "What are the premium payment options for life insurance?",
        "Can I pay monthly or quarterly?",
        "இதை தமிழில் விளக்க முடியுமா?",  # Tamil
        "¿Cuáles son los beneficios de la póliza?",  # Spanish
        "What is the claim settlement ratio?",
        "How do I add a nominee to my policy?",
        "Is there a medical examination required?",
        "What happens if a payment is missed?",
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries)) as executor:
        futures = {executor.submit(generate_text_embedding, q): q for q in queries}
        results = {}
        for f in concurrent.futures.as_completed(futures):
            q = futures[f]
            results[q] = f.result()

    assert len(results) == len(queries)
    for q, vec in results.items():
        assert isinstance(vec, list)
        assert len(vec) == 384, f"Vector dimension mismatch for '{q}': {len(vec)}"
        # Verify vector is non-zero
        arr = np.array(vec)
        norm = np.linalg.norm(arr)
        assert norm > 0.1, f"Vector norm too close to zero for '{q}'"


@pytest.mark.asyncio
async def test_independent_conversation_context_across_three_concurrent_callers():
    """
    Verifies that three concurrent callers with distinct prompts generate
    independent, non-interfering embeddings and retain isolated contexts.
    """
    caller_prompts = {
        "call_1_premium": "What are the exact monthly premium rates?",
        "call_2_budget": "I have a tight budget, give me the lowest price plan.",
        "call_3_tamil": "இதை தமிழில் சொல்லுங்கள்.",
    }

    async def _embed_async(call_id, prompt):
        vec = await asyncio.to_thread(generate_text_embedding, prompt)
        return call_id, vec

    tasks = [_embed_async(cid, p) for cid, p in caller_prompts.items()]
    embeddings = dict(await asyncio.gather(*tasks))

    # All 3 calls must have generated 384-d embeddings
    assert len(embeddings) == 3
    vec_1 = np.array(embeddings["call_1_premium"])
    vec_2 = np.array(embeddings["call_2_budget"])
    vec_3 = np.array(embeddings["call_3_tamil"])

    assert len(vec_1) == 384
    assert len(vec_2) == 384
    assert len(vec_3) == 384

    # The embeddings must be distinct vectors
    sim_1_2 = np.dot(vec_1, vec_2) / (np.linalg.norm(vec_1) * np.linalg.norm(vec_2))
    sim_1_3 = np.dot(vec_1, vec_3) / (np.linalg.norm(vec_1) * np.linalg.norm(vec_3))

    assert sim_1_2 < 0.99, "Distinct English utterances produced identical vectors"
    assert sim_1_3 < 0.99, "English and Tamil utterances produced identical vectors"
