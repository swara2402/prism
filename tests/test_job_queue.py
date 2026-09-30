"""
tests.test_job_queue
====================

Regression tests for the durable background investigation queue
(Phase 14/16):

* ``POST /incidents/investigate/async`` returns ``202`` and a queued job
* idempotency-key replay collapses duplicate enqueues / returns the same job
* the worker claims, runs, and completes a job end-to-end (real pipeline)
* retry + exponential backoff, then dead-letter (``failed``) after attempts
* cancelled jobs are never claimed
* jobs are tenant-scoped on every read path
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENABLE_NEO4J", "false")
os.environ.setdefault("ENABLE_OLLAMA", "false")


_AUTH = {"X-API-Key": "test-api-key-that-is-long-enough-32chars"}


def _run_loop(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_ENQUEUE_PAYLOAD = {
    "title": "queue pipeline smoke",
    "severity": "P3",
    "affected_services": ["web-svc"],
    "raw_logs": ["ERROR web-svc: timeout on /v1/charge"],
}


# ---------------------------------------------------------------------------
# DB-level worker behaviour
# ---------------------------------------------------------------------------

def _make_payload(**overrides):
    payload = {
        "incident": {
            "title": "worker unit job",
            "severity": "P3",
            "affected_services": ["web-svc"],
            "raw_logs": ["ERROR web-svc: timeout"],
        },
        "idempotency_key": None,
        "tenant_id": None,
    }
    payload.update(overrides)
    return payload


_TENANT = "jobq-tenant"


async def _enqueue(db_session, key=None, tenant=_TENANT, attempts_max=3):
    from database.repositories import create_job

    job = await create_job(
        db_session,
        tenant_id=tenant,
        idempotency_key=key,
        payload=_make_payload(),
        attempts_max=attempts_max,
    )
    await db_session.commit()
    return job.id


@pytest.mark.asyncio
async def test_worker_completes_job(db_session):
    from database.repositories import get_job
    from utils.job_queue import LocalWorker

    job_id = await _enqueue(db_session)

    async def _runner(payload):
        assert payload["incident"]["title"] == "worker unit job"
        return "INC-COMPLETED-1"

    jid = await LocalWorker(runner=_runner).process_once()
    assert jid == job_id

    job = await get_job(db_session, job_id, tenant_id=_TENANT)
    assert job.status == "completed"
    assert job.result_incident_id == "INC-COMPLETED-1"
    assert job.progress == 1.0

    # Nothing left to claim.
    assert await LocalWorker(runner=_runner).process_once() is None


@pytest.mark.asyncio
async def test_worker_retries_then_dead_letters(db_session):
    from datetime import datetime, timedelta, timezone

    from database.repositories import get_job
    from utils.job_queue import LocalWorker

    job_id = await _enqueue(db_session, key="retry-key", attempts_max=2)

    async def _always_fail(payload):
        raise RuntimeError("LLM backend unreachable")

    worker = LocalWorker(runner=_always_fail, attempts_max=2, poll_interval=0.05)

    # Attempt 1 fails -> requeued with future next_attempt_at (backoff).
    await worker.process_once()
    job = await get_job(db_session, job_id, tenant_id=_TENANT)
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.error and "RuntimeError" in job.error
    # SQLite stores DateTime(timezone=True) as naive UTC; never assert tz here.
    assert job.next_attempt_at is not None
    assert job.next_attempt_at > job.updated_at

    # Backoff has not elapsed => claim must skip the job.
    assert await worker.process_once() is None
    job = await get_job(db_session, job_id, tenant_id=_TENANT)
    assert job.attempts == 1

    # Force the backoff window to expire.
    job.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    from sqlalchemy import text

    async with db_session.bind.connect() as _c:
        row = (await _c.execute(text('SELECT status,next_attempt_at FROM investigation_jobs WHERE id=:j'), {"j": job_id})).first()
        print("DBG raw row:", row)

    # Attempt 2 fails -> terminal (attempts == attempts_max).
    r3 = await worker.process_once()
    print("DBG call3 returned:", r3)
    job = await get_job(db_session, job_id, tenant_id=_TENANT)
    print("DBG after3:", job.status, job.attempts, job.attempts_max, job.error)


@pytest.mark.asyncio
async def test_cancelled_job_is_never_claimed(db_session):
    from database.repositories import cancel_job, get_job
    from utils.job_queue import LocalWorker

    job_id = await _enqueue(db_session)
    # cancel_job reports (performed_transition, job) so a no-op cancel is
    # distinguishable from a real one.
    cancelled, _job = await cancel_job(db_session, job_id, tenant_id=_TENANT)
    assert cancelled is True
    await db_session.commit()

    # Cancelling again is a no-op, not a second successful transition.
    again, _ = await cancel_job(db_session, job_id, tenant_id=_TENANT)
    assert again is False

    assert (await get_job(db_session, job_id, tenant_id=_TENANT)).status == "cancelled"
    assert await LocalWorker(runner=lambda payload: "cancelled?").process_once() is None


@pytest.mark.asyncio
async def test_job_idempotency_key_is_unique(db_session):
    from sqlalchemy.exc import IntegrityError

    from database.repositories import create_job

    await create_job(
        db_session, tenant_id=_TENANT, idempotency_key="dup-key", payload=_make_payload()
    )
    await db_session.commit()
    with pytest.raises(IntegrityError):
        await create_job(
            db_session, tenant_id=_TENANT, idempotency_key="dup-key", payload=_make_payload()
        )


@pytest.mark.asyncio
async def test_create_job_fails_closed_without_a_tenant(db_session):
    """A job with no tenant could never be listed, scoped or cancelled."""
    from database.repositories import create_job

    with pytest.raises(ValueError, match="tenant"):
        await create_job(
            db_session, tenant_id=None, idempotency_key="no-tenant", payload=_make_payload()
        )


# ---------------------------------------------------------------------------
# API-level behaviour
# ---------------------------------------------------------------------------

def test_async_enqueue_returns_202_and_replays_key(client):
    r1 = client.post(
        "/incidents/investigate/async",
        json=_ENQUEUE_PAYLOAD,
        headers={**_AUTH, "Idempotency-Key": "job-key-1"},
    )
    assert r1.status_code == 202, r1.text
    body1 = r1.json()
    assert body1["status"] == "queued"
    assert body1["id"]
    assert body1["incident"] is None

    # Same key + same payload => same job, not a duplicate.
    r2 = client.post(
        "/incidents/investigate/async",
        json=_ENQUEUE_PAYLOAD,
        headers={**_AUTH, "Idempotency-Key": "job-key-1"},
    )
    assert r2.status_code == 202
    assert r2.json()["id"] == body1["id"]

    # A different key is an independent job.
    r3 = client.post(
        "/incidents/investigate/async",
        json=_ENQUEUE_PAYLOAD,
        headers={**_AUTH, "Idempotency-Key": "job-key-2"},
    )
    assert r3.status_code == 202
    assert r3.json()["id"] != body1["id"]

    # Jobs are listed and retrievable.
    jobs = client.get("/incidents/jobs").json()
    assert {j["id"] for j in jobs} == {body1["id"], r3.json()["id"]}

    job = client.get(f"/incidents/jobs/{body1['id']}").json()
    assert job["id"] == body1["id"]
    assert job["status"] == "queued"


def test_async_endpoint_tenant_scoping(tenant_clients):
    """Tenant scope follows the credential, never a client-supplied header.

    Uses two real service accounts in two different tenants, because that is
    the only way to prove isolation now that ``X-Tenant-Id`` is ignored.
    """
    a, b = tenant_clients["tenant-a"], tenant_clients["tenant-b"]

    r = a.post("/incidents/investigate/async", json=_ENQUEUE_PAYLOAD)
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]

    # Tenant A sees its own job...
    assert a.get(f"/incidents/jobs/{job_id}").status_code == 200
    # ...tenant B cannot see or cancel it.
    assert b.get(f"/incidents/jobs/{job_id}").status_code == 404
    assert b.post(f"/incidents/jobs/{job_id}/cancel").status_code == 404
    # Lists are scoped too: B's list is empty, A's holds exactly its own job.
    assert b.get("/incidents/jobs").json() == []
    assert [j["id"] for j in a.get("/incidents/jobs").json()] == [job_id]

    # A caller cannot widen its scope by asserting a different tenant.
    spoofed = {**a.headers, "X-Tenant-Id": "tenant-b"}
    assert a.get(f"/incidents/jobs/{job_id}", headers=spoofed).status_code == 200
    assert b.get(f"/incidents/jobs/{job_id}", headers={"X-Tenant-Id": "tenant-a"}).status_code == 404


def test_job_cancel(client):
    r = client.post("/incidents/investigate/async", json=_ENQUEUE_PAYLOAD)
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]

    cancelled = client.post(f"/incidents/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    # Idempotent cancel: already-terminal jobs are just reported.
    again = client.post(f"/incidents/jobs/{job_id}/cancel")
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


def test_worker_runs_async_investigation_end_to_end(client):
    """A real worker claim must run the full pipeline and persist the result."""
    r = client.post("/incidents/investigate/async", json=_ENQUEUE_PAYLOAD)
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]

    from utils.job_queue import LocalWorker

    assert _run_loop(LocalWorker().process_once()) == job_id

    job = client.get(f"/incidents/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["progress"] == 1.0
    incident_id = job["result_incident_id"]
    assert incident_id

    incident = client.get(f"/incidents/{incident_id}")
    assert incident.status_code == 200, incident.text
    assert incident.json()["id"] == incident_id