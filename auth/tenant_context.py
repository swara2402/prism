"""Trusted tenant context propagated from authentication into deep services."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_current_tenant: ContextVar[Optional[str]] = ContextVar("prism_tenant", default=None)


def set_tenant(tenant_id: Optional[str]):
    return _current_tenant.set(tenant_id)


def get_tenant() -> Optional[str]:
    return _current_tenant.get()


def reset(token) -> None:
    _current_tenant.reset(token)
