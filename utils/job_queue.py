"""
utils.job_queue
===============

Durable background investigation queue (Phase 14/16).

``POST /incidents/investigate/async`` enqueues a job row and returns
``202``.  A :class:`LocalWorker` — one asyncio task per process, started by
``main.lifespan`` when ``JOB_WORKER_ENABLED=true`` — claims queued jobs,
runs the full investigation pipeline, and persists the result incident id.

Properties
----------

* Durable: jobs live in Postgres, so a worker restart does not lose them.
* Idempotent: the unique ``idempotency_key`` collapses duplicate enqueues;
  a retried job replays through the pipeline's own idempotency check.
* Retry with exponential backoff up to ``attempts_max``; jobs that exhaust
  retries become ``failed`` (dead-lettered) with the last error preserved.
* Cancellable while queued or running.
* Claim is atomic when Postgres is used (``FOR UPDATE SKIP LOCKED``), so
  multiple worker replicas can share one table.

The synchronous ``POST /incidents/investigate`` path is untouched; the async
endpoint and worker are an addition.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("prism.job_queue")


async def run_investigation_job(payload: dict) -> str:
    """Default job runner: execute the full PRISM pipeline for ``payload``.

    ``payload`` is the serialized queue document produced by the async
    endpoint: ``{"incident": {...IncidentCreate}, "idempotency_key": ...,
    "tenant_id": ...}``.  Returns the persisted incident id.
    """
    from api.investigation import _run_investigation
    from models.schemas import IncidentCreate

    incident_in = IncidentCreate(**payload["incident"])
    result = await _run_investigation(
        incident_in,
        request_id=payload.get("request_id") or "job",
        idempotency_key=payload.get("idempotency_key"),
        tenant_id=payload.get("tenant_id"),
    )
    return result.incident_id


def _backoff(attempt: int, base_seconds: float) -> timedelta:
    """Exponential backoff with jitter: base * 2^(attempt-1) seconds."""
    return timedelta(seconds=base_seconds * (2 ** max(0, attempt - 1)) * (0.5 + random.random() / 2))


class LocalWorker:
    """Asyncio worker draining the ``investigation_jobs`` table.

    ``process_once`` claims and runs a single job (used by tests and the
    run loop); ``run`` loops until ``stop_event`` is set.
    """

    def __init__(
        self,
        runner: Optional[Callable[[dict], Awaitable[str]]] = None,
        *,
        poll_interval: float = 1.0,
        attempts_max: int = 3,
        worker_id: Optional[str] = None,
    ) -> None:
        self._runner = runner or run_investigation_job
        self.poll_interval = max(0.05, float(poll_interval))
        self.attempts_max = max(1, int(attempts_max))
        self.worker_id = worker_id or f"worker-{random.randrange(1 << 30):x}"
        self._logger = logger

    async def process_once(self) -> Optional[str]:
        """Claim one job and run it to completion.  Returns the job id or None."""
        from database.repositories import claim_next_job, update_job
        from database.session import get_async_session_local

        session_local = get_async_session_local()

        async with session_local() as session:
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
                await update_job(session, job_id, status="running", progress=0.5)
                await session.commit()
            incident_id = await self._runner(payload)
            async with session_local() as session:
                await update_job(
                    session,
                    job_id,
                    status="completed",
                    result_incident_id=incident_id,
                    progress=1.0,
                )
                await session.commit()
            self._logger.info(
                "job_completed",
                extra={"job_id": job_id, "result_incident_id": incident_id, "attempts": attempts},
            )
            return job_id
        except asyncio.CancelledError:
            async with session_local() as session:
                await update_job(session, job_id, status="queued")
                await session.commit()
            raise
        except Exception as exc:  # noqa: BLE001 — job failures are recorded, not raised
            terminal = attempts >= job_attempts_max
            error = f"{type(exc).__name__}: {exc}"
            wait = _backoff(attempts, base_seconds=max(0.5, self.poll_interval))
            if terminal:
                status = "failed"
                next_attempt = None
            else:
                status = "queued"
                next_attempt = start + wait
            async with session_local() as session:
                await update_job(
                    session,
                    job_id,
                    status=status,
                    error=error,
                    next_attempt_at=next_attempt,
                )
                await session.commit()
            self._logger.warning(
                "job_attempt_failed",
                extra={
                    "job_id": job_id,
                    "attempts": attempts,
                    "attempts_max": job_attempts_max,
                    "terminal": terminal,
                    "next_attempt_at": next_attempt.isoformat() if next_attempt else None,
                    "error": error,
                },
            )
            return job_id

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """Drain queued jobs until ``stop_event`` is set (or forever)."""
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                claimed_id = await self.process_once()
            except Exception:  # noqa: BLE001 — worker must stay alive
                self._logger.exception("worker_loop_error")
                claimed_id = None
            if claimed_id is None:
                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    return


async def enqueue_investigation(
    incident_payload: dict,
    *,
    idempotency_key: Optional[str],
    tenant_id: Optional[str],
    attempts_max: int = 3,
):
    """Enqueue an investigation and return the created :class:`Job` row.

    Raises :class:`sqlalchemy.exc.IntegrityError` when an earlier job already
    holds the idempotency key — the caller replays that job instead.
    """
    from database.repositories import create_job
    from database.session import get_async_session_local

    session_local = get_async_session_local()
    async with session_local() as session:
        job = await create_job(
            session,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            payload=incident_payload,
            attempts_max=attempts_max,
        )
        await session.commit()
        return job