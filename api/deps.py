"""
api.deps
========

Shared FastAPI dependencies: API-key authentication, request ID, etc.
"""
from __future__ import annotations

import hmac
import uuid
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from config.settings import settings


def _constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison using hmac.compare_digest."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> str:
    """
    Validate the ``X-API-Key`` header.

    - In production / when an API key is configured: require a matching key.
    - In development/test with no key configured: allow (open mode).
    Fail closed when a key *is* configured or when running in production.
    """
    expected = settings.api_key

    # Production always requires a configured key (validated at startup too)
    if settings.is_production and not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured",
        )

    # No key configured → open mode (dev/test only)
    if not expected:
        return "anonymous"

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if not _constant_time_compare(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    return x_api_key


def get_or_create_request_id(request: Request) -> str:
    """Return existing X-Request-ID or generate a new one."""
    rid = request.headers.get("X-Request-ID") or request.headers.get("x-request-id")
    if rid and len(rid) <= 128:
        return rid
    return str(uuid.uuid4())
