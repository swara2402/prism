"""Shared authentication and request dependencies."""
from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import Header, HTTPException, Request
from sqlalchemy import select

from auth.security import enforce_route_permissions, principal_from_request, Principal
from config.settings import settings
from database.auth_models import ServiceAccount, Tenant
from database.session import AsyncSessionLocal


def _constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _inject_tenant_header(request: Request, tenant_id: str) -> None:
    headers = list(request.scope.get("headers", []))
    headers = [(k, v) for k, v in headers if k.lower() != b"x-tenant-id"]
    headers.append((b"x-tenant-id", tenant_id.encode("utf-8")))
    request.scope["headers"] = headers


async def _service_account_principal(token: str) -> Optional[Principal]:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ServiceAccount, Tenant)
            .join(Tenant, Tenant.id == ServiceAccount.tenant_id)
            .where(
                ServiceAccount.token_hash == token_hash,
                ServiceAccount.is_active.is_(True),
                Tenant.is_active.is_(True),
            )
        )
        row = result.first()
        if not row:
            return None
        account, tenant = row
        now = datetime.now(timezone.utc)
        if account.expires_at is not None and account.expires_at <= now:
            return None
        account.last_used_at = now
        await session.commit()
        return Principal(
            user_id=f"service:{account.id}",
            email=f"service:{account.name}",
            tenant_id=account.tenant_id,
            role=account.role,
            tenant_name=tenant.name,
        )


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> str:
    """Authenticate a session or a tenant-bound machine credential.

    The legacy global API_KEY is intentionally not accepted for production
    traffic.  Service accounts carry their tenant identity server-side.
    """
    try:
        principal = await principal_from_request(request)
    except HTTPException as session_error:
        principal = None
        if x_api_key:
            principal = await _service_account_principal(x_api_key)
        if principal is None:
            if settings.is_test:
                return "test"
            raise session_error

    request.state.principal = principal
    request.state.tenant_id = principal.tenant_id
    _inject_tenant_header(request, principal.tenant_id)
    enforce_route_permissions(request, principal)
    return principal.user_id


def get_or_create_request_id(request: Request) -> str:
    rid = request.headers.get("X-Request-ID") or request.headers.get("x-request-id")
    return rid if rid and len(rid) <= 128 else str(uuid.uuid4())
