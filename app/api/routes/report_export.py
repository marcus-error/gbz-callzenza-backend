from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import AIAgent, Call, Campaign, Lead, User
from app.dependencies.tenant_context import get_current_tenant_id
from app.services.report_export import export_to_csv, export_to_excel, export_to_pdf

router = APIRouter(prefix="/api/reports/export", tags=["report-export"])


@router.get("/{slug}")
async def export_report(
    slug: str,
    format: str = Query("csv", pattern="^(csv|excel|xlsx|pdf)$"),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> Response:
    """Export tenant reports in CSV, Excel, or PDF format."""
    slug_clean = slug.strip().lower()
    rows = []
    title = f"{slug_clean.title()} Report"

    if slug_clean == "calls":
        stmt = select(Call).where(Call.business_id == tenant_id).order_by(Call.initiated_at.desc()).limit(1000)
        res = await db.execute(stmt)
        calls = res.scalars().all()
        rows = [
            {
                "Call ID": c.id,
                "From": c.from_number,
                "To": c.to_number,
                "Direction": c.direction,
                "Status": c.status,
                "Duration (s)": c.duration_seconds,
                "Client Name": getattr(c, "client_name", ""),
                "Initiated At": c.initiated_at.isoformat() if c.initiated_at else "",
            }
            for c in calls
        ]
    elif slug_clean == "leads":
        stmt = select(Lead).where(Lead.business_id == tenant_id).order_by(Lead.created_at.desc()).limit(1000)
        res = await db.execute(stmt)
        leads = res.scalars().all()
        rows = [
            {
                "Lead ID": l.id,
                "Name": l.name,
                "Phone": l.phone,
                "Email": l.email,
                "Status": l.status,
                "Created At": l.created_at.isoformat() if l.created_at else "",
            }
            for l in leads
        ]
    elif slug_clean == "campaigns":
        stmt = select(Campaign).where(Campaign.business_id == tenant_id).order_by(Campaign.created_at.desc())
        res = await db.execute(stmt)
        campaigns = res.scalars().all()
        rows = [
            {
                "Campaign ID": c.id,
                "Name": c.name,
                "Status": c.status,
                "Agent Type": c.agent_type,
                "Created At": c.created_at.isoformat() if c.created_at else "",
            }
            for c in campaigns
        ]
    elif slug_clean in ("agents", "voice-agents"):
        stmt = select(AIAgent).where(AIAgent.business_id == tenant_id).order_by(AIAgent.name.asc())
        res = await db.execute(stmt)
        agents = res.scalars().all()
        rows = [
            {
                "Agent ID": a.id,
                "Name": a.name,
                "Type": a.type,
                "Status": a.status,
                "Voice Provider": a.voice_provider,
                "Voice ID": a.voice_id,
            }
            for a in agents
        ]
    elif slug_clean in ("users", "user-stats"):
        stmt = select(User).where(User.business_id == tenant_id).order_by(User.created_at.desc())
        res = await db.execute(stmt)
        users = res.scalars().all()
        rows = [
            {
                "User ID": u.id,
                "Email": u.email,
                "Full Name": u.full_name,
                "Role": u.role,
                "Active": u.is_active,
                "Created At": u.created_at.isoformat() if u.created_at else "",
            }
            for u in users
        ]
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report dataset '{slug}' not found. Supported: calls, leads, campaigns, agents, user-stats",
        )

    if not rows:
        rows = [{"Notice": "No data found for this tenant report"}]

    fmt = format.lower()
    if fmt == "csv":
        data = export_to_csv(rows)
        media_type = "text/csv"
        filename = f"{slug_clean}_report.csv"
    elif fmt in ("excel", "xlsx"):
        data = export_to_excel(rows, sheet_name=slug_clean)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"{slug_clean}_report.xlsx"
    elif fmt == "pdf":
        data = export_to_pdf(title, rows)
        media_type = "application/pdf"
        filename = f"{slug_clean}_report.pdf"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported format")

    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
