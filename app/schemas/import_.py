from pydantic import BaseModel


class ImportSummary(BaseModel):
    total_rows: int = 0
    successfully_imported: int = 0
    duplicate_rows: int = 0
    invalid_rows: int = 0
    final_lead_count: int = 0
    leads_created: int = 0
    calls_queued: int = 0
    skipped_rows: int = 0
    lead_group_id: str | None = None
    errors: list[str] = []
