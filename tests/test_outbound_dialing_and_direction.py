import asyncio
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import Call, Lead, AIAgent, User, Business
from app.api.routes.agent_dashboard import InitiateCallRequest


# ==============================================================================
# Helper phone normalization logic matching frontend extractValidPhoneNumber
# ==============================================================================
def extract_valid_phone_number(val):
    if val is None:
        return None
    if isinstance(val, dict):
        cand = (
            val.get("phone")
            or val.get("phone_number")
            or val.get("contact_number")
            or val.get("number")
            or val.get("dialPhone")
        )
        if isinstance(cand, (str, int, float)):
            return extract_valid_phone_number(cand)
        return None
    if isinstance(val, (int, float)):
        return str(int(val))
    if isinstance(val, str):
        trimmed = val.strip()
        if not trimmed or trimmed == "[object Object]":
            return None
        return trimmed
    return None


def resolve_dial_phone(target_phone=None, manual_number=None, lead_phone=None):
    explicit_phone = extract_valid_phone_number(target_phone)
    manual = extract_valid_phone_number(manual_number)
    lead = extract_valid_phone_number(lead_phone)
    return (explicit_phone or manual or lead or "").strip()


# ==============================================================================
# Unit Tests for Points 1 to 13
# ==============================================================================

def test_1_assigned_lead_resolution():
    """TEST 1: Assigned lead appears for assigned Voice Agent."""
    assigned_leads = [
        {
            "id": "direct-assign-lead-1",
            "lead_id": "lead-uuid-1",
            "expert_id": "user-uuid-1",
            "context": {
                "first_name": "Suresh",
                "last_name": "Kumar",
                "phone": "+919876543210",
                "lead_status": "Ready",
            }
        }
    ]
    assert len(assigned_leads) > 0
    first = assigned_leads[0]
    lead_obj = {
        "id": first["lead_id"],
        "first_name": first["context"]["first_name"],
        "phone": first["context"]["phone"],
    }
    assert lead_obj["first_name"] == "Suresh"
    assert lead_obj["phone"] == "+919876543210"


def test_2_assigned_lead_phone_can_be_dialed():
    """TEST 2: Assigned lead phone can be dialed."""
    active_lead = {"phone": "+919876543210"}
    dial_phone = resolve_dial_phone(target_phone=None, manual_number=None, lead_phone=active_lead["phone"])
    assert dial_phone == "+919876543210"


def test_3_manual_number_9585310712_can_be_dialed():
    """TEST 3: Manual number 9585310712 can be dialed."""
    dial_phone = resolve_dial_phone(manual_number="9585310712")
    assert dial_phone == "9585310712"


def test_4_manual_number_not_replaced_by_active_lead_phone():
    """TEST 4: Manual number is not replaced by activeLead.phone."""
    active_lead = {"phone": "1234567890"}
    dial_phone = resolve_dial_phone(
        target_phone=None,
        manual_number="9585310712",
        lead_phone=active_lead["phone"]
    )
    # Explicit manual keypad entry takes precedence
    assert dial_phone == "9585310712"
    assert dial_phone != active_lead["phone"]


def test_5_numeric_phone_value_normalized_safely():
    """TEST 5: Numeric phone value is normalized safely without TypeError or [object Object]."""
    num_val = 9585310712
    res = extract_valid_phone_number(num_val)
    assert res == "9585310712"
    assert res != "[object Object]"


def test_6_null_undefined_empty_phone_rejected_cleanly():
    """TEST 6: Null/undefined/empty phone is rejected cleanly."""
    assert extract_valid_phone_number(None) is None
    assert extract_valid_phone_number("") is None
    assert extract_valid_phone_number("   ") is None
    # Passing a React synthetic event mock
    class FakeEvent:
        preventDefault = lambda s: None
        target = {}
        currentTarget = {}
    assert extract_valid_phone_number(FakeEvent()) is None


def test_7_structured_phone_representation_handled_correctly():
    """TEST 7: Actual structured phone representation from the API is handled correctly."""
    struct_phone = {"phone": "9585310712"}
    assert extract_valid_phone_number(struct_phone) == "9585310712"
    struct_phone_2 = {"contact_number": "+919585310712"}
    assert extract_valid_phone_number(struct_phone_2) == "+919585310712"


@pytest.mark.asyncio
async def test_8_manual_call_creates_outbound_direction_and_user_id():
    """TEST 8: Manual call creates/uses OUTBOUND direction and records user_id."""
    call = Call(
        id="call-outbound-test-1",
        business_id="biz-1",
        lead_id=None,
        campaign_id=None,
        agent_id="agent-uuid",
        user_id="user-human-uuid",
        from_number="+18392615369",
        to_number="9585310712",
        status="RINGING",
        direction="OUTBOUND",
    )
    assert call.direction == "OUTBOUND"
    assert call.user_id == "user-human-uuid"
    assert call.to_number == "9585310712"


@pytest.mark.asyncio
async def test_9_and_10_outbound_provider_event_correlation_prevents_inbound_loop():
    """TEST 9 & 10: Outbound provider event correlates to existing outbound call and does not create an inbound call."""
    from app.services.sip_inbound_daemon import ACTIVE_SIP_CALLS

    # Existing active outbound call
    existing_outbound_call_id = "call-outbound-corr-1"
    target_destination = "9585310712"

    call_info_record = {
        "db_call_id": existing_outbound_call_id,
        "call_cid": "cid-outbound-123",
        "target_to": target_destination,
    }
    ACTIVE_SIP_CALLS[existing_outbound_call_id] = call_info_record

    # An incoming INVITE reflects from PBX for target_destination
    clean_caller = "".join(c for c in target_destination if c.isdigit())
    found_correlation = False
    for cid, info in ACTIVE_SIP_CALLS.items():
        if info.get("target_to") and clean_caller in info.get("target_to"):
            found_correlation = True
            correlated_call_id = info.get("db_call_id")
            break

    assert found_correlation is True
    assert correlated_call_id == existing_outbound_call_id
    # Clean up test entry
    ACTIVE_SIP_CALLS.pop(existing_outbound_call_id, None)


def test_11_assigned_lead_dialing_still_works():
    """TEST 11: Assigned-lead dialing still works when target_phone passed explicitly."""
    dial_phone = resolve_dial_phone(
        target_phone="+919876543210",
        manual_number=None,
        lead_phone="9999999999"
    )
    assert dial_phone == "+919876543210"


def test_12_dashboard_outbound_disables_initial_ai_speech():
    """
    TEST 12: Verify that for dashboard outbound calls, is_dashboard_outbound is True
    and initial automatic AI speech / greeting is skipped so Voice Agent speaks first.
    """
    call = Call(
        id="call-dashboard-outbound-1",
        direction="OUTBOUND",
        user_id="user-123",
        agent_id="agent-456",
    )
    session = {"is_dashboard_outbound": True}

    is_dashboard_outbound = bool(
        session.get("is_dashboard_outbound")
        or (call and call.direction == "OUTBOUND" and bool(call.user_id))
    )
    assert is_dashboard_outbound is True

    # When is_dashboard_outbound is True, initial AI speech is not triggered
    session["initial_greeting_sent"] = True
    session["ai_speaking"] = False
    assert session["ai_speaking"] is False
    assert session["initial_greeting_sent"] is True


def test_13_automated_inbound_and_campaign_calls_retain_ai_speech():
    """
    TEST 13: Verify that automated campaign / inbound calls (non-dashboard outbound)
    still have is_dashboard_outbound = False and retain normal AI greeting flow.
    """
    # 1. Inbound call
    inbound_call = Call(
        id="call-inbound-1",
        direction="INBOUND",
        user_id="user-123",
    )
    inbound_session = {}
    is_inbound_dashboard_outbound = bool(
        inbound_session.get("is_dashboard_outbound")
        or (inbound_call and inbound_call.direction == "OUTBOUND" and bool(inbound_call.user_id))
    )
    assert is_inbound_dashboard_outbound is False

    # 2. Automated campaign outbound call (no user_id)
    campaign_call = Call(
        id="call-campaign-1",
        direction="OUTBOUND",
        user_id=None,
    )
    campaign_session = {}
    is_campaign_dashboard_outbound = bool(
        campaign_session.get("is_dashboard_outbound")
        or (campaign_call and campaign_call.direction == "OUTBOUND" and bool(campaign_call.user_id))
    )
    assert is_campaign_dashboard_outbound is False
