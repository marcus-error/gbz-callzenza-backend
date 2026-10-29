import asyncio
import time
from app.services.ai.llm_provider_manager import LLMProviderManager

async def main():
    mgr = LLMProviderManager.get_instance()
    t0 = time.perf_counter()
    first_chunk_time = None
    chunks = []
    async for chunk in mgr.generate_stream(
        prompt="The customer said: 'What are your store hours today?' Respond in 1 brief sentence.",
        max_tokens=60
    ):
        if first_chunk_time is None:
            first_chunk_time = time.perf_counter() - t0
        chunks.append(chunk)
    
    total_time = time.perf_counter() - t0
    full_text = "".join(chunks)
    print(f"First token: {first_chunk_time:.3f}s | Total: {total_time:.3f}s | Response: '{full_text.strip()}'")

if __name__ == "__main__":
    asyncio.run(main())
