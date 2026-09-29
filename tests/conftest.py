"""
Top-level shared fixtures for all Vageesh tests.
"""
import gc
import os
from unittest.mock import AsyncMock, patch
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Use a dedicated test database so the dev DB is never touched
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost/symphony_test",
)

# NullPool disables connection pooling — every operation gets a fresh connection
# that is not held between event loops, which avoids "Future attached to a
# different loop" errors in pytest-asyncio.
_test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
_TestSessionLocal = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Session-scoped: import all models then create tables once, drop after session
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    # Importing app.main ensures every model is registered with Base.metadata
    import app.main  # noqa: F401
    from app.database import Base

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await _test_engine.dispose()
    gc.collect()


# ---------------------------------------------------------------------------
# Function-scoped: truncate all tables after every integration test
# Unit tests don't use the DB so we guard with a try/except.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    yield
    try:
        import sqlalchemy
        async with _test_engine.begin() as conn:
            await conn.execute(
                sqlalchemy.text(
                    "TRUNCATE TABLE agent_memory, messages, logs, "
                    "workflow_runs, workflows, agents, mcp_audit_log RESTART IDENTITY CASCADE"
                )
            )
    except Exception:
        pass  # Unit tests have no DB state to clean


# ---------------------------------------------------------------------------
# DB session fixture — used directly by unit/integration helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with _TestSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# AsyncClient fixture — overrides FastAPI dependencies with test DB
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client() -> AsyncClient:
    from app.main import app
    from app.database import get_db
    from app.dependencies import get_current_user

    async def _override_get_db():
        async with _TestSessionLocal() as session:
            yield session

    def _override_get_current_user():
        return {"id": "test-user"}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user

    # Prevent real Slack connections during tests.
    with patch("app.main.start_slack_bot", AsyncMock()), \
         patch("app.main.stop_slack_bot", AsyncMock()):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Convenience header fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token"}
