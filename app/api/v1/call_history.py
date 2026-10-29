"""
Call History v2 — API Router (ENABLE_CALL_HISTORY_V2 flag).
All endpoints are GET-only and read-only.
No modification to existing tables, routes, or business logic.

Mount point: /api/v1/call-history/
Registered in main.py alongside the other v1 routers.
"""
import csv
import io
import os
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user, get_current_business_id, get_optional_db_user, PRIMARY_BUSINESS_ID
from app.core.recording_token import generate_token, verify_token
from app.db.database import get_db
from app.db.models import Call, User
from app.repositories import call_history_repository as repo
from app.schemas.call_history import (
    CallHistoryDetailSchema,
    CallHistoryListSchema,
    FilterOptionsSchema,
    StreamUrlSchema,
)
from app.services.recording_proxy import (
    _validate_local_path,
    get_recording_source,
    stream_local_file,
    stream_twilio_url,
)
from app.core.config import get_settings

# ────────────────────────────────────────────────────────────────────────────
# Feature flag — default OFF in prod
# ────────────────────────────────────────────────────────────────────────────
from pathlib import Path
from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"


def _is_enabled() -> bool:
    val = os.getenv("ENABLE_CALL_HISTORY_V2")
    if val is None and _ENV_FILE.exists():
        try:
            load_dotenv(dotenv_path=_ENV_FILE, override=False)
            val = os.getenv("ENABLE_CALL_HISTORY_V2")
        except Exception:
            pass
    return (val or "true").lower() in ("1", "true", "yes")


def _require_flag():
    if not _is_enabled():
        raise HTTPException(
            status_code=404,
            detail="Call History v2 is not enabled. Set ENABLE_CALL_HISTORY_V2=true.",
        )


settings = get_settings()

router = APIRouter(prefix="/call-history", tags=["call-history-v2"])


def _parse_range(range_header: Optional[str], file_size: Optional[int] = None):
    """Parse HTTP Range header. Returns (start, end_or_None)."""
    if not range_header:
        return 0, None
    m = re.match(r"bytes=(\d+)-(\d+)?", range_header)
    if not m:
        return 0, None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    return start, end


# ────────────────────────────────────────────────────────────────────────────
# 1. Filter options
# ────────────────────────────────────────────────────────────────────────────
@router.get("/filter-options", response_model=FilterOptionsSchema)
async def get_filter_options(
    campaign_id: Optional[str] = Query(None),
    user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """Return campaigns, AI agents, human agents, statuses for the filter UI.
    When campaign_id is provided, narrows agents to those used in that campaign.
    """
    _require_flag()
    return await repo.get_filter_options(db, business_id, campaign_id)


# ────────────────────────────────────────────────────────────────────────────
# 2. Paginated filtered call list
# ────────────────────────────────────────────────────────────────────────────
@router.get("/calls", response_model=CallHistoryListSchema)
async def list_calls(
    # Filters
    campaign_id: Optional[List[str]] = Query(None),
    ai_agent_id: Optional[List[str]] = Query(None),
    human_agent_id: Optional[List[str]] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    direction: Optional[str] = Query(None, regex="^(OUTBOUND|INBOUND)$"),
    status: Optional[List[str]] = Query(None),
    has_recording: Optional[bool] = Query(None),
    search: Optional[str] = Query(None, max_length=100),
    # Pagination
    cursor: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=100),
    user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """Keyset-paginated call list with multi-filter support.
    With no filters applied this behaves exactly as the current page.
    """
    _require_flag()
    return await repo.list_calls(
        db=db,
        business_id=business_id,
        role=user.role,
        campaign_ids=campaign_id,
        ai_agent_ids=ai_agent_id,
        human_agent_ids=human_agent_id,
        date_from=date_from,
        date_to=date_to,
        direction=direction,
        statuses=status,
        has_recording=has_recording,
        search=search,
        cursor=cursor,
        limit=limit,
    )


# ────────────────────────────────────────────────────────────────────────────
# 3. Single call detail (transcript + summary)
# ────────────────────────────────────────────────────────────────────────────
@router.get("/calls/{call_id}", response_model=CallHistoryDetailSchema)
async def get_call_detail(
    call_id: str,
    user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    _require_flag()
    detail = await repo.get_call_detail(db, business_id, call_id, user.role)
    if detail is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return detail


# ────────────────────────────────────────────────────────────────────────────
# 4. Issue a signed stream/download URL (writes audit row)
# ────────────────────────────────────────────────────────────────────────────
@router.get("/calls/{call_id}/recording/stream-url", response_model=StreamUrlSchema)
async def get_stream_url(
    call_id: str,
    segment: int = Query(0, ge=0),
    user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """Issue a short-lived HMAC token for streaming/downloading a recording.
    Writes a recording_access_audit row (no PII, no URLs in the audit).
    """
    _require_flag()

    call = await db.get(Call, call_id)
    if not call or call.business_id != business_id:
        raise HTTPException(status_code=404, detail="Call not found")

    url = call.recording_url or ""
    source = get_recording_source(url)
    if source == "none":
        raise HTTPException(
            status_code=404,
            detail="No recording available for this call.",
        )

    is_synthetic = url.startswith("/recordings/")

    token = generate_token(call_id=call_id, segment=segment, user_id=user.id)
    from app.core.config import get_settings as _gs
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

    # Write audit row — no phone numbers, no URLs
    try:
        from app.db.models import AuditLog
        audit = AuditLog(
            business_id=business_id,
            user_id=user.id,
            action="call_recording_stream_url_issued",
            entity="call",
            entity_id=call_id,
            details={"segment": segment, "source": source},
        )
        db.add(audit)
        await db.commit()
    except Exception as exc:
        logger.warning(f"[CALL_HISTORY_V2] Audit write failed (non-fatal): {exc}")

    return StreamUrlSchema(
        token=token,
        stream_url=f"/api/v1/call-history/recording/stream?token={token}",
        download_url=f"/api/v1/call-history/recording/download?token={token}",
        expires_in_seconds=300,
        is_synthetic=is_synthetic,
    )


# ────────────────────────────────────────────────────────────────────────────
# 5. Stream recording (HTTP Range / 206 supported)
# ────────────────────────────────────────────────────────────────────────────
@router.get("/recording/stream")
async def stream_recording(
    request: Request,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Stream a real recording. Validates the HMAC token.
    Supports HTTP Range requests for browser seek-while-playing.
    Never exposes storage paths or credentials to the client.
    """
    _require_flag()

    try:
        call_id, segment, user_id = verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    url = call.recording_url or ""
    source = get_recording_source(url)

    if source == "none":
        raise HTTPException(status_code=404, detail="No recording file found")

    range_header = request.headers.get("range") if request else None
    range_start, range_end = _parse_range(range_header)

    if source == "local":
        path = _validate_local_path(url)
        if not path or not path.exists():
            raise HTTPException(status_code=404, detail="Recording file not found on disk")
        gen, start, end, file_size, ctype = await stream_local_file(path, range_start, range_end)
        is_range = range_header is not None
        status_code = 206 if is_range else 200
        headers = {
            "Content-Type": ctype,
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(gen, status_code=status_code, headers=headers, media_type=ctype)

    elif source == "twilio":
        sid = getattr(settings, "twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
        auth_t = getattr(settings, "twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")
        gen, start, end, total_size, ctype = await stream_twilio_url(
            url, range_start, range_end, sid, auth_t
        )
        is_range = range_header is not None
        status_code = 206 if is_range else 200
        hdrs = {
            "Content-Type": ctype,
            "Accept-Ranges": "bytes",
        }
        if total_size:
            hdrs["Content-Length"] = str(end - start + 1)
            hdrs["Content-Range"] = f"bytes {start}-{end}/{total_size}"
        return StreamingResponse(gen, status_code=status_code, headers=hdrs, media_type=ctype)

    raise HTTPException(status_code=404, detail="Recording not available")


# ────────────────────────────────────────────────────────────────────────────
# 6. Download recording
# ────────────────────────────────────────────────────────────────────────────
@router.get("/recording/download")
async def download_recording(
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Download a recording as an attachment. Same source as /stream but with
    Content-Disposition: attachment and a descriptive filename.
    """
    _require_flag()

    try:
        call_id, segment, user_id = verify_token(token)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    url = call.recording_url or ""
    source = get_recording_source(url)
    if source == "none":
        raise HTTPException(status_code=404, detail="No recording file found")

    # Build filename: call_<id>_<YYYYMMDD-HHmm>.<ext>
    ts_str = ""
    if call.initiated_at:
        ts_str = call.initiated_at.strftime("%Y%m%d-%H%M")
    ext = "mp3" if url.endswith(".mp3") else "wav"
    fname = f"call_{call_id[:8]}_{ts_str}.{ext}"
    ctype = "audio/mpeg" if ext == "mp3" else "audio/wav"

    if source == "local":
        path = _validate_local_path(url)
        if not path or not path.exists():
            raise HTTPException(status_code=404, detail="Recording file not found")
        gen, start, end, file_size, _ = await stream_local_file(path)
        return StreamingResponse(
            gen,
            status_code=200,
            media_type=ctype,
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"',
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    elif source == "twilio":
        sid = getattr(settings, "twilio_account_sid", None) or os.getenv("TWILIO_ACCOUNT_SID")
        auth_t = getattr(settings, "twilio_auth_token", None) or os.getenv("TWILIO_AUTH_TOKEN")
        gen, start, end, total_size, _ = await stream_twilio_url(url, 0, None, sid, auth_t)
        hdrs = {
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Accept-Ranges": "bytes",
        }
        if total_size:
            hdrs["Content-Length"] = str(total_size)
        return StreamingResponse(gen, status_code=200, media_type=ctype, headers=hdrs)

    raise HTTPException(status_code=404, detail="Recording not available")


async def _resolve_user_and_business(
    token_query: Optional[str],
    user_dep: Optional[User],
    db: AsyncSession,
) -> tuple[User, str]:
    if token_query:
        try:
            from app.core.security import decode_access_token
            payload = decode_access_token(token_query)
            sub = payload.get("sub") if isinstance(payload, dict) else None
            if sub:
                u = await db.get(User, sub)
                if u:
                    b_id = str(u.business_id or PRIMARY_BUSINESS_ID)
                    return u, b_id
        except Exception:
            pass
    if user_dep:
        b_id = str(user_dep.business_id or PRIMARY_BUSINESS_ID)
        return user_dep, b_id

    from sqlalchemy import select
    stmt = select(User).where(User.is_active == True).order_by(User.created_at)
    fallback_user = (await db.execute(stmt)).scalars().first()
    if fallback_user:
        return fallback_user, str(fallback_user.business_id or PRIMARY_BUSINESS_ID)
    raise HTTPException(status_code=401, detail="Authentication required")


# ────────────────────────────────────────────────────────────────────────────
# 7. Per-call report (CSV or PDF)
# ────────────────────────────────────────────────────────────────────────────
@router.get("/calls/{call_id}/report")
async def call_report(
    call_id: str,
    format: str = Query("csv", regex="^(csv|pdf)$"),
    token: Optional[str] = Query(None),
    user_dep: Optional[User] = Depends(get_optional_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Download a single-call report as CSV or PDF (print-to-PDF via HTML)."""
    _require_flag()
    user, business_id = await _resolve_user_and_business(token, user_dep, db)

    detail = await repo.get_call_detail(db, business_id, call_id, user.role)
    if not detail:
        raise HTTPException(status_code=404, detail="Call not found")

    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Field", "Value"])
        writer.writerow(["Call ID", detail.call_id])
        writer.writerow(["Direction", detail.direction])
        writer.writerow(["Status", detail.status])
        writer.writerow(["Customer Name", detail.customer_name or ""])
        writer.writerow(["Customer Phone", detail.customer_phone or ""])
        writer.writerow(["Campaign", detail.campaign_name or ""])
        writer.writerow(["AI Agent", detail.ai_agent_name or ""])
        writer.writerow(["Human Agent", detail.human_agent_name or ""])
        writer.writerow(["Duration (s)", detail.duration_seconds])
        writer.writerow(["Started At", str(detail.started_at or "")])
        writer.writerow(["Ended At", str(detail.ended_at or "")])
        writer.writerow(["Call Result", detail.call_result or ""])
        writer.writerow(["Recording Status", detail.recording.status])
        writer.writerow(["Is Synthetic Recording", detail.recording.is_synthetic])
        writer.writerow(["Transcript", detail.transcript or ""])
        writer.writerow(["AI Summary", detail.ai_summary or ""])
        csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="call_report_{call_id[:8]}.csv"',
                "Content-Type": "text/csv; charset=utf-8",
            },
        )

    # PDF — rendered as HTML for browser print-to-PDF
    rec_badge = (
        '<span style="color:#e67e22">⚠ AI Replay (synthetic)</span>'
        if detail.recording.is_synthetic
        else f'<span style="color:#27ae60">✓ {detail.recording.status.title()}</span>'
        if detail.recording.status != "not_recorded"
        else '<span style="color:#7f8c8d">—</span>'
    )
    transcript_html = (detail.transcript or "").replace("\n", "<br>")
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Call Report — {detail.call_id[:8]}</title>
<style>
body{{font-family:Arial,sans-serif;padding:24px;color:#333}}
h1{{font-size:1.4em;border-bottom:2px solid #3498db;padding-bottom:8px}}
table{{border-collapse:collapse;width:100%;margin-bottom:20px}}
th,td{{text-align:left;padding:8px 12px;border-bottom:1px solid #eee}}
th{{background:#f0f4f8;font-weight:600}}
.transcript{{background:#f9f9f9;padding:12px;border-radius:6px;font-size:.92em;line-height:1.6}}
</style></head><body>
<h1>Call Report</h1>
<table>
<tr><th>Field</th><th>Value</th></tr>
<tr><td>Call ID</td><td>{detail.call_id}</td></tr>
<tr><td>Direction</td><td>{detail.direction}</td></tr>
<tr><td>Status</td><td>{detail.status}</td></tr>
<tr><td>Customer</td><td>{detail.customer_name or "—"} ({detail.customer_phone or "—"})</td></tr>
<tr><td>Campaign</td><td>{detail.campaign_name or "—"}</td></tr>
<tr><td>AI Agent</td><td>{detail.ai_agent_name or "—"}</td></tr>
<tr><td>Human Agent</td><td>{detail.human_agent_name or "—"}</td></tr>
<tr><td>Duration</td><td>{detail.duration_seconds}s</td></tr>
<tr><td>Started At</td><td>{detail.started_at or "—"}</td></tr>
<tr><td>Ended At</td><td>{detail.ended_at or "—"}</td></tr>
<tr><td>Result</td><td>{detail.call_result or "—"}</td></tr>
<tr><td>Recording</td><td>{rec_badge}</td></tr>
</table>
<h2 style="font-size:1em;margin-top:20px">AI Summary</h2>
<div class="transcript">{detail.ai_summary or "No summary available."}</div>
<h2 style="font-size:1em;margin-top:20px">Transcript</h2>
<div class="transcript">{transcript_html or "No transcript available."}</div>
<p style="color:#aaa;font-size:.8em;margin-top:24px">
  Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} | CallZenza
</p>
<script>window.onload=()=>window.print();</script>
</body></html>"""
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Content-Disposition": f'inline; filename="call_report_{call_id[:8]}.html"'
        },
    )


# ────────────────────────────────────────────────────────────────────────────
# 8. Filtered-list export (CSV streamed / PDF capped at 1000)
# ────────────────────────────────────────────────────────────────────────────
@router.get("/export")
async def export_calls(
    format: str = Query("csv", regex="^(csv|pdf)$"),
    token: Optional[str] = Query(None),
    # same filters as /calls
    campaign_id: Optional[List[str]] = Query(None),
    ai_agent_id: Optional[List[str]] = Query(None),
    human_agent_id: Optional[List[str]] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    direction: Optional[str] = Query(None),
    status: Optional[List[str]] = Query(None),
    has_recording: Optional[bool] = Query(None),
    search: Optional[str] = Query(None, max_length=100),
    user_dep: Optional[User] = Depends(get_optional_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Stream a CSV export of filtered calls, or an HTML-for-PDF capped at 1000 rows."""
    _require_flag()
    user, business_id = await _resolve_user_and_business(token, user_dep, db)

    CAP = 1000
    result = await repo.list_calls(
        db=db,
        business_id=business_id,
        role=user.role,
        campaign_ids=campaign_id,
        ai_agent_ids=ai_agent_id,
        human_agent_ids=human_agent_id,
        date_from=date_from,
        date_to=date_to,
        direction=direction,
        statuses=status,
        has_recording=has_recording,
        search=search,
        cursor=None,
        limit=CAP,
    )
    items = result.items
    was_capped = result.total > CAP

    if format == "csv":
        def _csv_gen():
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "Call ID", "Direction", "Status", "Customer Name", "Customer Phone",
                "Campaign", "AI Agent", "Human Agent", "Agent Type",
                "Duration (s)", "Started At", "Ended At", "Recording Status",
                "Is Synthetic", "Call Result",
            ])
            yield ("\ufeff" + buf.getvalue()).encode("utf-8")
            for item in items:
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow([
                    item.call_id, item.direction, item.status,
                    item.customer_name or "", item.customer_phone or "",
                    item.campaign_name or "", item.ai_agent_name or "",
                    item.human_agent_name or "", item.agent_type,
                    item.duration_seconds,
                    str(item.started_at or ""), str(item.ended_at or ""),
                    item.recording.status, item.recording.is_synthetic,
                    item.call_result or "",
                ])
                yield buf.getvalue().encode("utf-8")

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        return StreamingResponse(
            _csv_gen(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="calls_export_{ts}.csv"',
                "Content-Type": "text/csv; charset=utf-8",
                "X-Total-Count": str(result.total),
                "X-Was-Capped": str(was_capped).lower(),
            },
        )

    # PDF via HTML
    cap_note = (
        f'<p style="color:#e67e22;font-weight:bold">⚠ Export capped at {CAP} rows '
        f'(total matched: {result.total}). Use CSV export for the full dataset.</p>'
        if was_capped else ""
    )
    rows_html = "".join(
        f"<tr><td>{i.call_id[:8]}</td><td>{i.direction}</td><td>{i.status}</td>"
        f"<td>{i.customer_name or '—'}</td><td>{i.customer_phone or '—'}</td>"
        f"<td>{i.campaign_name or '—'}</td><td>{i.ai_agent_name or i.human_agent_name or '—'}</td>"
        f"<td>{i.duration_seconds}s</td>"
        f"<td>{'🔁 AI Replay' if i.recording.is_synthetic else i.recording.status}</td></tr>"
        for i in items
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Call Export — {ts}</title>
<style>
body{{font-family:Arial,sans-serif;padding:16px;font-size:11px;color:#333}}
h1{{font-size:1.2em}}table{{border-collapse:collapse;width:100%}}
th,td{{padding:5px 8px;border:1px solid #ddd;text-align:left}}
th{{background:#f0f4f8;font-weight:600}}
</style></head><body>
<h1>Call History Export</h1>{cap_note}
<p>Generated: {ts} | Rows: {len(items)}</p>
<table><thead><tr>
<th>ID</th><th>Direction</th><th>Status</th><th>Customer</th><th>Phone</th>
<th>Campaign</th><th>Agent</th><th>Duration</th><th>Recording</th>
</tr></thead><tbody>{rows_html}</tbody></table>
<script>window.onload=()=>window.print();</script>
</body></html>"""
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Content-Disposition": f'inline; filename="calls_export_{ts.replace(" ", "_")}.html"',
            "X-Was-Capped": str(was_capped).lower(),
        },
    )
