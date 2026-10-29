import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_callmira.db"

import pytest
import pytest_asyncio
from app.db.database import engine, init_db
from app.db.models import Base

@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def init_test_database():
    try:
        await init_db()
    except Exception:
        pass
    yield
