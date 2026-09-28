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


@pytest.fixture
def api_headers():
    """Headers with valid test API key."""
    return {"X-API-Key": "test-api-key-that-is-long-enough-32chars"}


@pytest.fixture
def client(api_headers):
    """FastAPI TestClient with auth headers helper."""
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)
