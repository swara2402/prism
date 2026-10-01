"""
tests.conftest
==============

Shared pytest fixtures.

Uses SQLite (in-memory) so tests can run without a PostgreSQL
server.  Neo4j, Ollama, and FAISS are stubbed / disabled.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Make sure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Force test environment BEFORE any settings import
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["DATABASE_SYNC_URL"] = "sqlite:///:memory:"
os.environ["ENABLE_NEO4J"] = "false"
os.environ["ENABLE_OLLAMA"] = "false"
os.environ["ENABLE_FAISS"] = "false"
os.environ["SENTENCE_TRANSFORMER_MODEL"] = "all-MiniLM-L6-v2"
# Test API key (long enough)
os.environ["API_KEY"] = "test-api-key-that-is-long-enough-32chars"
os.environ["CORS_ORIGINS"] = "http://testclient"


@pytest_asyncio.fixture
async def db_session():
    """Provide an in-memory SQLite session for each test."""
    # Clear settings cache so test env is picked up
    from config.settings import get_settings
    get_settings.cache_clear()

    from database.session import Base, engine, AsyncSessionLocal

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.rollback()
            await session.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def event_loop():
    """Override the event loop to allow nested asyncio in tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session", autouse=True)
def _clean_test_data_dir():
    """Start every pytest session from an empty PRISM data dir.

    ``settings.data_dir`` is a stable test-mode path (``<tmp>/prism_test_data``)
    shared across processes and runs.  Learning stores persist JSON there, so a
    stale file from an older run could otherwise resurrect state after the
    per-test in-memory resets.  Purge it once per session for a reproducible,
    fresh evaluation environment.
    """
    import shutil

    from config.settings import settings

    data = settings.data_dir
    if data.name == "prism_test_data":
        shutil.rmtree(data, ignore_errors=True)
        data.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture(autouse=True)
def _reset_learning_state():
    """Strict per-test isolation for all module-level learning state.

    Reliability EMA, action-effectiveness EMA, memory index, knowledge-graph
    fallback graph, and the DB engine are all process-global singletons.
    Without resetting them, an earlier scenario silently contaminates the
    next (Phase 3: reproducible, independent evaluation runs).  The persisted
    JSON stores are live files under ``settings.data_dir``, so the on-disk
    state is purged before every test as well — otherwise an earlier test
    that wrote to the store would be reloaded (not just cached) by a later
    one.
    """
    import shutil

    from config.settings import settings

    data = settings.data_dir
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True, exist_ok=True)

    from investigation.reliability_store import reset_reliability_cache
    from investigation.action_effectiveness import reset_effectiveness_cache
    from memory.store import MemoryStore
    from knowledge_graph.store import KnowledgeGraphStore
    from database.session import reset_engine

    reset_reliability_cache()
    reset_effectiveness_cache()
    MemoryStore.reset()
    KnowledgeGraphStore.reset()
    reset_engine()

    # The request rate limiter is a process-global sliding window keyed by
    # API key; without clearing it, calls from earlier tests would count
    # against later ones and trip spurious 429s.
    inv = sys.modules.get("api.investigation")
    limiter = getattr(inv, "_rate_limiter", None) if inv is not None else None
    if limiter is not None:
        hits = getattr(limiter, "_hits", None)
        if hits is not None:
            hits.clear()
    yield


TEST_TENANT_ID = "test-tenant"
TEST_USER_EMAIL = "test@waypoint.local"


@pytest.fixture
def api_headers():
    """Headers with valid test API key."""
    return {"X-API-Key": "test-api-key-that-is-long-enough-32chars"}


def _test_principal(role: str = "owner", tenant_id: str = TEST_TENANT_ID):
    from auth.security import Principal

    return Principal(
        user_id="test-user",
        email=TEST_USER_EMAIL,
        tenant_id=tenant_id,
        role=role,
        tenant_name="Test Workspace",
        scopes=("read", "write", "admin"),
    )


def _install_auth_overrides(app, *, role: str = "owner", tenant_id: str = TEST_TENANT_ID):
    """Authenticate every request as a synthetic principal.

    Implemented with FastAPI's ``dependency_overrides`` so the decision lives in
    the test that makes it. The previous environment-gated bypass in
    ``require_api_key`` applied to every test in the process, which is why the
    auth-rejection tests reported 200.
    """
    from api.deps import require_api_key, require_tenant

    principal = _test_principal(role=role, tenant_id=tenant_id)

    async def _fake_api_key(request):
        request.state.principal = principal
        request.state.tenant_id = principal.tenant_id
        return principal.user_id

    async def _fake_tenant():
        return principal.tenant_id

    app.dependency_overrides[require_api_key] = _fake_api_key
    app.dependency_overrides[require_tenant] = _fake_tenant
    return principal


@pytest.fixture
def client(api_headers):
    """Authenticated FastAPI TestClient.

    Entered as a context manager so the app's lifespan actually runs: returning
    a bare ``TestClient(app)`` skipped startup entirely, so no schema was
    created and no stores were initialised.
    """
    from fastapi.testclient import TestClient
    from main import app

    _install_auth_overrides(app)
    try:
        with TestClient(app) as c:
            c.headers.update(api_headers)
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def anon_client():
    """TestClient with the real authentication path and no credentials.

    Use this for tests that assert 401/403: it is the only client that does not
    have a principal injected.
    """
    from fastapi.testclient import TestClient
    from main import app

    app.dependency_overrides.clear()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sa_client(api_headers):
    """TestClient authenticating with a real service-account token."""
    from fastapi.testclient import TestClient
    from main import app

    app.dependency_overrides.clear()
    with TestClient(app) as c:
        # Provisioned lazily: the schema exists only after lifespan startup.
        token = _provision_service_account_sync()
        c.headers.update({"X-API-Key": token})
        yield c


def _provision_service_account_sync(
    tenant_id: str = TEST_TENANT_ID,
    *,
    service_account_id: str = "sa-test",
) -> str:
    """Create a service account from synchronous code (TestClient context)."""
    import asyncio
    import hashlib
    import secrets

    from api.auth import SCOPES_BY_ROLE
    from database.auth_models import ServiceAccount, Tenant
    from database.session import AsyncSessionLocal

    token = "prism_sa_" + secrets.token_urlsafe(32)

    async def _make() -> None:
        async with AsyncSessionLocal() as session:
            existing = await session.get(Tenant, tenant_id)
            if existing is None:
                session.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Workspace {tenant_id}",
                        is_active=True,
                    )
                )
            session.add(
                ServiceAccount(
                    id=service_account_id,
                    tenant_id=tenant_id,
                    name="pytest",
                    token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    role="engineer",
                    # Derived from the production role map so this fixture
                    # cannot drift from the scopes the app actually enforces.
                    scopes=list(SCOPES_BY_ROLE["engineer"]),
                    is_active=True,
                )
            )
            await session.commit()

    asyncio.run(_make())
    return token


async def make_incident(
    session,
    incident_id: str,
    *,
    tenant_id: str = TEST_TENANT_ID,
    title: str = "test incident",
    **fields,
):
    """Insert a real ``Incident`` row.

    Learning and pattern persistence verify tenant ownership against the stored
    incident, so tests that exercise those paths need a real row rather than an
    invented id.
    """
    from database import models as dbm

    incident = dbm.Incident(
        id=incident_id,
        tenant_id=tenant_id,
        title=title,
        **{"affected_services": [], "raw_logs": [], **fields},
    )
    session.add(incident)
    await session.flush()
    return incident


@pytest.fixture
def tenant_clients():
    from contextlib import ExitStack

    from fastapi.testclient import TestClient
    from main import app

    app.dependency_overrides.clear()
    tenants = ("tenant-a", "tenant-b")
    with ExitStack() as stack:
        # The first client enters lifespan, which creates the schema.
        clients: dict[str, TestClient] = {}
        for i, tenant_id in enumerate(tenants):
            client = stack.enter_context(TestClient(app))
            client.headers.update(
                {
                    "X-API-Key": _provision_service_account_sync(
                        tenant_id, service_account_id=f"sa-{i}"
                    )
                }
            )
            clients[tenant_id] = client
        yield clients
