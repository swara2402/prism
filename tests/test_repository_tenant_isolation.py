from __future__ import annotations

import pytest

from auth.tenant_context import reset, set_tenant
from database import models as dbm
from database.repositories import get_incident, get_root_cause, list_findings, update_incident


@pytest.mark.asyncio
async def test_incident_repository_cannot_cross_tenants(db_session):
    token_a = set_tenant("tenant-a")
    try:
        incident = dbm.Incident(
            title="Tenant A incident",
            tenant_id="tenant-a",
            affected_services=["checkout"],
            raw_logs=[], metrics={}, traces=[], topology={}, context={},
        )
        db_session.add(incident)
        await db_session.commit()
        incident_id = incident.id
    finally:
        reset(token_a)

    token_b = set_tenant("tenant-b")
    try:
        assert await get_incident(db_session, incident_id) is None
        assert await list_findings(db_session, incident_id) == []
        assert await get_root_cause(db_session, incident_id) is None
        assert await update_incident(db_session, incident_id, status="resolved") is False
    finally:
        reset(token_b)


@pytest.mark.asyncio
async def test_repository_requires_tenant_context(db_session):
    with pytest.raises(RuntimeError, match="Tenant context"):
        await get_incident(db_session, "does-not-exist")
