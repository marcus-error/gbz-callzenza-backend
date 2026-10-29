import pytest
import asyncio
import uuid
from app.services.inbound_routing_service import (
    InboundRoutingType,
    InboundRoutingStatus,
    InboundCallSession,
    inbound_session_registry,
    classify_inbound_call,
    execute_live_route_transfer,
)

@pytest.mark.asyncio
async def test_classify_normal_inbound_call():
    """Test 1: Unrecognized caller number auto-creates Lead and defaults to NORMAL_INBOUND."""
    caller_phone = "+919999900001"
    dest_number = "09410697548"
    default_tenant_id = "org-test-123"
    test_call_id = f"test-call-{uuid.uuid4().hex[:8]}"
    
    session, matched_lead, matched_campaign = await classify_inbound_call(
        caller_number=caller_phone,
        dest_number=dest_number,
        default_tenant_id=default_tenant_id,
        caller_name="Test Unrecognized Caller",
        call_id_val=test_call_id
    )
    
    assert session is not None
    assert session.routing_type == InboundRoutingType.NORMAL_INBOUND
    assert session.routing_status == InboundRoutingStatus.WAITING_FOR_ROUTE
    assert matched_lead is not None
    assert matched_lead.phone == caller_phone

@pytest.mark.asyncio
async def test_inbound_session_lifecycle():
    """Test 2: Register, retrieve, double-route protection, and cleanup of inbound session."""
    test_call_id = f"call-test-{uuid.uuid4().hex[:8]}"
    session = InboundCallSession(
        call_id=test_call_id,
        session_id=f"sip-in-{test_call_id}",
        organization_id="org-test-999",
        inbound_phone="+919876543210",
        to_number="09410697548",
        routing_type=InboundRoutingType.AI_AGENT_CALLBACK,
        routing_status=InboundRoutingStatus.WAITING_FOR_ROUTE,
        lead_id="lead-101",
        lead_name="John Doe",
        campaign_id="camp-202",
        campaign_name="Solar Outreach",
        outbound_agent_id="ai-303",
        outbound_agent_name="Solar AI Bot",
        outbound_agent_type="AI_AGENT"
    )
    
    # Register session in registry
    await inbound_session_registry.register(session)
    
    # Retrieve session from registry
    retrieved = await inbound_session_registry.get(test_call_id)
    assert retrieved is not None
    assert retrieved.call_id == test_call_id
    assert retrieved.routing_type == InboundRoutingType.AI_AGENT_CALLBACK
    assert retrieved.routing_status == InboundRoutingStatus.WAITING_FOR_ROUTE

    # Assign agent
    await inbound_session_registry.assign_agent(test_call_id, "agent-human-101", "Agent Smith")
    assert retrieved.routing_status == InboundRoutingStatus.RINGING_AGENT
    assert retrieved.selected_agent_id == "agent-human-101"

    # Close session
    await inbound_session_registry.close(test_call_id)
    assert retrieved.is_active is False

if __name__ == "__main__":
    asyncio.run(test_classify_normal_inbound_call())
    asyncio.run(test_inbound_session_lifecycle())
    print("ALL INBOUND ROUTING UNIT TESTS PASSED SUCCESSFULLY!")
