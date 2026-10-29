import csv
import io
from typing import Any, Dict, List, Optional
import pandas as pd

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


def export_to_csv(rows: List[Dict[str, Any]], fieldnames: Optional[List[str]] = None) -> bytes:
    """Generate CSV bytes from a list of dictionary records."""
    if not rows:
        return b""

    if not fieldnames:
        fieldnames = list(rows[0].keys())

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: str(v) if v is not None else "" for k, v in row.items()})

    return output.getvalue().encode("utf-8")


def export_to_excel(rows: List[Dict[str, Any]], sheet_name: str = "Report") -> bytes:
    """Generate Excel .xlsx bytes from a list of dictionary records."""
    df = pd.DataFrame(rows if rows else [{}])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    return output.getvalue()


def export_to_pdf(title: str, rows: List[Dict[str, Any]], headers: Optional[List[str]] = None) -> bytes:
    """Generate PDF bytes from report records using ReportLab."""
    output = io.BytesIO()

    if not REPORTLAB_AVAILABLE or not rows:
        # Fallback to simple text formatted output if reportlab is unavailable
        text = f"{title}\n{'=' * len(title)}\n\n"
        for r in rows:
            text += ", ".join(f"{k}: {v}" for k, v in r.items()) + "\n"
        return text.encode("utf-8")

    doc = SimpleDocTemplate(output, pagesize=letter, leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    elements = []

    # Title
    elements.append(Paragraph(f"<b>{title}</b>", styles["Title"]))
    elements.append(Spacer(1, 15))

    keys = headers if headers else list(rows[0].keys())[:7]  # Limit to first 7 cols for page width
    table_data = [keys]

    for row in rows[:200]:  # Limit table rows to prevent massive single document PDF
        row_vals = [str(row.get(k, ""))[:35] for k in keys]
        table_data.append(row_vals)

    table = Table(table_data)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4F46E5")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 9),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F9FAFB")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
                ("FONTSIZE", (0, 1), (-1, -1), 8),
            ]
        )
    )
    elements.append(table)
    doc.build(elements)
    return output.getvalue()
