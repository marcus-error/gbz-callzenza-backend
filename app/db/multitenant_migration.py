import datetime
import uuid
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.deps import PRIMARY_BUSINESS_ID
from app.db.database import Base
# Import all new multi-tenant models so Base.metadata knows about them
import app.models.branding
import app.models.file
import app.models.notification
import app.models.organization
import app.models.plan
import app.models.role
import app.models.tenant
import app.models.tenant_settings
from app.models.permission import STANDARD_PERMISSIONS


async def run_multitenant_migration(engine: AsyncEngine) -> None:
    """Executes safe, additive multi-tenant database migration and default data seeding."""
    logger.info("[MULTI-TENANT] Running additive multi-tenant schema verification and seeding...")

    is_sqlite = engine.name == "sqlite"

    # 1. Create any missing new tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Safely add nullable tenant_id column to existing tenant-scoped tables
    tables_to_tenant_scope = [
        "users",
        "campaigns",
        "leads",
        "calls",
        "lead_groups",
        "experts",
        "email_logs",
    ]

    for tbl in tables_to_tenant_scope:
        try:
            async with engine.begin() as conn:
                if is_sqlite:
                    # Check if column already exists
                    res = await conn.execute(text(f"PRAGMA table_info({tbl})"))
                    cols = [c[1] for c in res.fetchall()]
                    if "tenant_id" not in cols:
                        await conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN tenant_id TEXT NULL"))
                else:
                    await conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36) NULL"))
        except Exception as e:
            logger.debug(f"[MULTI-TENANT MIGRATION] Column add note ({tbl}.tenant_id): {e}")

    # 2b. Additive columns for Subdomain, Cluster Distribution & Data Isolation
    saas_arch_columns = [
        ("subdomain", "VARCHAR(100)", "NULL"),
        ("custom_domain", "VARCHAR(255)", "NULL"),
        ("cluster_id", "VARCHAR(50)", "'default'"),
        ("server_url", "VARCHAR(255)", "NULL"),
        ("region", "VARCHAR(50)", "'us-east'"),
        ("isolation_strategy", "VARCHAR(50)", "'SHARED_DATABASE'"),
        ("database_url", "VARCHAR(500)", "NULL"),
        ("schema_name", "VARCHAR(100)", "NULL"),
        ("registration_source", "VARCHAR(50)", "'UNKNOWN'"),
        ("status", "VARCHAR(50)", "'active'"),
    ]

    for target_tbl in ["tenants", "businesses"]:
        try:
            async with engine.begin() as conn:
                existing_cols = []
                if is_sqlite:
                    res = await conn.execute(text(f"PRAGMA table_info({target_tbl})"))
                    existing_cols = [c[1] for c in res.fetchall()]
                
                for col_name, col_type, default_val in saas_arch_columns:
                    if is_sqlite:
                        if col_name not in existing_cols:
                            sqlite_type = "TEXT" if "VARCHAR" in col_type else col_type
                            await conn.execute(text(f"ALTER TABLE {target_tbl} ADD COLUMN {col_name} {sqlite_type} DEFAULT {default_val}"))
                    else:
                        await conn.execute(text(f"ALTER TABLE {target_tbl} ADD COLUMN IF NOT EXISTS {col_name} {col_type} DEFAULT {default_val}"))
        except Exception as e:
            logger.debug(f"[MULTI-TENANT MIGRATION] SaaS arch columns add note ({target_tbl}): {e}")

    # 2c. Additive columns for Plans table
    plan_columns = [
        ("annual_price_cents", "INTEGER", "0"),
        ("max_storage_mb", "INTEGER", "5000"),
    ]
    try:
        async with engine.begin() as conn:
            existing_pcols = []
            if is_sqlite:
                res = await conn.execute(text("PRAGMA table_info(plans)"))
                existing_pcols = [c[1] for c in res.fetchall()]
            for col_name, col_type, default_val in plan_columns:
                if is_sqlite:
                    if col_name not in existing_pcols:
                        await conn.execute(text(f"ALTER TABLE plans ADD COLUMN {col_name} {col_type} DEFAULT {default_val}"))
                else:
                    await conn.execute(text(f"ALTER TABLE plans ADD COLUMN IF NOT EXISTS {col_name} {col_type} DEFAULT {default_val}"))
    except Exception as e:
        logger.debug(f"[MULTI-TENANT MIGRATION] Plan columns add note: {e}")

    # 3. Seed Default Tenant
    try:
        async with engine.begin() as conn:
            res = await conn.execute(
                text("SELECT id FROM tenants WHERE id = :id OR slug = 'default'"),
                {"id": PRIMARY_BUSINESS_ID},
            )
            existing_tenant = res.scalar()
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

            if not existing_tenant:
                await conn.execute(
                    text("""
                        INSERT INTO tenants (id, name, slug, status, cluster_id, region, isolation_strategy, created_at, updated_at)
                        VALUES (:id, 'Default Organization', 'default', 'active', 'default', 'us-east', 'SHARED_DATABASE', :now, :now)
                    """),
                    {"id": PRIMARY_BUSINESS_ID, "now": now},
                )
                logger.info(f"[MULTI-TENANT] Seeded default tenant ({PRIMARY_BUSINESS_ID})")

            # Sync all businesses into tenants table so both tables stay in parity on both Postgres & SQLite
            try:
                if is_sqlite:
                    await conn.execute(text("""
                        INSERT OR IGNORE INTO tenants (id, name, slug, subdomain, status, cluster_id, region, isolation_strategy, created_at, updated_at)
                        SELECT id, name, slug, subdomain, status, cluster_id, region, isolation_strategy, created_at, updated_at
                        FROM businesses
                    """))
                else:
                    await conn.execute(text("""
                        INSERT INTO tenants (id, name, slug, subdomain, status, cluster_id, region, isolation_strategy, created_at, updated_at)
                        SELECT id, name, slug, subdomain, status, cluster_id, region, isolation_strategy, created_at, updated_at
                        FROM businesses
                        ON CONFLICT (id) DO NOTHING
                    """))
            except Exception as b_err:
                logger.debug(f"[MULTI-TENANT] Business to Tenant sync note: {b_err}")

            # Backfill any null tenant_id in existing tables
            for tbl in tables_to_tenant_scope:
                try:
                    await conn.execute(
                        text(f"UPDATE {tbl} SET tenant_id = :tid WHERE tenant_id IS NULL OR tenant_id = ''"),
                        {"tid": PRIMARY_BUSINESS_ID},
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[MULTI-TENANT MIGRATION] Default tenant check/seed warning: {e}")

    # 4. Seed Standard Permissions
    perm_id_map = {}
    try:
        async with engine.begin() as conn:
            for name, desc, mod in STANDARD_PERMISSIONS:
                res = await conn.execute(
                    text("SELECT id FROM permissions WHERE name = :name"),
                    {"name": name},
                )
                pid = res.scalar()
                now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                if not pid:
                    pid = str(uuid.uuid4())
                    await conn.execute(
                        text("""
                            INSERT INTO permissions (id, name, description, module, created_at)
                            VALUES (:id, :name, :desc, :mod, :now)
                        """),
                        {"id": pid, "name": name, "desc": desc, "mod": mod, "now": now},
                    )
                perm_id_map[name] = pid
    except Exception as e:
        logger.warning(f"[MULTI-TENANT MIGRATION] Standard permissions seed note: {e}")

    # 5. Seed Standard Roles
    standard_roles = [
        ("SUPER_ADMIN", "Full system and multi-tenant administrative access", True),
        ("ADMIN", "Full access to tenant resources, users, and billing", True),
        ("SUPERVISOR", "Manage campaigns, monitor calls, barge stream", True),
        ("VOICE_AGENT", "Voice agent user and transfer participant", True),
        ("SALES_MANAGER", "Oversees sales leads, campaigns, assignments", True),
        ("SALES_EXPERT", "Handles customer calls and lead updates", True),
        ("AGENT_MANAGER", "Manages AI and human agent configurations", True),
        ("VIEWER", "Read-only access to dashboards and reports", True),
    ]

    try:
        async with engine.begin() as conn:
            for rname, rdesc, is_sys in standard_roles:
                res = await conn.execute(
                    text("SELECT id FROM roles WHERE name = :name AND tenant_id IS NULL"),
                    {"name": rname},
                )
                rid = res.scalar()
                now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                if not rid:
                    rid = str(uuid.uuid4())
                    await conn.execute(
                        text("""
                            INSERT INTO roles (id, tenant_id, name, description, is_system, created_at, updated_at)
                            VALUES (:id, NULL, :name, :desc, :is_sys, :now, :now)
                        """),
                        {"id": rid, "name": rname, "desc": rdesc, "is_sys": 1 if is_sqlite else True, "now": now},
                    )

                # Link all permissions to SUPER_ADMIN and ADMIN
                if rname in ("SUPER_ADMIN", "ADMIN"):
                    for pid in perm_id_map.values():
                        try:
                            await conn.execute(
                                text("""
                                    INSERT INTO role_permissions (role_id, permission_id)
                                    VALUES (:rid, :pid)
                                """),
                                {"rid": rid, "pid": pid},
                            )
                        except Exception:
                            pass
    except Exception as e:
        logger.warning(f"[MULTI-TENANT MIGRATION] Standard roles seed note: {e}")

    # 6. Seed Default Plans
    plans_data = [
        # (name, slug, desc, price_cents, annual_price_cents, interval, max_ag, max_cc, max_ld, max_min, max_storage_mb, feats)
        ("Starter Free", "free", "Free tier with essential AI calling features", 0, 0, "monthly", 2, 1, 200, 60, 1000, {}),
        ("Pro Growth", "pro", "Scale outbound AI campaigns and multi-agent queues", 4900, 49000, "monthly", 10, 5, 5000, 600, 10000, {"barge": True, "transfers": True}),
        ("Enterprise", "enterprise", "Unlimited high-concurrency infrastructure", 19900, 199000, "monthly", 50, 20, 50000, 3000, 100000, {"barge": True, "transfers": True, "custom_voices": True}),
    ]

    try:
        async with engine.begin() as conn:
            for pname, pslug, pdesc, price, annual_price, interval, max_ag, max_cc, max_ld, max_min, max_storage, feats in plans_data:
                res = await conn.execute(
                    text("SELECT id FROM plans WHERE slug = :slug"),
                    {"slug": pslug},
                )
                if not res.scalar():
                    pid = str(uuid.uuid4())
                    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                    import json
                    feats_json = json.dumps(feats)
                    await conn.execute(
                        text("""
                            INSERT INTO plans (id, name, slug, description, price_cents, annual_price_cents, billing_interval,
                                               max_agents, max_concurrent_calls, max_leads, max_minutes_monthly, max_storage_mb,
                                               features, is_active, created_at)
                            VALUES (:id, :name, :slug, :desc, :price, :annual_price, :interval, :max_ag, :max_cc, :max_ld, :max_min, :max_storage,
                                    :feats, :is_act, :now)
                        """),
                        {
                            "id": pid,
                            "name": pname,
                            "slug": pslug,
                            "desc": pdesc,
                            "price": price,
                            "annual_price": annual_price,
                            "interval": interval,
                            "max_ag": max_ag,
                            "max_cc": max_cc,
                            "max_ld": max_ld,
                            "max_min": max_min,
                            "max_storage": max_storage,
                            "feats": feats_json,
                            "is_act": 1 if is_sqlite else True,
                            "now": now,
                        },
                    )
    except Exception as e:
        logger.warning(f"[MULTI-TENANT MIGRATION] Default plans seed note: {e}")


    # 7. Ensure platform admins, superadmins, and seeded system users have email_verified = True
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                    UPDATE users 
                    SET email_verified = :verified,
                        full_name = CASE 
                            WHEN email = 'admin@callzenza.com' OR full_name = 'Super Admin' THEN 'System Admin'
                            ELSE full_name
                        END
                    WHERE role IN ('SUPER_ADMIN', 'ADMIN')
                       OR email IN ('admin@callzenza.com', 'admin@callmira.com', 'superadmin@callzenza.com')
                """),
                {"verified": 1 if is_sqlite else True},
            )
            # Backfill registration_source if UNKNOWN
            await conn.execute(
                text("""
                    UPDATE businesses 
                    SET registration_source = 'SELF_REGISTERED'
                    WHERE (registration_source IS NULL OR registration_source = 'UNKNOWN')
                      AND (status = 'pending_verification' OR id IN (
                          SELECT business_id FROM users WHERE role = 'ADMIN' AND email != 'admin@callzenza.com'
                      ))
                """)
            )
            await conn.execute(
                text("""
                    UPDATE businesses 
                    SET registration_source = 'SUPER_ADMIN_CREATED'
                    WHERE (registration_source IS NULL OR registration_source = 'UNKNOWN');
                """)
            )
    except Exception as e:
        logger.warning(f"[MULTI-TENANT MIGRATION] Verify admins note: {e}")

    # 8. Clean up any invalid Expert records accidentally created for non-voice-agent users (SUPER_ADMIN, ADMIN, SUPERVISOR, BUSINESS_OWNER, USER)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                    DELETE FROM experts
                    WHERE user_id IN (
                        SELECT id FROM users
                        WHERE UPPER(role) IN ('SUPER_ADMIN', 'ADMIN', 'SUPERVISOR', 'BUSINESS_OWNER', 'USER')
                    )
                    OR LOWER(email) IN ('admin@callzenza.com', 'admin@callmira.com', 'superadmin@callzenza.com', 'superv@gmail.com');
                """)
            )
    except Exception as e:
        logger.warning(f"[MULTI-TENANT MIGRATION] Clean non-voice-agent experts note: {e}")

    logger.info("[MULTI-TENANT] Additive migration and seeding complete.")
