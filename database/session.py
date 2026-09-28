"""
database.session
================

Async SQLAlchemy engine + session factory.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


class Base(DeclarativeBase):
    """Common SQLAlchemy declarative base."""


def _build_engine():
    """Build the async engine with pool options appropriate for the dialect."""
    url = settings.database_url
    kwargs: dict[str, object] = {"echo": settings.db_echo, "future": True}
    # SQLite (incl. aiosqlite) does not support pool_size / max_overflow
    if not url.startswith("sqlite"):
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    return create_async_engine(url, **kwargs)


# Lazy-initialized engine and session factory
_engine = None
_async_session_local = None

def get_engine():
    """Get or create the async engine lazily."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine

def get_async_session_local():
    """Get or create the async session factory lazily."""
    global _async_session_local
    if _async_session_local is None:
        engine = get_engine()
        _async_session_local = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _async_session_local


def reset_engine() -> None:
    """Dispose and clear the lazy engine + session factory.

    The next ``get_engine()`` / ``AsyncSessionLocal`` access rebuilds both
    from the *current* settings, so each test / evaluation scenario starts
    from a genuinely fresh database instead of inheriting earlier state.
    """
    global _engine, _async_session_local
    old = _engine
    _engine = None
    _async_session_local = None
    if old is not None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(old.dispose())
        else:
            asyncio.get_running_loop().create_task(old.dispose())

# Maintain backwards compatibility - expose the same global names
# They will be initialized on first access
class LazySessionFactory:
    def __call__(self, **kwargs):
        return get_async_session_local()(**kwargs)
    
    def __getattr__(self, name):
        return getattr(get_async_session_local(), name)

class LazyEngine:
    def __getattr__(self, name):
        return getattr(get_engine(), name)

engine = LazyEngine()
AsyncSessionLocal = LazySessionFactory()


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Context manager yielding an :class:`AsyncSession`."""
    AsyncSessionLocal = get_async_session_local()
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session."""
    AsyncSessionLocal = get_async_session_local()
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables.  Called on application startup."""
    # Import here so that all models are registered on Base.metadata
    from database import models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Dispose of the engine.  Called on application shutdown."""
    engine = get_engine()
    await engine.dispose()