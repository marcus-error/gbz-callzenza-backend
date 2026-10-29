import asyncio
import time
from app.conversation.engine import ConversationalEngine
from app.conversation.session import SessionManager
from tests.test_points_20_and_21_exact_suite import NURSE_OCLOCK_SCRIPT, SOLAR_CAMPAIGN_SCRIPT
from app.services.sip_ai_bot import synthesize_text_to_mulaw

async def benchmark_script(name, script, turns):
    print(f"\n=======================================================")
    print(f"  BENCHMARK: {name}")
    print(f"=======================================================")
    sess_id = f"bench_{name.lower().replace(' ', '_')}"
    SessionManager.clear_session(sess_id)
    
    total_engine_ms = 0
    total_tts_ms = 0
    
    for idx, user_input in enumerate(turns):
        t0 = time.perf_counter()
        res = await ConversationalEngine.process_turn(sess_id, user_input, script, f"camp_{name}")
        t_engine = (time.perf_counter() - t0) * 1000
        total_engine_ms += t_engine
        
        reply = res["response"]
        t1 = time.perf_counter()
        audio = await synthesize_text_to_mulaw(reply)
        t_tts = (time.perf_counter() - t1) * 1000
        total_tts_ms += t_tts
        
        total_t = t_engine + t_tts
        print(f"\nTurn {idx+1}: Caller -> '{user_input}'")
        print(f"  Agent: '{reply}'")
        print(f"  Step ID: {res['current_step_id']} | Completed Steps: {res['completed_steps']} | Data: {res['collected_data']}")
        print(f"  Turn Complete: {res['is_complete']}")
        print(f"  Engine Latency: {t_engine:.1f}ms | TTS Latency: {t_tts:.1f}ms | Total Turn Latency: {total_t:.1f}ms | Audio Size: {len(audio) if audio else 0} bytes")
    
    avg_engine = total_engine_ms / len(turns)
    avg_tts = total_tts_ms / len(turns)
    print(f"\nSummary for {name}:")
    print(f"  Avg Engine Latency: {avg_engine:.1f}ms")
    print(f"  Avg TTS Latency: {avg_tts:.1f}ms")
    print(f"  Avg Total Response Time: {(avg_engine + avg_tts):.1f}ms")

async def main():
    nurse_turns = [
        "",
        "can you list the services?",
        "idk",
        "Physiotherapy",
        "My name is Ravi",
        "I am 35",
        "tomorrow at 10 am",
        "Yes, that is correct."
    ]
    await benchmark_script("Nurse O Clock", NURSE_OCLOCK_SCRIPT, nurse_turns)
    
    solar_turns = [
        "",
        "Hello, yes I am doing good.",
        "I own my home.",
        "$180 per month.",
        "Austin Texas",
        "Shingle roof",
        "Next Monday",
        "Yes, that is right."
    ]
    await benchmark_script("SunPower Solar", SOLAR_CAMPAIGN_SCRIPT, solar_turns)

if __name__ == "__main__":
    asyncio.run(main())
