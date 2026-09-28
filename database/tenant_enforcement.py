"""Database-level tenant guard for request and worker scoped writes."""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from auth.tenant_context import get_tenant
from config.settings import settings
from database import models as dbm


_TENANT_MODELS = (dbm.Incident, dbm.InvestigationJob)


@event.listens_for(Session, "before_flush")
def _enforce_tenant_on_flush(session: Session, flush_context, instances) -> None:
    trusted = get_tenant()

    for obj in session.new:
        if not isinstance(obj, _TENANT_MODELS):
            continue
        if obj.tenant_id is None:
            if trusted is not None:
                obj.tenant_id = trusted
            elif settings.is_production:
                raise RuntimeError("Tenant context is required for production database writes")
        elif trusted is not None and obj.tenant_id != trusted:
            raise RuntimeError("Attempted cross-tenant database write")

    for obj in session.dirty:
        if not isinstance(obj, _TENANT_MODELS):
            continue
        if trusted is not None and obj.tenant_id != trusted:
            raise RuntimeError("Attempted cross-tenant database update")
