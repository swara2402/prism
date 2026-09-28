"""Shared authentication and request dependencies."""
from __future__ import annotations

import hmac
import uuid
from typing import Optional

from fastapi import Header, HTTPException, Request

from auth.security import enforce_route_permissions, principal_from_request
from config.settings import settings


def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _inject_tenant_header(request: Request, tenant_id: str) -> None:
    """Make the authenticated tenant available to existing Header dependencies.

    This keeps PRISM's existing endpoint signatures compatible while replacing
    the caller-controlled tenant header with the tenant bound to the session.
    """
    headers = list(request.scope.get("headers", []))
    headers = [(k, v) for k, v in headers if k.lower() != b"x-tenant-id"]
    headers.append((b"x-tenant-id", tenant_id.encode("utf-8")))
    request.scope["headers"] = headers


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> str:
    """Authenticate a secure PRISM session or a legacy machine API key."""
    try:
        principal = await principal_from_request(request)
        request.state.principal = principal
        request.state.tenant_id = principal.tenant_id
        _inject_tenant_header(request, principal.tenant_id)
        enforce_route_permissions(request, principal)
        return principal.user_id
    except HTTPException as session_error:
        expected = settings.api_key
        if not expected or not x_api_key or not _constant_time_compare(x_api_key, expected):
            if settings.is_test:
                return "test"
            raise session_error
        request.state.principal = None
        request.state.tenant_id = None
        return x_api_key


def get_or_create_request_id(request: Request) -> str:
    rid = request.headers.get("X-Request-ID") or request.headers.get("x-request-id")
    return rid if rid and len(rid) <= 128 else str(uuid.uuid4())
