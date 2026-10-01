"""
database.repositories
=====================

Async data-access helpers wrapping common CRUD operations for each
ORM model.  Keeping SQL isolated here lets agents and engines stay
focused on business logic.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from database import models as dbm


# ---------- Incidents ----------

def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a stored timestamp to tz-aware UTC for in-process comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require_tenant(tenant_id: Optional[str], caller: str) -> str:
    """Refuse to run an unscoped query.

    A missing tenant must never be interpreted as "no filter": that turns an
    omitted header (or a missed call site) into a cross-tenant read or write.
    """
    if not tenant_id:
        raise ValueError(f"{caller} requires an explicit tenant_id; refusing an unscoped query")
    return tenant_id


def _title_tokens(title: str) -> set[str]:
    """Significant lowercase alphanumeric tokens (>=3 chars) of a title."""
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(t) >= 3}


def _mutually_similar(title_a: str, title_b: str) -> bool:
    """
    True when two incident titles actually describe the same event.

    Dedup must never *merge* two genuinely different incidents just because
    they share a keyword (e.g. two unrelated "oom" alerts).  Both titles must
    share a meaningful fraction of their tokens; the classic
    ``"oom" <-> "out of memory"`` pairing is treated as one only when BOTH
    titles contain it.
    """
    la, lb = (title_a or "").lower(), (title_b or "").lower()
    oo_merge = ("oom" in la or "out of memory" in la) and ("oom" in lb or "out of memory" in lb)

    ta = _title_tokens(title_a)
    tb = _title_tokens(title_b)
    if oo_merge:
        ta.add("oom")
        tb.add("oom")
    if not ta or not tb:
        return False
    shared = ta & tb
    if not shared:
        return False
    return len(shared) / len(ta | tb) >= 0.4


async def get_similar_incident(
    session: AsyncSession,
    title: str,
    affected_services: List[str],
    started_at: Optional[datetime],
    time_window_minutes: int = 5,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.Incident]:
    """
    Find a *genuinely* similar incident to merge with: overlapping title,
    at least one shared affected service, and recent (or still open).

    Service overlap is prefiltered in SQL; title similarity is decided in
    Python via :func:`_mutually_similar` so two distinct incidents that merely
    share a keyword are never collapsed into one row.  Matches are always
    restricted to ``tenant_id`` -- there is no unscoped mode.
    """
    window = timedelta(minutes=max(1, time_window_minutes))
    tenant_id = _require_tenant(tenant_id, "get_similar_incident")

    if not affected_services:
        return None

    # affected_services is a dialect-aware JSON/JSONB TypeDecorator.
    # Its generic SQLAlchemy comparator implements contains as string LIKE,
    # which PostgreSQL rejects when the bound value is JSONB. Keep the
    # service-overlap check in Python after a bounded, tenant-scoped query.
    # This preserves correctness on both PostgreSQL and SQLite without relying
    # on a dialect-specific comparator.
    res = await session.execute(
        select(dbm.Incident)
        .where(dbm.Incident.tenant_id == tenant_id)
        .order_by(dbm.Incident.created_at.desc())
        .limit(500)
    )
    candidates: Sequence[dbm.Incident] = res.scalars().all()

    now = datetime.now(timezone.utc)
    for cand in candidates:
        created = _as_utc(cand.created_at) or _as_utc(cand.started_at) or now
        # The dedup window closes once an incident has been analyzed: without
        # this an incident is mergeable forever, so every repeat run collides
        # on the same row.
        still_open = (cand.status or "") not in {"analyzed", "resolved", "closed"}
        if not still_open and (now - created) > window:
            continue
        if not _mutually_similar(cand.title, title):
            continue
        if not (set(cand.affected_services or []) & set(affected_services)):
            continue
        return cand
    return None


async def create_incident(session: AsyncSession, *, tenant_id: Optional[str] = None, **kwargs: Any) -> dbm.Incident:
    tenant_id = _require_tenant(tenant_id, "create_incident")
    # Deduplication check
    existing_incident = await get_similar_incident(
        session,
        title=kwargs["title"],
        affected_services=kwargs["affected_services"],
        started_at=kwargs.get("started_at"),
        tenant_id=tenant_id,
    )
    if existing_incident:
        # If a similar incident exists, update it instead of creating a new one
        await update_incident(session, existing_incident.id, tenant_id=tenant_id, **kwargs)
        await session.refresh(existing_incident)
        return existing_incident

    incident = dbm.Incident(tenant_id=tenant_id, **kwargs)
    session.add(incident)
    await session.flush()
    return incident


async def get_incident(
    session: AsyncSession,
    incident_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.Incident]:
    tenant_id = _require_tenant(tenant_id, "get_incident")
    inc = await session.get(dbm.Incident, incident_id)
    if inc is None or inc.tenant_id != tenant_id:
        return None
    return inc


async def get_incident_by_idempotency_key(
    session: AsyncSession,
    idempotency_key: Optional[str],
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.Incident]:
    """Look up an incident previously created under the same idempotency key.

    Scoped to the caller's tenant: an unscoped lookup would let a client that
    reuses an ``Idempotency-Key`` (a normal retry pattern) read another
    tenant's stored investigation result.
    """
    if not idempotency_key:
        return None
    tenant_id = _require_tenant(tenant_id, "get_incident_by_idempotency_key")
    res = await session.execute(
        select(dbm.Incident)
        .where(
            dbm.Incident.idempotency_key == idempotency_key,
            dbm.Incident.tenant_id == tenant_id,
        )
        .order_by(dbm.Incident.created_at.desc())
    )
    return res.scalars().first()


async def list_incidents(
    session: AsyncSession,
    limit: int = 50,
    *,
    tenant_id: Optional[str] = None,
) -> Sequence[dbm.Incident]:
    tenant_id = _require_tenant(tenant_id, "list_incidents")
    stmt = select(dbm.Incident).where(dbm.Incident.tenant_id == tenant_id)
    res = await session.execute(stmt.order_by(dbm.Incident.created_at.desc()).limit(limit))
    return res.scalars().all()


async def get_incidents_by_ids(
    session: AsyncSession,
    incident_ids: Sequence[str],
    *,
    tenant_id: Optional[str] = None,
) -> Sequence[dbm.Incident]:
    """Batch fetch incidents for one tenant (avoids N+1 in list responses)."""
    ids = [i for i in dict.fromkeys(incident_ids) if i]
    if not ids:
        return []
    tenant_id = _require_tenant(tenant_id, "get_incidents_by_ids")
    res = await session.execute(
        select(dbm.Incident).where(dbm.Incident.id.in_(ids), dbm.Incident.tenant_id == tenant_id)
    )
    return res.scalars().all()


async def update_incident(
    session: AsyncSession,
    incident_id: str,
    *,
    tenant_id: Optional[str] = None,
    **fields: Any,
) -> None:
    """Update an incident row, scoped to the caller's tenant.

    Without the tenant predicate this write is reachable with any incident id,
    letting a caller in tenant A mutate tenant B's incident.
    """
    tenant_id = _require_tenant(tenant_id, "update_incident")
    if "tenant_id" in fields and fields["tenant_id"] != tenant_id:
        raise ValueError("update_incident cannot move an incident across tenants")
    await session.execute(
        update(dbm.Incident)
        .where(dbm.Incident.id == incident_id, dbm.Incident.tenant_id == tenant_id)
        .values(**fields)
    )


# ---------- Findings ----------

async def add_finding(
    session: AsyncSession,
    *,
    incident_id: str,
    **kwargs: Any,
) -> dbm.Finding:
    """Insert a finding, inheriting ``tenant_id`` from its parent incident.

    The tenant is read back from the ``incidents`` row rather than taken from a
    caller argument or an ambient ContextVar. The orchestrator persists findings
    from background job workers, where the request-scoped tenant ContextVar is
    never set, so any of the other three sources would silently write NULL (or
    the wrong tenant) exactly where isolation matters most.
    """
    res = await session.execute(
        select(dbm.Incident.tenant_id).where(dbm.Incident.id == incident_id)
    )
    tenant_id = res.scalar_one_or_none()
    if tenant_id is None:
        raise ValueError(f"add_finding: unknown incident {incident_id!r}")

    f = dbm.Finding(incident_id=incident_id, tenant_id=tenant_id, **kwargs)
    session.add(f)
    await session.flush()
    return f


async def list_findings(
    session: AsyncSession,
    incident_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Sequence[dbm.Finding]:
    """List findings for an incident, scoped to the caller's tenant."""
    tenant_id = _require_tenant(tenant_id, "list_findings")
    res = await session.execute(
        select(dbm.Finding)
        .join(dbm.Incident, dbm.Incident.id == dbm.Finding.incident_id)
        .where(
            dbm.Finding.incident_id == incident_id,
            dbm.Incident.tenant_id == tenant_id,
        )
        .order_by(dbm.Finding.created_at)
    )
    return res.scalars().all()


# ---------- RootCause ----------

async def upsert_root_cause(
    session: AsyncSession,
    *,
    tenant_id: Optional[str] = None,
    **kwargs: Any,
) -> dbm.RootCause:
    """Insert or replace the root cause for an incident.

    ``RootCause.incident_id`` is unique, and ``create_incident`` deliberately
    merges a deduplicated request back into the existing incident row. A plain
    ``save_root_cause`` therefore raised ``IntegrityError`` on every repeat run
    against the same incident, surfacing as a 500. Upsert is the only correct
    behaviour here.
    """
    incident_id = kwargs["incident_id"]
    tenant_id = _require_tenant(tenant_id, "upsert_root_cause")
    res = await session.execute(
        select(dbm.RootCause).where(
            dbm.RootCause.incident_id == incident_id,
            dbm.RootCause.tenant_id == tenant_id,
        )
    )
    rc = res.scalars().first()
    if rc is None:
        rc = dbm.RootCause(tenant_id=tenant_id, **kwargs)
        session.add(rc)
    else:
        for key, value in kwargs.items():
            setattr(rc, key, value)
    await session.flush()
    return rc


async def save_root_cause(
    session: AsyncSession,
    *,
    tenant_id: Optional[str] = None,
    **kwargs: Any,
) -> dbm.RootCause:
    return await upsert_root_cause(session, tenant_id=tenant_id, **kwargs)


async def get_root_cause(
    session: AsyncSession,
    incident_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.RootCause]:
    tenant_id = _require_tenant(tenant_id, "get_root_cause")
    res = await session.execute(
        select(dbm.RootCause).where(
            dbm.RootCause.incident_id == incident_id,
            dbm.RootCause.tenant_id == tenant_id,
        )
    )
    return res.scalars().first()


# ---------- Resolution ----------

async def save_resolution(
    session: AsyncSession,
    *,
    tenant_id: Optional[str] = None,
    **kwargs: Any,
) -> dbm.Resolution:
    tenant_id = _require_tenant(tenant_id, "save_resolution")
    r = dbm.Resolution(tenant_id=tenant_id, **kwargs)
    session.add(r)
    await session.flush()
    return r


async def get_resolution(
    session: AsyncSession,
    incident_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.Resolution]:
    tenant_id = _require_tenant(tenant_id, "get_resolution")
    res = await session.execute(
        select(dbm.Resolution).where(
            dbm.Resolution.incident_id == incident_id,
            dbm.Resolution.tenant_id == tenant_id,
        )
    )
    return res.scalars().first()


# ---------- Lessons ----------

async def add_lesson(session: AsyncSession, *, tenant_id: Optional[str] = None, **kwargs: Any) -> dbm.LessonLearned:
    tenant_id = _require_tenant(tenant_id, "add_lesson")
    lesson = dbm.LessonLearned(tenant_id=tenant_id, **kwargs)
    session.add(lesson)
    await session.flush()
    return lesson


# ---------- Patterns ----------

async def create_pattern(session: AsyncSession, *, tenant_id: Optional[str] = None, **kwargs: Any) -> dbm.Pattern:
    tenant_id = _require_tenant(tenant_id, "create_pattern")
    p = dbm.Pattern(tenant_id=tenant_id, **kwargs)
    session.add(p)
    await session.flush()
    return p


async def get_pattern_by_signature(
    session: AsyncSession, signature: str, *, tenant_id: Optional[str] = None
) -> Optional[dbm.Pattern]:
    tenant_id = _require_tenant(tenant_id, "get_pattern_by_signature")
    res = await session.execute(
        select(dbm.Pattern).where(
            dbm.Pattern.pattern_signature == signature,
            dbm.Pattern.tenant_id == tenant_id,
        )
    )
    return res.scalars().first()


async def list_approved_patterns(
    session: AsyncSession, *, tenant_id: Optional[str] = None, limit: int = 500
) -> Sequence[dbm.Pattern]:
    """Approved patterns, optionally restricted to one tenant.

    Bounded by ``limit``: this feeds plain ``GET`` endpoints, and an
    unbounded read amplification on a cheap route is a DoS primitive.
    """
    stmt = select(dbm.Pattern).where(dbm.Pattern.approved.is_(True))
    if tenant_id is not None:
        stmt = stmt.where(dbm.Pattern.tenant_id == tenant_id)
    res = await session.execute(stmt.limit(limit))
    return res.scalars().all()


async def list_pending_patterns(
    session: AsyncSession, *, tenant_id: Optional[str] = None, limit: int = 500
) -> Sequence[dbm.Pattern]:
    stmt = select(dbm.Pattern).where(dbm.Pattern.approved.is_(False))
    if tenant_id is not None:
        stmt = stmt.where(dbm.Pattern.tenant_id == tenant_id)
    res = await session.execute(stmt.limit(limit))
    return res.scalars().all()


async def approve_pattern(
    session: AsyncSession, pattern_id: str, approver: str, *, tenant_id: Optional[str] = None
) -> bool:
    tenant_id = _require_tenant(tenant_id, "approve_pattern")
    res = await session.execute(
        update(dbm.Pattern)
        .where(dbm.Pattern.id == pattern_id, dbm.Pattern.tenant_id == tenant_id)
        .values(approved=True, approved_by=approver)
    )
    await session.commit()
    return res.rowcount > 0  # type: ignore


# ---------- Incident Memory ----------

async def add_memory(session: AsyncSession, *, tenant_id: Optional[str] = None, **kwargs: Any) -> dbm.IncidentMemory:
    tenant_id = _require_tenant(tenant_id, "add_memory")
    m = dbm.IncidentMemory(tenant_id=tenant_id, **kwargs)
    session.add(m)
    await session.flush()
    return m


async def list_memory(
    session: AsyncSession, limit: int = 1000, *, tenant_id: Optional[str] = None
) -> Sequence[dbm.IncidentMemory]:
    tenant_id = _require_tenant(tenant_id, "list_memory")
    stmt = select(dbm.IncidentMemory).where(dbm.IncidentMemory.tenant_id == tenant_id)
    res = await session.execute(
        stmt.order_by(dbm.IncidentMemory.created_at.desc()).limit(limit)
    )
    return res.scalars().all()


# ---------- Agent Reliability ----------

async def get_agent_reliability(
    session: AsyncSession, agent_name: str, *, tenant_id: Optional[str] = None
) -> Optional[dbm.AgentReliability]:
    tenant_id = _require_tenant(tenant_id, "get_agent_reliability")
    stmt = select(dbm.AgentReliability).where(
        dbm.AgentReliability.agent_name == agent_name,
        dbm.AgentReliability.tenant_id == tenant_id,
    )
    res = await session.execute(stmt)
    return res.scalars().first()


async def upsert_agent_reliability(
    session: AsyncSession, agent_name: str, **fields: Any
) -> dbm.AgentReliability:
    tenant_id = fields.get("tenant_id")
    ar = await get_agent_reliability(session, agent_name, tenant_id=tenant_id)
    if ar is None:
        ar = dbm.AgentReliability(agent_name=agent_name, **fields)
        session.add(ar)
    else:
        for k, v in fields.items():
            setattr(ar, k, v)
    await session.flush()
    return ar


async def list_agent_reliabilities(
    session: AsyncSession, *, tenant_id: Optional[str] = None
) -> Sequence[dbm.AgentReliability]:
    tenant_id = _require_tenant(tenant_id, "list_agent_reliabilities")
    stmt = select(dbm.AgentReliability).where(dbm.AgentReliability.tenant_id == tenant_id)
    res = await session.execute(stmt)
    return res.scalars().all()


# ---------- Predictions ----------

async def save_prediction(session: AsyncSession, **kwargs: Any) -> dbm.Prediction:
    # Check for existing prediction for the same tenant, service and failure type
    tenant_id = _require_tenant(kwargs.get("tenant_id"), "save_prediction")
    existing_res = await session.execute(
        select(dbm.Prediction).where(
            dbm.Prediction.tenant_id == tenant_id,
            dbm.Prediction.service == kwargs["service"],
            dbm.Prediction.predicted_failure_type == kwargs["predicted_failure_type"],
        )
    )
    existing_prediction = existing_res.scalars().first()

    if existing_prediction:
        # Update existing prediction
        for key, value in kwargs.items():
            setattr(existing_prediction, key, value)
        await session.flush()
        return existing_prediction
    # Create new prediction
    p = dbm.Prediction(**kwargs)
    session.add(p)
    await session.flush()
    return p


async def list_predictions(
    session: AsyncSession, limit: int = 50, *, tenant_id: Optional[str] = None
) -> Sequence[dbm.Prediction]:
    tenant_id = _require_tenant(tenant_id, "list_predictions")
    res = await session.execute(
        select(dbm.Prediction)
        .where(dbm.Prediction.tenant_id == tenant_id)
        .order_by(dbm.Prediction.updated_at.desc())
        .limit(limit)
    )
    return res.scalars().all()


# ---------- Meta-reasoning ----------

async def save_meta_reasoning(session: AsyncSession, **kwargs: Any) -> dbm.MetaReasoningRecord:
    m = dbm.MetaReasoningRecord(**kwargs)
    session.add(m)
    await session.flush()
    return m


# ---------- Investigation Jobs (background queue) ----------

async def create_job(
    session: AsyncSession,
    *,
    tenant_id: Optional[str],
    idempotency_key: Optional[str],
    payload: dict,
    attempts_max: int = 3,
) -> dbm.InvestigationJob:
    """Insert a new queued job.  Raises IntegrityError on duplicate key."""
    # Every other tenant-scoped read/write fails closed on a missing tenant;
    # this one silently accepted NULL, which would enqueue work that no tenant
    # could ever see or cancel.
    tenant_id = _require_tenant(tenant_id, "create_job")
    job = dbm.InvestigationJob(
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        payload=payload,
        status="queued",
        attempts=0,
        attempts_max=max(1, int(attempts_max)),
    )
    session.add(job)
    await session.flush()
    return job


async def get_job(
    session: AsyncSession,
    job_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.InvestigationJob]:
    tenant_id = _require_tenant(tenant_id, "get_job")
    res = await session.execute(
        select(dbm.InvestigationJob).where(dbm.InvestigationJob.id == job_id)
    )
    job = res.scalars().first()
    if job is None or job.tenant_id != tenant_id:
        return None
    return job


async def get_job_by_idempotency_key(
    session: AsyncSession,
    key: Optional[str],
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.InvestigationJob]:
    # A ``None`` key must never reach the query: it would render as ``IS NULL``
    # and match an arbitrary keyless job belonging to another tenant.
    if not key:
        return None
    tenant_id = _require_tenant(tenant_id, "get_job_by_idempotency_key")
    res = await session.execute(
        select(dbm.InvestigationJob).where(
            dbm.InvestigationJob.idempotency_key == key,
            dbm.InvestigationJob.tenant_id == tenant_id,
        )
    )
    job = res.scalars().first()
    return job


async def list_jobs(
    session: AsyncSession,
    limit: int = 50,
    *,
    tenant_id: Optional[str] = None,
) -> Sequence[dbm.InvestigationJob]:
    tenant_id = _require_tenant(tenant_id, "list_jobs")
    stmt = select(dbm.InvestigationJob).where(dbm.InvestigationJob.tenant_id == tenant_id)
    res = await session.execute(
        stmt.order_by(dbm.InvestigationJob.created_at.desc()).limit(limit)
    )
    return res.scalars().all()


async def reclaim_stale_jobs(
    session: AsyncSession, *, lease_seconds: float
) -> int:
    """Return jobs whose worker died mid-flight back to the queue.

    ``claim_next_job`` sets ``locked_at`` but nothing ever read it, so a worker
    killed between claim and completion (OOM, deploy rollout, unhandled
    ``BaseException``) left a job ``running`` forever: never retried, never
    dead-lettered, and invisible to the caller. Anything older than the lease
    is assumed abandoned.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=max(1.0, float(lease_seconds)))
    res = await session.execute(
        update(dbm.InvestigationJob)
        .where(
            dbm.InvestigationJob.status == "running",
            or_(
                dbm.InvestigationJob.locked_at.is_(None),
                dbm.InvestigationJob.locked_at <= cutoff,
            ),
        )
        .values(
            status="queued",
            locked_at=None,
            next_attempt_at=now,
            error="Worker lease expired; job requeued",
        )
    )
    await session.flush()
    return int(res.rowcount or 0)


async def claim_next_job(
    session: AsyncSession, worker_id: str
) -> Optional[dbm.InvestigationJob]:
    """Atomically claim the oldest enqueued (and backoff-elapsed) job.

    Uses ``FOR UPDATE SKIP LOCKED`` on PostgreSQL so concurrent workers never
    pick up the same row.  Other dialects (SQLite in tests / local dev) fall
    back to a plain read, which is safe for a single-process worker.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stmt = (
        select(dbm.InvestigationJob)
        .where(
            and_(
                dbm.InvestigationJob.status == "queued",
                or_(
                    dbm.InvestigationJob.next_attempt_at.is_(None),
                    dbm.InvestigationJob.next_attempt_at <= now,
                ),
            )
        )
        .order_by(dbm.InvestigationJob.created_at.asc())
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    res = await session.execute(stmt)
    job = res.scalars().first()
    if job is None:
        return None
    job.status = "running"
    job.attempts = (job.attempts or 0) + 1
    job.progress = 0.1
    job.locked_at = datetime.now(timezone.utc)
    job.next_attempt_at = None
    await session.flush()
    return job


async def update_job(
    session: AsyncSession,
    job_id: str,
    *,
    status: str,
    result_incident_id: Optional[str] = None,
    error: Optional[str] = None,
    progress: Optional[float] = None,
    next_attempt_at: Optional[Any] = None,
) -> None:
    values: dict[str, Any] = {"status": status}
    if result_incident_id is not None:
        values["result_incident_id"] = result_incident_id
    if error is not None:
        values["error"] = error
    if progress is not None:
        values["progress"] = progress
    if next_attempt_at is not None:
        values["next_attempt_at"] = next_attempt_at
    await session.execute(
        update(dbm.InvestigationJob).where(dbm.InvestigationJob.id == job_id).values(**values)
    )


async def cancel_job(
    session: AsyncSession,
    job_id: str,
    *,
    tenant_id: Optional[str] = None,
) -> tuple[bool, Optional[dbm.InvestigationJob]]:
    """Cancel a queued/running job.

    Returns ``(cancelled, job)``. ``cancelled`` is True only when this call
    performed the transition, so the caller can tell a real cancel from a no-op
    instead of reporting success for an already-finished job. ``job`` is
    returned in every case where the row exists, so the caller can report the
    true current status.
    """
    job = await get_job(session, job_id, tenant_id=tenant_id)
    if job is None:
        return False, None
    if job.status in {"queued", "running"}:
        job.status = "cancelled"
        job.locked_at = None
        await session.flush()
        return True, job
    return False, job
