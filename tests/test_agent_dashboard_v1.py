import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone

from app.main import app
from app.db.database import get_db, AsyncSessionLocal
from app.db.models import Campaign, Call, Lead, CampaignLead, Business, User
from app.services.agent_dashboard_service import AgentDashboardService
from app.schemas.agent_dashboard import (
    DashboardFiltersQuery,
    AgentHeaderInfo,
    AgentDashboardSummary,
    CallsPageResponse,
    CallDetailResponse,
    QueuePageResponse,
    HistoryPageResponse,
    ReportsResponse,
    TranscriptsSearchResponse,
    FilterOptionsResponse,
)


@pytest.mark.asyncio
async def test_agent_dashboard_404_for_unknown_agent():
    async with AsyncSessionLocal() as db:
        with pytest.raises(Exception) as exc_info:
            await AgentDashboardService.get_header("non-existent-agent-id-12345", db)
        assert "404" in str(exc_info.value) or "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_agent_dashboard_service_with_campaign():
    uid = uuid.uuid4().hex[:8]
    camp_id = f"test-camp-{uid}"
    call_id = f"test-call-{uid}"
    lead_id = f"test-lead-{uid}"

    async with AsyncSessionLocal() as db:
        # Create a test campaign
        test_camp = Campaign(
            id=camp_id,
            name=f"QA Campaign {uid}",
            status="RUNNING",
            agent_type="AI_VOICE",
            twilio_number="+15551234567",
            business_id="c3dce06c-8330-4724-ba33-b5282b3c3596",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(test_camp)

        # Create a test lead
        test_lead = Lead(
            id=lead_id,
            first_name="Jane",
            last_name="Doe",
            phone="+15559876543",
            email="jane@example.com",
            business_id="c3dce06c-8330-4724-ba33-b5282b3c3596",
        )
        db.add(test_lead)

        # Create a test call
        test_call = Call(
            id=call_id,
            campaign_id=camp_id,
            lead_id=lead_id,
            business_id="c3dce06c-8330-4724-ba33-b5282b3c3596",
            client_name="Jane Doe",
            to_number="+15559876543",
            status="COMPLETED",
            duration_seconds=125,
            call_result="Interested",
            transcript="AI: Hello Jane!\nLead: Hi, tell me more about pricing.",
        )
        db.add(test_call)

        # Create queue lead
        q_lead = CampaignLead(
            id=f"q-{uid}",
            campaign_id=camp_id,
            lead_id=lead_id,
            status="PENDING",
            attempt_count=1,
        )
        db.add(q_lead)

        await db.commit()

        # 1. Header
        header = await AgentDashboardService.get_header(camp_id, db)
        assert isinstance(header, AgentHeaderInfo)
        assert header.name == f"QA Campaign {uid}"
        assert header.type == "AI Agent"
        assert header.phone_number == "+15551234567"

        # 2. Summary
        filters = DashboardFiltersQuery()
        summary = await AgentDashboardService.get_summary(camp_id, db, filters)
        assert isinstance(summary, AgentDashboardSummary)
        assert summary.kpis.total_calls >= 1
        assert summary.kpis.connected_calls >= 1
        assert summary.kpis.leads_in_queue >= 1
        assert summary.kpis.cost_metric.available is False

        # 3. Calls
        calls_res = await AgentDashboardService.get_calls(camp_id, db, filters)
        assert isinstance(calls_res, CallsPageResponse)
        assert len(calls_res.items) >= 1
        assert calls_res.items[0].lead_name == "Jane Doe"
        assert calls_res.items[0].outcome == "Interested"

        # 4. Call Detail
        detail = await AgentDashboardService.get_call_detail(camp_id, call_id, db)
        assert isinstance(detail, CallDetailResponse)
        assert detail.call_id == call_id
        assert "Hello Jane" in (detail.transcript or "")

        # 5. Queue
        queue_res = await AgentDashboardService.get_queue(camp_id, db, page=1, page_size=10)
        assert isinstance(queue_res, QueuePageResponse)
        assert len(queue_res.items) >= 1

        # 6. History
        hist_res = await AgentDashboardService.get_history(camp_id, db, page=1, page_size=10)
        assert isinstance(hist_res, HistoryPageResponse)
        assert len(hist_res.items) >= 1

        # 7. Reports
        reports_res = await AgentDashboardService.get_reports(camp_id, db, granularity="day")
        assert isinstance(reports_res, ReportsResponse)
        assert reports_res.total_calls >= 1

        # 8. Transcripts Search
        t_res = await AgentDashboardService.search_transcripts(camp_id, "pricing", db)
        assert isinstance(t_res, TranscriptsSearchResponse)
        assert len(t_res.items) >= 1
        assert "pricing" in t_res.items[0].matched_snippet.lower()

        # 9. Filter Options
        f_options = await AgentDashboardService.get_filter_options(camp_id, db)
        assert isinstance(f_options, FilterOptionsResponse)
        assert any(s.value == "COMPLETED" for s in f_options.statuses)

        # 10. Export CSV
        csv_bytes = await AgentDashboardService.export_stream(camp_id, db, export_type="csv", tab="calls")
        csv_text = csv_bytes.decode("utf-8")
        assert "Call ID" in csv_text
        assert "Jane Doe" in csv_text


@pytest.mark.asyncio
async def test_agent_dashboard_api_all_endpoints():
    uid = uuid.uuid4().hex[:8]
    camp_id = f"test-camp-api-{uid}"
    call_id = f"test-call-api-{uid}"

    async with AsyncSessionLocal() as db:
        test_camp = Campaign(
            id=camp_id,
            name="Test API Campaign",
            status="RUNNING",
            agent_type="AI_VOICE",
            twilio_number="+15551234567",
            business_id="c3dce06c-8330-4724-ba33-b5282b3c3596",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(test_camp)

        test_call = Call(
            id=call_id,
            campaign_id=camp_id,
            business_id="c3dce06c-8330-4724-ba33-b5282b3c3596",
            client_name="Bob Smith",
            to_number="+15553334444",
            status="COMPLETED",
            duration_seconds=90,
            call_result="Qualified",
            transcript="AI: Hello Bob!\nBob: I want to sign up.",
        )
        db.add(test_call)
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. 404 for unknown agent
        res_404 = await ac.get("/api/v1/agent-dashboard/non-existent-9999/header")
        assert res_404.status_code == 404

        # 2. Header
        res_header = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/header")
        assert res_header.status_code == 200
        assert res_header.json()["name"] == "Test API Campaign"

        # 3. Summary
        res_summary = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/summary")
        assert res_summary.status_code == 200
        assert res_summary.json()["kpis"]["total_calls"] >= 1

        # 4. Calls
        res_calls = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/calls?page=1&page_size=20")
        assert res_calls.status_code == 200
        assert len(res_calls.json()["items"]) >= 1

        # 5. Call Detail
        res_detail = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/calls/{call_id}")
        assert res_detail.status_code == 200
        assert res_detail.json()["call_id"] == call_id

        # 6. Queue
        res_queue = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/queue")
        assert res_queue.status_code == 200

        # 7. History
        res_hist = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/history")
        assert res_hist.status_code == 200

        # 8. Reports
        res_rep = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/reports?granularity=day")
        assert res_rep.status_code == 200

        # 9. Transcripts Search
        res_trans = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/transcripts?q=sign")
        assert res_trans.status_code == 200
        assert len(res_trans.json()["items"]) >= 1

        # 10. Filter Options
        res_opts = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/filter-options")
        assert res_opts.status_code == 200
        assert "statuses" in res_opts.json()

        # 11. Export CSV
        res_exp = await ac.get(f"/api/v1/agent-dashboard/{camp_id}/export?type=csv&tab=calls")
        assert res_exp.status_code == 200
        assert "attachment" in res_exp.headers.get("content-disposition", "")
