"""Durable background investigation queue for WayPoint."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from auth.tenant_context import reset as reset_tenant, set_tenant
from config.settings import settings

logger = logging.getLogger("waypoint.job_queue")


async def run_investigation_job(payload: dict) -> str:
    """Execute a queued investigation under its trusted tenant context."""
    from api.investigation import _run_investigation
    from models.schemas import IncidentCreate

    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        raise ValueError("Queued investigations require tenant_id")
    token = set_tenant(tenant_id)
    try:
        incident_in = IncidentCreate(**payload["incident"])
        result = await _run_investigation(
            incident_in,
            request_id=payload.get("request_id") or "job",
            idempotency_key=payload.get("idempotency_key"),
            tenant_id=tenant_id,
        )
        return result.incident_id
    finally:
        reset_tenant(token)


def _backoff(attempt: int, base_seconds: float) -> timedelta:
    return timedelta(seconds=base_seconds * (2 ** max(0, attempt - 1)) * (0.5 + random.random() / 2))


class LocalWorker:
    """Async worker with stale-lease recovery and cooperative cancellation."""

    def __init__(self, runner: Optional[Callable[[dict], Awaitable[str]]] = None, *, poll_interval: float = 1.0, attempts_max: int = 3, worker_id: Optional[str] = None) -> None:
        self._runner = runner or run_investigation_job
        self.poll_interval = max(0.05, float(poll_interval))
        self.attempts_max = max(1, int(attempts_max))
        self.worker_id = worker_id or f"worker-{random.randrange(1 << 30):x}"
        self._logger = logger

    async def _reclaim_stale_jobs(self, session) -> None:
        """Return jobs abandoned by a dead worker to the queue."""
        from sqlalchemy import update
        from database import models as dbm

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(30, settings.job_lease_seconds))
        result = await session.execute(
            update(dbm.InvestigationJob)
            .where(
                dbm.InvestigationJob.status == "running",
                dbm.InvestigationJob.locked_at.is_not(None),
                dbm.InvestigationJob.locked_at < cutoff,
            )
            .values(status="queued", error="Worker lease expired; job reclaimed", locked_at=None)
        )
        if result.rowcount:
            self._logger.warning("stale_jobs_reclaimed", extra={"count": result.rowcount})

    async def _job_status(self, session, job_id: str) -> Optional[str]:
        from database import models as dbm
        job = await session.get(dbm.InvestigationJob, job_id)
        return job.status if job else None

    async def process_once(self) -> Optional[str]:
        from database.repositories import claim_next_job, update_job
        from database.session import get_async_session_local

        session_local = get_async_session_local()
        async with session_local() as session:
            await self._reclaim_stale_jobs(session)
            await session.commit()
            job = await claim_next_job(session, self.worker_id)
            if job is None:
                return None
            await session.commit()
            job_id = job.id
            attempts = job.attempts or 0
            payload = dict(job.payload or {})
            job_attempts_max = job.attempts_max or self.attempts_max

        start = datetime.now(timezone.utc)
        try:
            async with session_local() as session:
                if await self._job_status(session, job_id) == "cancelled":
                    return job_id
                await update_job(session, job_id, status="running", progress=0.5)
                await session.commit()

            incident_id = await self._runner(payload)

            async with session_local() as session:
                if await self._job_status(session, job_id) == "cancelled":
                    self._logger.info("job_cancelled_after_runner", extra={"job_id": job_id})
                    return job_id
                await update_job(session, job_id, status="completed", result_incident_id=incident_id, progress=1.0)
                await session.commit()
            self._logger.info("job_completed", extra={"job_id": job_id, "result_incident_id": incident_id, "attempts": attempts})
            return job_id
        except asyncio.CancelledError:
            async with session_local() as session:
                if await self._job_status(session, job_id) != "cancelled":
                    await update_job(session, job_id, status="queued")
                    await session.commit()
            raise
        except Exception as exc:
            terminal = attempts >= job_attempts_max
            error = f"{type(exc).__name__}: {exc}"
            wait = _backoff(attempts, base_seconds=max(0.5, self.poll_interval))
            status = "failed" if terminal else "queued"
            next_attempt = None if terminal else start + wait
            async with session_local() as session:
                if await self._job_status(session, job_id) == "cancelled":
                    self._logger.info("job_failure_ignored_after_cancel", extra={"job_id": job_id})
                    return job_id
                await update_job(session, job_id, status=status, error=error, next_attempt_at=next_attempt)
                await session.commit()
            self._logger.warning("job_attempt_failed", extra={"job_id": job_id, "attempts": attempts, "attempts_max": job_attempts_max, "terminal": terminal, "next_attempt_at": next_attempt.isoformat() if next_attempt else None, "error": error})
            return job_id

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                claimed_id = await self.process_once()
            except Exception:
                self._logger.exception("worker_loop_error")
                claimed_id = None
            if claimed_id is None:
                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    return


async def enqueue_investigation(incident_payload: dict, *, idempotency_key: Optional[str], tenant_id: Optional[str], attempts_max: int = 3):
    from database.repositories import create_job
    from database.session import get_async_session_local

    if not tenant_id:
        raise ValueError("tenant_id is required when enqueueing an investigation")
    session_local = get_async_session_local()
    async with session_local() as session:
        job = await create_job(session, tenant_id=tenant_id, idempotency_key=idempotency_key, payload=incident_payload, attempts_max=attempts_max)
        await session.commit()
        return job
