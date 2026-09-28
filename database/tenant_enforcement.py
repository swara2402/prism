"""Database-level tenant guard for request and worker scoped operations."""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from auth.tenant_context import get_tenant
from config.settings import settings
from database import models as dbm


_TENANT_MODELS = (dbm.Incident, dbm.InvestigationJob)


@event.listens_for(Session, "before_flush")
def _enforce_tenant_on_flush(session: Session, _flush_context, instances) -> None:
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


@event.listens_for(Session, "do_orm_execute")
def _scope_tenant_reads(execute_state) -> None:
    """Defense-in-depth tenant predicate for ORM SELECT/UPDATE/DELETE."""
    if execute_state.is_relationship_load or execute_state.is_column_load:
        return
    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return

    trusted = get_tenant()
    if trusted is None:
        if settings.is_production:
            raise RuntimeError("Tenant context is required for production ORM access")
        return

    statement = execute_state.statement
    if execute_state.is_select:
        statement = statement.options(
            with_loader_criteria(dbm.Incident, lambda cls: cls.tenant_id == trusted, include_aliases=True),
            with_loader_criteria(dbm.InvestigationJob, lambda cls: cls.tenant_id == trusted, include_aliases=True),
        )
    else:
        for desc in getattr(statement, "column_descriptions", []) or []:
            entity = desc.get("entity")
            if entity in _TENANT_MODELS:
                statement = statement.where(entity.tenant_id == trusted)
    execute_state.statement = statement
