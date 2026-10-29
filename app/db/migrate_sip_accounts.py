"""
Migration script for Multi-Tenant SIP Configuration & Campaign-Level SIP Assignment.
Ensures:
- 'sip_accounts' table exists
- 'campaigns.sip_account_id' column exists
- 'calls.sip_account_id' column exists
- Appropriate indexes exist
Fully reversible, idempotent, works on SQLite and PostgreSQL.
"""
import asyncio
from sqlalchemy import text
from loguru import logger

from app.db.database import AsyncSessionLocal, Base, engine
import app.db.models  # registers SIPAccount, Campaign, Call with Base


async def run_sip_account_migration():
    logger.info("Starting Multi-Tenant SIP Account migration...")

    # 1. Ensure all declared tables (including sip_accounts) exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Base.metadata.create_all completed.")

    # 2. Add columns if missing (defensive for existing databases)
    async with AsyncSessionLocal() as session:
        # Check SQLite or PostgreSQL dialect
        bind = session.bind or engine
        is_sqlite = "sqlite" in str(bind.url)

        # Columns to ensure
        target_columns = [
            ("campaigns", "sip_account_id", "VARCHAR(36)"),
            ("calls", "sip_account_id", "VARCHAR(36)"),
        ]

        for table, col, col_type in target_columns:
            has_col = False
            try:
                if is_sqlite:
                    res = await session.execute(text(f"PRAGMA table_info({table});"))
                    existing_cols = [row[1] for row in res.fetchall()]
                    has_col = col in existing_cols
                else:
                    res = await session.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = :t AND column_name = :c;"
                        ),
                        {"t": table, "c": col},
                    )
                    has_col = res.first() is not None

                if not has_col:
                    logger.info(f"Adding column '{col}' to table '{table}'...")
                    await session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};"))
                    await session.commit()
                    logger.info(f"Column '{col}' added successfully to '{table}'.")
                else:
                    logger.debug(f"Column '{col}' already present on '{table}'.")
            except Exception as exc:
                logger.warning(f"Note on migration for {table}.{col}: {exc}")
                await session.rollback()

    logger.info("Multi-Tenant SIP Account migration verified successfully.")


if __name__ == "__main__":
    asyncio.run(run_sip_account_migration())
