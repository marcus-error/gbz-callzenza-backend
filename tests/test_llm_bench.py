import asyncio
import time
from app.services.ai.llm_provider_manager import LLMProviderManager

async def main():
    mgr = LLMProviderManager.get_instance()
    t0 = time.perf_counter()
    resp = await mgr.generate(
        prompt="The customer said: 'What are your store hours today?' Respond in 1 brief sentence.",
        max_tokens=60
    )
    dur = time.perf_counter() - t0
    print(f"LLM Response: '{resp}' (took {dur:.3f}s)")

if __name__ == "__main__":
    asyncio.run(main())
