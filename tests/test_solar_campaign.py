import pytest
from app.services.solar_campaign_service import (
    extract_and_update_state,
    compute_stage_progress,
    build_solar_campaign_prompt,
    DEFAULT_QUALIFICATION_DATA,
)

@pytest.mark.asyncio
async def test_extract_and_update_state_name_and_invalid_name():
    # Valid name extraction
    state = await extract_and_update_state(
        current_data=DEFAULT_QUALIFICATION_DATA,
        user_message="My name is John Doe"
    )
    assert state.get("firstName") == "John"
    assert state.get("lastName") == "Doe"
    assert state.get("invalidNameAttempt") is False

    # Invalid name extraction (numbers / gibberish)
    state = await extract_and_update_state(
        current_data=DEFAULT_QUALIFICATION_DATA,
        user_message="12345"
    )
    assert state.get("invalidNameAttempt") is True

@pytest.mark.asyncio
async def test_extract_and_update_state_homeownership_and_bill():
    state = await extract_and_update_state(
        current_data=DEFAULT_QUALIFICATION_DATA,
        user_message="I own my home and my monthly electricity bill is $250"
    )
    assert state.get("homeOwnership") == "Own"
    assert state.get("monthlyElectricityBill") == "$250"

def test_compute_stage_progress():
    state = dict(DEFAULT_QUALIFICATION_DATA)
    stage_idx, stage_name, script_steps = compute_stage_progress("IN_PROGRESS", state, history_length=1)
    assert stage_name == "1. Greeting"
    assert stage_idx == 0

    state["firstName"] = "Jane"
    state["homeOwnership"] = "Own"
    state["singleResidence"] = True
    state["taxPayer"] = True
    state["zipCode"] = "90210"
    state["monthlyElectricityBill"] = "$180"

    stage_idx, stage_name, script_steps = compute_stage_progress("IN_PROGRESS", state, history_length=6)
    assert stage_name == "4. Decision"


def test_build_solar_campaign_prompt_invalid_name():
    state = dict(DEFAULT_QUALIFICATION_DATA)
    state["invalidNameAttempt"] = True
    prompt = build_solar_campaign_prompt(
        agent_name="Alex",
        campaign_name="Solar Sales",
        script_text="Intro script",
        collected_data=state,
        qualification_status="IN_PROGRESS",
        history=[]
    )
    assert "real name" in prompt.lower()

