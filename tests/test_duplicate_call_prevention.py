import pytest
import asyncio
from datetime import datetime, timezone
from app.api.routes.local_voice_test import canonical_phone_key, ACTIVE_CALL_STATUSES


def test_canonical_phone_key_normalization():
    # Verify various phone formats resolve to the exact same canonical digit key
    assert canonical_phone_key("09585310712") == "9585310712"
    assert canonical_phone_key("9585310712") == "9585310712"
    assert canonical_phone_key("+919585310712") == "9585310712"
    assert canonical_phone_key("+91 958-531-0712") == "9585310712"
    assert canonical_phone_key("+1 (555) 019-2831") == "15550192831"
    assert canonical_phone_key("") == ""
    assert canonical_phone_key(None) == ""


def is_call_duplicate(
    target_phone: str,
    existing_calls: list[dict],
) -> bool:
    """Simulates active-call duplicate detection logic."""
    target_key = canonical_phone_key(target_phone)
    if not target_key:
        return False

    for c in existing_calls:
        if c.get("status") in ACTIVE_CALL_STATUSES and c.get("ended_at") is None:
            c_key = canonical_phone_key(c.get("client_phone")) or canonical_phone_key(c.get("to_number"))
            if c_key and c_key == target_key:
                return True
    return False


def test_scenario_1_first_call_allowed():
    # 1. First call to a number when no active calls exist -> allowed
    existing = []
    assert is_call_duplicate("09585310712", existing) is False


def test_scenario_2_second_call_while_ringing_rejected():
    # 2. Second call while first is RINGING -> rejected
    existing = [{
        "id": "call-1",
        "to_number": "9585310712",
        "client_phone": "09585310712",
        "status": "RINGING",
        "ended_at": None
    }]
    assert is_call_duplicate("09585310712", existing) is True
    assert is_call_duplicate("+919585310712", existing) is True


def test_scenario_3_second_call_while_in_progress_rejected():
    # 3. Second call while first is IN_PROGRESS or CONNECTED -> rejected
    existing = [{
        "id": "call-1",
        "to_number": "9585310712",
        "client_phone": "9585310712",
        "status": "CONNECTED",
        "ended_at": None
    }]
    assert is_call_duplicate("9585310712", existing) is True


def test_scenario_4_after_completed_allowed():
    # 4. Same number after previous call COMPLETED -> allowed
    existing = [{
        "id": "call-1",
        "to_number": "9585310712",
        "client_phone": "9585310712",
        "status": "COMPLETED",
        "ended_at": datetime.now(timezone.utc)
    }]
    assert is_call_duplicate("9585310712", existing) is False


def test_scenario_5_after_failed_allowed():
    # 5. Same number after previous call FAILED -> allowed
    existing = [{
        "id": "call-1",
        "to_number": "9585310712",
        "client_phone": "9585310712",
        "status": "FAILED",
        "ended_at": datetime.now(timezone.utc)
    }]
    assert is_call_duplicate("9585310712", existing) is False


def test_scenario_6_after_hung_up_allowed():
    # 6. Same number after previous call HUNG_UP/ENDED -> allowed
    existing = [{
        "id": "call-1",
        "to_number": "9585310712",
        "client_phone": "09585310712",
        "status": "ENDED",
        "ended_at": datetime.now(timezone.utc)
    }]
    assert is_call_duplicate("09585310712", existing) is False


def test_scenario_7_different_destination_allowed():
    # 7. Different destination while another call is active -> allowed
    existing = [{
        "id": "call-1",
        "to_number": "9585310712",
        "client_phone": "09585310712",
        "status": "IN_PROGRESS",
        "ended_at": None
    }]
    assert is_call_duplicate("9876543210", existing) is False


@pytest.mark.asyncio
async def test_scenario_8_concurrent_near_simultaneous_requests():
    # 8. Two near-simultaneous start requests -> only one outbound call is created
    lock = asyncio.Lock()
    active_db = []
    results = []

    async def simulate_start_request(req_id: str, phone: str):
        async with lock:
            if is_call_duplicate(phone, active_db):
                results.append((req_id, "REJECTED_409"))
                return
            # Register active call
            active_db.append({
                "id": f"call-{req_id}",
                "to_number": phone,
                "client_phone": phone,
                "status": "RINGING",
                "ended_at": None
            })
            results.append((req_id, "ALLOWED"))

    # Launch two requests 10ms apart concurrently
    task1 = asyncio.create_task(simulate_start_request("A", "09585310712"))
    await asyncio.sleep(0.005) # 5ms
    task2 = asyncio.create_task(simulate_start_request("B", "9585310712"))

    await asyncio.gather(task1, task2)

    statuses = [r[1] for r in results]
    assert statuses.count("ALLOWED") == 1
    assert statuses.count("REJECTED_409") == 1
    assert len(active_db) == 1
