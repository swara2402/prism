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
os.environ["DATABASE_SYNC_URL"] = "sqlite:///./waypoint-ci.db"
os.environ["ENABLE_NEO4J"] = "false"
os.environ["ENABLE_OLLAMA"] = "false"
os.environ["ENABLE_FAISS"] = "false"
os.environ["SENTENCE_TRANSFORMER_MODEL"] = "all-MiniLM-L6-v2"
os.environ["API_KEY"] = "test-api-key-that-is-long-enough-32chars"
os.environ["CORS_ORIGINS"] = "http://testclient"


@pytest_asyncio.fixture
async def db_session():
    """Provide an in-memory SQLite session for each test."""
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
    """Start every pytest session from an empty PRISM data dir."""
    import shutil

    from config.settings import settings

    data = settings.data_dir
    if data.name == "prism_test_data":
        shutil.rmtree(data, ignore_errors=True)
        data.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture(autouse=True)
def _reset_learning_state():
    """Reset all process-global learning and request state between tests."""
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

    inv = sys.modules.get("api.investigation")
    limiter = getattr(inv, "_rate_limiter", None) if inv is not None else None
    if limiter is not None:
        hits = getattr(limiter, "_hits", None)
        if hits is not None:
            hits.clear()
    yield


@pytest.fixture
def client():
    """FastAPI TestClient."""
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)
