"""Shared authentication and request dependencies."""
from __future__ import annotations

import hmac
import uuid
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from auth.security import Principal, enforce_route_permissions, principal_from_request
from config.settings import settings


def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> str:
    """Authenticate either the secure PRISM session cookie/Bearer token or legacy API key.

    API keys remain available for machine-to-machine integrations. Browser users
    use the HttpOnly PRISM session cookie and therefore never need to store a
    long-lived secret in localStorage.
    """
    try:
        principal = await principal_from_request(request)
        request.state.principal = principal
        request.state.tenant_id = principal.tenant_id
        enforce_route_permissions(request, principal)
        return principal.user_id
    except HTTPException as session_error:
        expected = settings.api_key
        if not expected or not x_api_key or not _constant_time_compare(x_api_key, expected):
            if settings.is_test:
                return "test"
            raise session_error
        # Legacy API keys are intentionally tenantless. They are suitable for
        # single-tenant integrations only. They cannot claim a tenant by header.
        request.state.principal = None
        request.state.tenant_id = None
        return x_api_key


def get_or_create_request_id(request: Request) -> str:
    rid = request.headers.get("X-Request-ID") or request.headers.get("x-request-id")
    return rid if rid and len(rid) <= 128 else str(uuid.uuid4())
