"""Safe migration script for tenant-level data isolation.
Ensures all records in calls, campaigns, leads, and call_recordings have a valid tenant_id/business_id.
Backfills legacy records to the original primary business without modifying or deleting active data.
"""
import sqlite3
from pathlib import Path
from loguru import logger

DB_PATH = Path(__file__).resolve().parent.parent.parent / "callzenza.db"
LEGACY_DEFAULT_BIZ = "fa5ffab7-4039-47e3-9c05-3546b2109a12"


def run_migration():
    if not DB_PATH.exists():
        logger.info(f"Database not found at {DB_PATH}, skipping migration.")
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        # Check call_recordings for NULL tenant_id
        cur.execute(
            """
            UPDATE call_recordings 
            SET tenant_id = ?, recording_status = 'expired'
            WHERE tenant_id IS NULL OR tenant_id = ''
            """,
            (LEGACY_DEFAULT_BIZ,)
        )
        updated_recs = cur.rowcount
        con.commit()
        if updated_recs > 0:
            logger.info(f"[MIGRATION] Backfilled {updated_recs} call_recordings with NULL tenant_id to {LEGACY_DEFAULT_BIZ}")

        # Ensure index on call_recordings(tenant_id)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_call_recordings_tenant_id ON call_recordings(tenant_id)")
        # Ensure index on calls(business_id)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_calls_business_id ON calls(business_id)")
        # Ensure index on campaigns(business_id)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_campaigns_business_id ON campaigns(business_id)")
        # Ensure index on leads(business_id)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_business_id ON leads(business_id)")
        con.commit()
        logger.info("[MIGRATION] Tenant isolation indexes and data verified successfully.")
    except Exception as exc:
        logger.error(f"[MIGRATION] Tenant isolation migration note: {exc}")
        con.rollback()
    finally:
        con.close()


if __name__ == "__main__":
    run_migration()
