"""Shared authentication and request dependencies."""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Optional

from fastapi import Header, HTTPException, Request, status
from sqlalchemy import select

from auth.security import Principal, enforce_route_permissions, principal_from_request
from auth.tenant_context import reset as reset_tenant
from auth.tenant_context import set_tenant
from config.settings import settings
from database.auth_models import ServiceAccount, Tenant
from database.session import AsyncSessionLocal

# A client-supplied correlation id is echoed into response headers and logs, so
# it must be constrained to an unambiguous, single-line token.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# ``last_used_at`` is a coarse audit signal. Writing it on every request turns
# the auth path into a commit-per-request amplifier, so it is throttled.
_LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)

_PUBLIC_PATHS = frozenset({"/health", "/ready", "/login", "/auth/login", "/", "/static"})


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a stored timestamp to a timezone-aware UTC datetime.

    The columns are declared ``DateTime(timezone=True)``, but SQLite (and some
    driver configurations) hand back naive datetimes regardless. Comparing one
    of those against an aware ``datetime.now(timezone.utc)`` raises
    TypeError, which turned both the expiry check and the last-used throttle
    into a 500 - so an expired token failed closed for the wrong reason.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _service_account_principal(token: str) -> Optional[Principal]:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
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
        expires_at = _as_utc(account.expires_at)
        if expires_at is not None and expires_at <= now:
            return None
        last_used = _as_utc(account.last_used_at)
        if last_used is None or (now - last_used) > _LAST_USED_WRITE_INTERVAL:
            account.last_used_at = now
            await session.commit()
        return Principal(
            user_id=f"service:{account.id}",
            email=f"service:{account.name}",
            tenant_id=account.tenant_id,
            role=account.role,
            tenant_name=tenant.name,
            scopes=tuple(account.scopes or ()),
        )


async def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> AsyncGenerator[str, None]:
    """Authenticate a session, tenant-bound service credential, or test principal.

    The returned tenant is authoritative: it is bound to the credential and
    published on ``request.state``. Clients cannot influence it.

    There is no environment-based bypass. Tests inject a principal with
    ``app.dependency_overrides`` so the auth path is exercised (or deliberately
    bypassed) by the test that owns it, rather than by a flag that silently
    applies to every test in the process.
    """
    try:
        principal = await principal_from_request(request)
    except HTTPException as session_error:
        principal = await _service_account_principal(x_api_key) if x_api_key else None
        if principal is None:
            # Re-raising the session error verbatim told an API-key caller to
            # "sign in", which they cannot do with a header credential.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Invalid or expired credentials. Provide a valid session "
                    "cookie or an X-API-Key header."
                ),
                headers={"WWW-Authenticate": "Bearer"},
            ) from session_error

    request.state.principal = principal
    request.state.tenant_id = principal.tenant_id
    token = set_tenant(principal.tenant_id)
    try:
        enforce_route_permissions(request, principal)
        yield principal.user_id
    finally:
        # The tenant context is a ContextVar. Leaving it set means any later
        # work scheduled from this task (or any host that reuses a task across
        # requests) inherits the previous request's tenant.
        reset_tenant(token)


async def require_tenant(request: Request) -> str:
    """Return the tenant bound to the authenticated credential.

    This is the only supported source of tenant identity. There is deliberately
    no ``X-Tenant-Id`` fallback: tenant is an authentication property, not a
    client-selected routing value.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=403, detail="No tenant is bound to this credential")
    return tenant_id


def get_or_create_request_id(request: Request) -> str:
    rid = request.headers.get("X-Request-ID", "")
    return rid if _REQUEST_ID_RE.match(rid) else str(uuid.uuid4())


def is_public_path(path: str) -> bool:
    return path.rstrip("/") in _PUBLIC_PATHS
