from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from database import models as dbm
from utils.job_queue import LocalWorker


@pytest.mark.asyncio
async def test_stale_running_job_is_reclaimed(db_session, monkeypatch):
    job = dbm.InvestigationJob(
        tenant_id="tenant-a",
        payload={"incident": {"title": "test"}, "tenant_id": "tenant-a"},
        status="running",
        attempts=1,
        attempts_max=3,
        locked_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.commit()

    worker = LocalWorker(poll_interval=0.05)
    await worker._reclaim_stale_jobs(db_session)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.status == "queued"
    assert job.locked_at is None
    assert "lease expired" in (job.error or "")
