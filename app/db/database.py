from collections.abc import AsyncGenerator
import datetime
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from urllib.parse import urlparse, urlunparse
from app.core.config import get_settings

settings = get_settings()

db_url = settings.database_url
if db_url:
    parsed = urlparse(db_url)
    if parsed.scheme in ("postgres", "postgresql"):
        db_url = urlunparse(parsed._replace(scheme="postgresql+asyncpg"))

connect_args = {}
if db_url and "sqlite" in db_url:
    connect_args = {"timeout": 30}

engine = create_async_engine(db_url, echo=False, future=True, poolclass=NullPool, connect_args=connect_args)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
async_session_factory = AsyncSessionLocal

if db_url and "sqlite" in db_url:
    from sqlalchemy import event
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=30000;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.close()
        except Exception:
            pass



class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


from loguru import logger

async def init_db() -> None:
    import app.db.models  # Ensure models are registered with Base.metadata
    import app.models.file  # Ensure TenantFile is registered
    import app.models.plan  # Ensure Plan and Subscription are registered
    import app.db.call_history_models  # Call History v2 additive tables
    if db_url and "sqlite" in db_url:
        async with engine.begin() as conn:
            try:
                await conn.execute(text("PRAGMA journal_mode=WAL;"))
                await conn.execute(text("PRAGMA busy_timeout=30000;"))
                await conn.execute(text("PRAGMA synchronous=NORMAL;"))
            except Exception as e:
                logger.warning(f"Could not set SQLite PRAGMA: {e}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    migrations = [
        ("knowledge_documents", "rag_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "carrier_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "trunk_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "caller_did_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "sip_account_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "voice_config", "JSON", "'{}'"),
        ("did_inventory", "sip_account_id", "VARCHAR(36)", "NULL"),
        ("calls", "sip_account_id", "VARCHAR(36)", "NULL"),
        ("knowledge_documents", "agent_id", "VARCHAR(36)", "NULL"),
        ("knowledge_documents", "version_id", "VARCHAR(50)", "'v1'"),
        ("knowledge_documents", "is_active", "BOOLEAN", "TRUE"),
        ("knowledge_chunks", "rag_id", "VARCHAR(36)", "NULL"),
        ("knowledge_chunks", "agent_id", "VARCHAR(36)", "NULL"),
        ("knowledge_chunks", "version_id", "VARCHAR(50)", "'v1'"),
        ("knowledge_chunks", "is_active", "BOOLEAN", "TRUE"),
        ("knowledge_library", "business_id", "VARCHAR(36)", "NULL"),
        ("knowledge_library", "version_id", "VARCHAR(50)", "'v1'"),
        ("knowledge_library", "is_active", "BOOLEAN", "TRUE"),
        ("leads", "business_id", "VARCHAR(36)", "NULL"),
        ("leads", "created_by_role", "VARCHAR(20)", "'USER'"),
        ("leads", "group_id", "VARCHAR(36)", "NULL"),
        ("leads", "assigned_expert_id", "VARCHAR(36)", "NULL"),
        ("leads", "attempt_count", "INTEGER", "0"),
        ("leads", "last_attempt_at", "TIMESTAMP", "NULL"),
        ("leads", "next_retry_at", "TIMESTAMP", "NULL"),
        ("leads", "call_status", "VARCHAR(50)", "''"),
        ("leads", "call_date_time", "TIMESTAMP", "NULL"),
        ("leads", "call_duration", "INTEGER", "0"),
        ("leads", "twilio_call_sid", "VARCHAR(64)", "''"),
        ("leads", "call_result", "VARCHAR(255)", "''"),
        ("lead_groups", "business_id", "VARCHAR(36)", "NULL"),
        ("lead_groups", "user_id", "VARCHAR(36)", "NULL"),
        ("lead_groups", "status", "VARCHAR(50)", "'COMPLETED'"),
        ("lead_groups", "lead_count", "INTEGER", "0"),
        ("businesses", "wallet_balance", "DOUBLE PRECISION", "0.0"),
        ("businesses", "current_plan", "VARCHAR(50)", "'Free Plan'"),
        ("businesses", "permissions", "JSON", "'{}'"),
        ("businesses", "subdomain", "VARCHAR(100)", "NULL"),
        ("businesses", "custom_domain", "VARCHAR(255)", "NULL"),
        ("businesses", "cluster_id", "VARCHAR(50)", "'default'"),
        ("businesses", "server_url", "VARCHAR(255)", "NULL"),
        ("businesses", "region", "VARCHAR(50)", "'us-east'"),
        ("businesses", "isolation_strategy", "VARCHAR(50)", "'SHARED_DATABASE'"),
        ("businesses", "database_url", "VARCHAR(500)", "NULL"),
        ("businesses", "schema_name", "VARCHAR(100)", "NULL"),
        ("businesses", "status", "VARCHAR(50)", "'active'"),
        ("businesses", "registration_source", "VARCHAR(50)", "'UNKNOWN'"),
        ("users", "first_name", "VARCHAR(100)", "''"),
        ("users", "last_name", "VARCHAR(100)", "''"),
        ("users", "phone", "VARCHAR(50)", "NULL"),
        ("users", "country", "VARCHAR(100)", "NULL"),
        ("users", "email_verified", "BOOLEAN", "FALSE"),
        ("users", "is_active", "BOOLEAN", "TRUE"),
        ("users", "permissions", "JSON", "'{}'"),
        ("users", "plain_password", "VARCHAR(255)", "''"),
        ("users", "status", "VARCHAR(30)", "'AVAILABLE'"),
        ("knowledge_library", "embedding", "TEXT", "NULL"),
        ("mgmt_agents", "user_id", "VARCHAR(36)", "NULL"),
        ("mgmt_agents", "login_user_id", "VARCHAR(36)", "NULL"),
        ("agents", "business_id", "VARCHAR(36)", "NULL"),
        ("lead_assignments", "business_id", "VARCHAR(36)", "NULL"),
        ("lead_assignments", "rag_id", "VARCHAR(36)", "NULL"),
        ("lead_assignments", "status", "VARCHAR(20)", "'ACTIVE'"),
        ("lead_assignments", "trigger_source", "VARCHAR(50)", "'TALK_TO_AGENT'"),
        ("lead_assignments", "ai_agent_name", "VARCHAR(255)", "''"),
        ("lead_assignments", "qualification_stage", "VARCHAR(100)", "''"),
        ("lead_assignments", "transcript", "TEXT", "''"),
        ("lead_assignments", "notes", "TEXT", "''"),
        ("lead_assignments", "context", "JSON", "'{}'"),
        ("lead_assignments", "previous_expert_id", "VARCHAR(36)", "NULL"),
        ("agent_assignment_queue", "requested_expert_id", "VARCHAR(36)", "NULL"),
        ("agent_assignment_queue", "assigned_agent_id", "VARCHAR(36)", "NULL"),
        ("agent_assignment_queue", "call_started_at", "TIMESTAMP", "NULL"),
        ("agent_assignment_queue", "call_completed_at", "TIMESTAMP", "NULL"),
        ("agent_assignment_queue", "call_duration", "INTEGER", "NULL"),
        ("agent_assignment_queue", "call_session_id", "VARCHAR(64)", "NULL"),
        ("campaigns", "client_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "voice_agent_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "lead_group_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "agent_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "user_id", "VARCHAR(36)", "NULL"),
        ("campaigns", "agent_type", "VARCHAR(50)", "'AI_VOICE'"),
        ("campaigns", "client_name", "VARCHAR(255)", "''"),
        ("campaigns", "client_phone", "VARCHAR(50)", "''"),
        ("campaigns", "client_company", "VARCHAR(255)", "''"),
        ("campaigns", "voice_agent_name", "VARCHAR(255)", "''"),
        ("campaigns", "lead_group_name", "VARCHAR(255)", "''"),
        ("campaigns", "twilio_number", "VARCHAR(50)", "''"),
        ("campaigns", "auto_dial_enabled", "BOOLEAN", "FALSE"),
        ("campaigns", "auto_dial_ratio", "INTEGER", "1"),
        ("campaigns", "started_at", "TIMESTAMP", "NULL"),
        ("campaigns", "completed_at", "TIMESTAMP", "NULL"),
        ("campaign_leads", "assigned_expert_id", "VARCHAR(36)", "NULL"),
        ("campaign_leads", "status", "VARCHAR(20)", "'PENDING'"),
        ("campaign_leads", "call_id", "VARCHAR(36)", "NULL"),
        ("campaign_leads", "attempt_count", "INTEGER", "0"),
        ("campaign_leads", "last_attempt_at", "TIMESTAMP", "NULL"),
        ("campaign_leads", "next_retry_at", "TIMESTAMP", "NULL"),
        ("calls", "business_id", "VARCHAR(36)", "NULL"),
        ("calls", "lead_id", "VARCHAR(36)", "NULL"),
        ("calls", "agent_id", "VARCHAR(36)", "NULL"),
        ("calls", "direction", "VARCHAR(20)", "'OUTBOUND'"),
        ("calls", "status", "VARCHAR(30)", "'COMPLETED'"),
        ("calls", "duration_seconds", "INTEGER", "0"),
        ("calls", "from_number", "VARCHAR(50)", "''"),
        ("calls", "to_number", "VARCHAR(50)", "''"),
        ("calls", "call_sid", "VARCHAR(64)", "NULL"),
        ("calls", "agent_type", "VARCHAR(50)", "'AI_VOICE'"),
        ("calls", "client_id", "VARCHAR(36)", "NULL"),
        ("calls", "campaign_id", "VARCHAR(36)", "NULL"),
        ("calls", "client_name", "VARCHAR(255)", "''"),
        ("calls", "client_phone", "VARCHAR(50)", "''"),
        ("calls", "conference_sid", "VARCHAR(64)", "NULL"),
        ("calls", "barge_call_sid", "VARCHAR(64)", "NULL"),
        ("calls", "call_result", "VARCHAR(255)", "''"),
        ("calls", "initiated_at", "TIMESTAMP", "NULL"),
        ("calls", "connected_at", "TIMESTAMP", "NULL"),
        ("calls", "ended_at", "TIMESTAMP", "NULL"),
        ("calls", "client_connected_at", "TIMESTAMP", "NULL"),
        ("calls", "client_disconnected_at", "TIMESTAMP", "NULL"),
        ("calls", "client_duration_seconds", "INTEGER", "0"),
        ("calls", "recording_url", "VARCHAR(500)", "''"),
        ("calls", "transcript", "TEXT", "''"),
        ("calls", "turn_count", "INTEGER", "0"),
        ("call_participants", "phone_number", "VARCHAR(50)", "''"),
        ("call_participants", "status", "VARCHAR(20)", "'JOINED'"),
        ("call_participants", "duration_seconds", "INTEGER", "0"),
        ("call_participants", "joined_at", "TIMESTAMP", "NULL"),
        ("call_participants", "left_at", "TIMESTAMP", "NULL"),
        ("call_participants", "barge_call_sid", "VARCHAR(64)", "NULL"),
        ("call_transfers", "connected_at", "TIMESTAMP", "NULL"),
        ("call_transfers", "disconnected_at", "TIMESTAMP", "NULL"),
        ("call_transfers", "campaign_id", "VARCHAR(36)", "NULL"),
        ("call_transfers", "client_id", "VARCHAR(36)", "NULL"),
        ("call_transfers", "duration_seconds", "INTEGER", "0"),
        ("clients", "group_id", "VARCHAR(36)", "NULL"),
        ("clients", "original_filename", "VARCHAR(255)", "NULL"),
        ("clients", "assigned_agent_id", "VARCHAR(36)", "NULL"),
        ("parked_calls", "slot", "INTEGER", "1"),
        ("parked_calls", "hold_reason", "VARCHAR(255)", "''"),
        ("parked_calls", "status", "VARCHAR(20)", "'ON_HOLD'"),
        ("parked_calls", "hold_start_time", "TIMESTAMP", "NULL"),
        ("parked_calls", "hold_end_time", "TIMESTAMP", "NULL"),
        ("parked_conferences", "D1_phone", "VARCHAR(50)", "''"),
        ("parked_conferences", "D2_phone", "VARCHAR(50)", "''"),
        ("parked_conferences", "D3_phone", "VARCHAR(50)", "''"),
        ("client_configs", "slot", "INTEGER", "1"),
        ("client_configs", "client_id", "VARCHAR(36)", "NULL"),
        ("client_configs", "client_name", "VARCHAR(255)", "''"),
        ("client_configs", "client_phone", "VARCHAR(50)", "''"),
        ("audit_logs", "details", "JSON", "'{}'"),
        ("experts", "auto_dial_enabled", "BOOLEAN", "FALSE"),
        ("experts", "user_id", "VARCHAR(36)", "NULL"),
        ("experts", "business_id", "VARCHAR(36)", "NULL"),
        ("experts", "skills", "JSON", "'[]'"),
        ("experts", "status", "VARCHAR(20)", "'AVAILABLE'"),
        ("experts", "active_lead_count", "INTEGER", "0"),
        ("users", "business_id", "VARCHAR(36)", "NULL"),
        ("businesses", "slug", "VARCHAR(100)", "NULL"),
        ("mgmt_agents", "business_id", "VARCHAR(36)", "NULL"),
        ("rag_agents", "business_id", "VARCHAR(36)", "NULL"),
        ("calls", "user_id", "VARCHAR(36)", "NULL"),
        ("calls", "sip_extension", "VARCHAR(50)", "NULL"),
        ("calls", "hangup_reason", "VARCHAR(100)", "NULL"),
        ("users", "sip_extension", "VARCHAR(50)", "NULL"),
        ("notifications", "reference_id", "VARCHAR(255)", "NULL"),
        ("call_recordings", "tenant_id", "VARCHAR(36)", "NULL"),
        ("call_recordings", "campaign_id", "VARCHAR(36)", "NULL"),
        ("call_recordings", "call_direction", "VARCHAR(20)", "'OUTBOUND'"),
        ("call_recordings", "expires_at", "TIMESTAMP", "NULL"),
        ("call_recordings", "recording_status", "VARCHAR(30)", "'available'"),
        ("call_recordings", "backup_status", "VARCHAR(30)", "'NOT_BACKED_UP'"),
        ("call_recordings", "google_drive_file_id", "VARCHAR(255)", "NULL"),
        ("call_recordings", "google_drive_folder_id", "VARCHAR(255)", "NULL"),
        ("call_recordings", "google_drive_file_url", "VARCHAR(500)", "NULL"),
        ("call_recordings", "backup_completed_at", "TIMESTAMP", "NULL"),
        ("call_recordings", "backup_error", "VARCHAR(500)", "NULL"),
        ("call_recordings", "cleanup_status", "VARCHAR(30)", "'pending'"),
        ("call_recordings", "updated_at", "TIMESTAMP", "NULL"),
        ("ai_agents", "voice_config_id", "VARCHAR(36)", "NULL"),
        ("ai_agents", "voice_name", "VARCHAR(255)", "NULL"),
    ]
    is_sqlite = engine.name == "sqlite"
    if is_sqlite:
        async with engine.begin() as conn:
            # Inspect existing columns per table to avoid duplicate column name errors
            tables = {m[0] for m in migrations}
            table_cols: dict[str, set[str]] = {}
            for tbl in tables:
                try:
                    res = await conn.execute(text(f"PRAGMA table_info({tbl})"))
                    table_cols[tbl] = {row[1] for row in res.fetchall()}
                except Exception:
                    table_cols[tbl] = set()

            for table, column, col_type, default in migrations:
                if column in table_cols.get(table, set()):
                    continue

                target_default = default
                if col_type == "BOOLEAN":
                    if default in ("0", "'0'", "FALSE", "false"):
                        target_default = "0"
                    elif default in ("1", "'1'", "TRUE", "true"):
                        target_default = "1"

                sqlite_type = "TEXT" if col_type in ("JSON", "VARCHAR(255)", "VARCHAR(50)", "VARCHAR(100)", "VARCHAR(36)", "VARCHAR(20)", "VARCHAR(30)", "VARCHAR(64)", "VARCHAR(500)") else ("DATETIME" if col_type == "TIMESTAMP" else col_type)
                try:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type} DEFAULT {target_default}"))
                    table_cols.setdefault(table, set()).add(column)
                except Exception as mig_err:
                    if "duplicate column name" not in str(mig_err).lower():
                        logger.debug(f"Migration note ({table}.{column}): {mig_err}")
    else:
        for table, column, col_type, default in migrations:
            try:
                target_default = default
                if col_type == "BOOLEAN":
                    if default in ("0", "'0'", "FALSE", "false"):
                        target_default = "FALSE"
                    elif default in ("1", "'1'", "TRUE", "true"):
                        target_default = "TRUE"

                async with engine.begin() as conn:
                    sql_cmd = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type} DEFAULT {target_default}"
                    await conn.execute(text(sql_cmd))
            except Exception as mig_err:
                logger.debug(f"Migration note ({table}.{column}): {mig_err}")

    if is_sqlite:
        try:
            async with engine.begin() as conn:
                res = await conn.execute(text("PRAGMA table_info(chat_conversations)"))
                cols = res.fetchall()
                # col format: (cid, name, type, notnull, dflt_value, pk)
                user_id_col = next((c for c in cols if c[1] == "user_id"), None)
                if user_id_col and user_id_col[3] == 1:
                    await conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS chat_conversations_new (
                            id VARCHAR(36) PRIMARY KEY,
                            business_id VARCHAR(36),
                            user_id VARCHAR(36),
                            title VARCHAR(255) DEFAULT 'New Conversation',
                            is_global BOOLEAN DEFAULT 0,
                            pdf_path VARCHAR(500),
                            created_at DATETIME,
                            updated_at DATETIME
                        );
                    """))
                    try:
                        await conn.execute(text("""
                            INSERT INTO chat_conversations_new (id, business_id, user_id, title, is_global, pdf_path, created_at, updated_at)
                            SELECT id, business_id, user_id, title, is_global, pdf_path, created_at, updated_at FROM chat_conversations;
                        """))
                    except Exception:
                        pass
                    await conn.execute(text("DROP TABLE IF EXISTS chat_conversations;"))
                    await conn.execute(text("ALTER TABLE chat_conversations_new RENAME TO chat_conversations;"))
        except Exception as e:
            logger.warning(f"SQLite chat_conversations migration note: {e}")
    else:
        for stmt in [
            "ALTER TABLE lead_assignments ALTER COLUMN strategy TYPE VARCHAR(50)",
            "ALTER TABLE lead_assignments ALTER COLUMN trigger_source TYPE VARCHAR(50)",
            "ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_agent_id_fkey",
            "ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_business_id_fkey",
            "ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_campaign_id_fkey",
            "ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_lead_id_fkey",
            "ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_client_id_fkey",
            "ALTER TABLE call_participants DROP CONSTRAINT IF EXISTS call_participants_participant_id_fkey",
            "ALTER TABLE call_participants DROP CONSTRAINT IF EXISTS call_participants_call_id_fkey",
            "ALTER TABLE call_transfers DROP CONSTRAINT IF EXISTS call_transfers_business_id_fkey",
            "ALTER TABLE call_transfers DROP CONSTRAINT IF EXISTS call_transfers_from_agent_id_fkey",
            "ALTER TABLE call_transfers DROP CONSTRAINT IF EXISTS call_transfers_to_agent_id_fkey",
            "ALTER TABLE call_transfers DROP CONSTRAINT IF EXISTS call_transfers_call_id_fkey",
            "ALTER TABLE call_transfers DROP CONSTRAINT IF EXISTS call_transfers_lead_id_fkey",
            "ALTER TABLE call_transfers DROP CONSTRAINT IF EXISTS call_transfers_client_id_fkey",
            "ALTER TABLE call_transfers DROP CONSTRAINT IF EXISTS call_transfers_campaign_id_fkey",
            "ALTER TABLE inbound_numbers DROP CONSTRAINT IF EXISTS inbound_numbers_business_id_fkey",
            "ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_business_id_fkey",
            "ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_group_id_fkey",
            "ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_assigned_expert_id_fkey",
            "ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_agent_id_fkey",
            "ALTER TABLE lead_groups DROP CONSTRAINT IF EXISTS lead_groups_business_id_fkey",
            "ALTER TABLE lead_assignments DROP CONSTRAINT IF EXISTS lead_assignments_business_id_fkey",
            "ALTER TABLE lead_assignments DROP CONSTRAINT IF EXISTS lead_assignments_lead_id_fkey",
            "ALTER TABLE lead_assignments DROP CONSTRAINT IF EXISTS lead_assignments_expert_id_fkey",
            "ALTER TABLE agent_assignment_queue DROP CONSTRAINT IF EXISTS agent_assignment_queue_business_id_fkey",
            "ALTER TABLE agent_assignment_queue DROP CONSTRAINT IF EXISTS agent_assignment_queue_lead_id_fkey",
            "ALTER TABLE agent_assignment_queue DROP CONSTRAINT IF EXISTS agent_assignment_queue_expert_id_fkey",
            "ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_agent_id_fkey",
            "ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_business_id_fkey",
            "ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_lead_id_fkey",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS business_id VARCHAR(36) DEFAULT NULL",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS group_id VARCHAR(36) DEFAULT NULL",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS created_by_role VARCHAR(20) DEFAULT 'USER'",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS assigned_expert_id VARCHAR(36) DEFAULT NULL",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS attempt_count INTEGER DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP DEFAULT NULL",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS call_status VARCHAR(50) DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS call_date_time TIMESTAMP DEFAULT NULL",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS call_duration INTEGER DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS twilio_call_sid VARCHAR(64) DEFAULT ''",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS call_result VARCHAR(255) DEFAULT ''",
            "ALTER TABLE lead_groups ADD COLUMN IF NOT EXISTS business_id VARCHAR(36) DEFAULT NULL",
            "ALTER TABLE lead_groups ADD COLUMN IF NOT EXISTS user_id VARCHAR(36) DEFAULT NULL",
            "ALTER TABLE lead_groups ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'COMPLETED'",
            "ALTER TABLE lead_groups ADD COLUMN IF NOT EXISTS lead_count INTEGER DEFAULT 0",
            "ALTER TABLE experts ADD COLUMN IF NOT EXISTS auto_dial_enabled BOOLEAN DEFAULT FALSE",
            "ALTER TABLE experts ADD COLUMN IF NOT EXISTS user_id VARCHAR(36) DEFAULT NULL",
            "ALTER TABLE experts ADD COLUMN IF NOT EXISTS business_id VARCHAR(36) DEFAULT NULL",
            "ALTER TABLE experts ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'AVAILABLE'",
            "ALTER TABLE experts ADD COLUMN IF NOT EXISTS active_lead_count INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS business_id VARCHAR(36) DEFAULT NULL",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS subdomain VARCHAR(100) DEFAULT NULL",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS custom_domain VARCHAR(255) DEFAULT NULL",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS cluster_id VARCHAR(50) DEFAULT 'default'",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS server_url VARCHAR(255) DEFAULT NULL",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS region VARCHAR(50) DEFAULT 'us-east'",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS isolation_strategy VARCHAR(50) DEFAULT 'SHARED_DATABASE'",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS database_url VARCHAR(500) DEFAULT NULL",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS schema_name VARCHAR(100) DEFAULT NULL",
            "ALTER TABLE businesses ADD COLUMN IF NOT EXISTS permissions JSON DEFAULT '{}'",
            "UPDATE experts SET auto_dial_enabled = FALSE WHERE auto_dial_enabled IS NULL",
        ]:
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(stmt))
            except Exception as e:
                logger.debug(f"Postgres migration statement note ({stmt}): {e}")

    # Ensure default primary business exists and backfill null business_ids
    try:
        PRIMARY_BUSINESS_ID = "c3dce06c-8330-4724-ba33-b5282b3c3596"
        async with engine.begin() as conn:
            res = await conn.execute(text("SELECT id FROM businesses WHERE id = :bid"), {"bid": PRIMARY_BUSINESS_ID})
            if not res.scalar():
                now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                await conn.execute(
                    text("""
                        INSERT INTO businesses (id, name, slug, industry, owner_user_id, twilio_number, is_active, status, registration_source, current_plan, wallet_balance, cluster_id, region, isolation_strategy, billing_company_name, billing_email, billing_address, billing_tax_id, permissions, created_at, updated_at)
                        VALUES (:bid, 'CallZenza Primary Business', 'callzenza', 'Telecom', 'system', '', :is_active, 'active', 'SUPER_ADMIN_CREATED', 'Enterprise', 1000.0, 'default', 'us-east', 'SHARED_DATABASE', 'CallZenza Primary Business', '', '', '', '{}', :now, :now)
                    """),
                    {"bid": PRIMARY_BUSINESS_ID, "is_active": True, "now": now}
                )
                logger.info(f"Initialized default primary business {PRIMARY_BUSINESS_ID}")

            # Backfill any orphaned rows in core multi-tenant tables
            for tbl in ["users", "mgmt_agents", "rag_agents", "campaigns", "leads", "calls", "experts", "agents", "lead_assignments", "lead_groups"]:
                try:
                    await conn.execute(
                        text(f"UPDATE {tbl} SET business_id = :bid WHERE business_id IS NULL OR business_id = ''"),
                        {"bid": PRIMARY_BUSINESS_ID}
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Tenant backfill / initialization warning: {e}")

    # Ensure admin@callzenza.com exists if INITIAL_ADMIN_PASSWORD is set in environment
    try:
        from app.core.security import hash_password
        import os

        admin_pw = os.getenv("INITIAL_ADMIN_PASSWORD")
        if admin_pw and admin_pw.strip():
            async with engine.begin() as conn:
                res = await conn.execute(text("SELECT id FROM users WHERE email = 'admin@callzenza.com'"))
                existing = res.scalar()
                if not existing:
                    admin_id = str(uuid.uuid4())
                    hashed = hash_password(admin_pw.strip())
                    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                    await conn.execute(
                        text("""
                            INSERT INTO users (id, email, hashed_password, full_name, first_name, last_name, role, is_active, created_at)
                            VALUES (:id, 'admin@callzenza.com', :hashed, 'System Admin', 'System', 'Admin', 'SUPER_ADMIN', TRUE, :now)
                        """),
                        {"id": admin_id, "hashed": hashed, "now": now}
                    )
                    logger.info("Created initial admin user for admin@callzenza.com from INITIAL_ADMIN_PASSWORD env")
                else:
                    await conn.execute(text("UPDATE users SET role = 'SUPER_ADMIN' WHERE email = 'admin@callzenza.com'"))
    except Exception as e:
        logger.warning(f"Could not auto-promote or create admin@callzenza.com: {e}")

    try:
        from app.db.multitenant_migration import run_multitenant_migration
        await run_multitenant_migration(engine)
    except Exception as e:
        logger.warning(f"Could not execute multi-tenant migration: {e}")
