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
    share a keyword are never collapsed into one row.  When ``tenant_id`` is
    supplied, matches are restricted to that tenant.
    """
    window = timedelta(minutes=max(1, time_window_minutes))

    if not affected_services:
        return None

    conditions = or_(
        *[dbm.Incident.affected_services.contains([svc]) for svc in affected_services]
    )
    if tenant_id:
        conditions = and_(conditions, dbm.Incident.tenant_id == tenant_id)
    res = await session.execute(
        select(dbm.Incident).where(conditions).order_by(dbm.Incident.created_at.desc())
    )
    candidates: Sequence[dbm.Incident] = res.scalars().all()

    now = datetime.now(timezone.utc)
    for cand in candidates:
        created = _as_utc(cand.created_at) or _as_utc(cand.started_at) or now
        still_open = (cand.status or "") not in {"resolved", "closed"}
        if not still_open and (now - created) > window:
            continue
        if not _mutually_similar(cand.title, title):
            continue
        if not (set(cand.affected_services or []) & set(affected_services)):
            continue
        return cand
    return None


async def create_incident(session: AsyncSession, *, tenant_id: Optional[str] = None, **kwargs: Any) -> dbm.Incident:
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
        await update_incident(session, existing_incident.id, **kwargs)
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
    inc = await session.get(dbm.Incident, incident_id)
    if inc is None or (tenant_id and inc.tenant_id != tenant_id):
        return None
    return inc


async def get_incident_by_idempotency_key(
    session: AsyncSession,
    idempotency_key: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.Incident]:
    """Look up an incident previously created under the same idempotency key."""
    res = await session.execute(
        select(dbm.Incident).where(
            and_(
                dbm.Incident.idempotency_key == idempotency_key,
                dbm.Incident.tenant_id == tenant_id if tenant_id else True,
            )
        ).order_by(dbm.Incident.created_at.desc())
    )
    return res.scalars().first()


async def list_incidents(
    session: AsyncSession,
    limit: int = 50,
    *,
    tenant_id: Optional[str] = None,
) -> Sequence[dbm.Incident]:
    stmt = select(dbm.Incident)
    if tenant_id:
        stmt = stmt.where(dbm.Incident.tenant_id == tenant_id)
    res = await session.execute(stmt.order_by(dbm.Incident.created_at.desc()).limit(limit))
    return res.scalars().all()


async def update_incident(session: AsyncSession, incident_id: str, **fields: Any) -> None:
    await session.execute(
        update(dbm.Incident).where(dbm.Incident.id == incident_id).values(**fields)
    )


# ---------- Findings ----------

async def add_finding(session: AsyncSession, **kwargs: Any) -> dbm.Finding:
    f = dbm.Finding(**kwargs)
    session.add(f)
    await session.flush()
    return f


async def list_findings(session: AsyncSession, incident_id: str) -> Sequence[dbm.Finding]:
    res = await session.execute(
        select(dbm.Finding)
        .where(dbm.Finding.incident_id == incident_id)
        .order_by(dbm.Finding.created_at)
    )
    return res.scalars().all()


# ---------- RootCause ----------

async def save_root_cause(session: AsyncSession, **kwargs: Any) -> dbm.RootCause:
    rc = dbm.RootCause(**kwargs)
    session.add(rc)
    await session.flush()
    return rc


async def get_root_cause(session: AsyncSession, incident_id: str) -> Optional[dbm.RootCause]:
    res = await session.execute(
        select(dbm.RootCause).where(dbm.RootCause.incident_id == incident_id)
    )
    return res.scalars().first()


# ---------- Resolution ----------

async def save_resolution(session: AsyncSession, **kwargs: Any) -> dbm.Resolution:
    r = dbm.Resolution(**kwargs)
    session.add(r)
    await session.flush()
    return r


async def get_resolution(session: AsyncSession, incident_id: str) -> Optional[dbm.Resolution]:
    res = await session.execute(
        select(dbm.Resolution).where(dbm.Resolution.incident_id == incident_id)
    )
    return res.scalars().first()


# ---------- Lessons ----------

async def add_lesson(session: AsyncSession, **kwargs: Any) -> dbm.LessonLearned:
    lesson = dbm.LessonLearned(**kwargs)
    session.add(lesson)
    await session.flush()
    return lesson


# ---------- Patterns ----------

async def create_pattern(session: AsyncSession, **kwargs: Any) -> dbm.Pattern:
    p = dbm.Pattern(**kwargs)
    session.add(p)
    await session.flush()
    return p


async def get_pattern_by_signature(
    session: AsyncSession, signature: str
) -> Optional[dbm.Pattern]:
    res = await session.execute(
        select(dbm.Pattern).where(dbm.Pattern.pattern_signature == signature)
    )
    return res.scalars().first()


async def list_approved_patterns(session: AsyncSession) -> Sequence[dbm.Pattern]:
    res = await session.execute(
        select(dbm.Pattern).where(dbm.Pattern.approved.is_(True))
    )
    return res.scalars().all()


async def list_pending_patterns(session: AsyncSession) -> Sequence[dbm.Pattern]:
    res = await session.execute(
        select(dbm.Pattern).where(dbm.Pattern.approved.is_(False))
    )
    return res.scalars().all()


async def approve_pattern(
    session: AsyncSession, pattern_id: str, approver: str
) -> bool:
    res = await session.execute(
        update(dbm.Pattern)
        .where(dbm.Pattern.id == pattern_id)
        .values(approved=True, approved_by=approver)
    )
    await session.commit()
    return res.rowcount > 0  # type: ignore


# ---------- Incident Memory ----------

async def add_memory(session: AsyncSession, **kwargs: Any) -> dbm.IncidentMemory:
    m = dbm.IncidentMemory(**kwargs)
    session.add(m)
    await session.flush()
    return m


async def list_memory(session: AsyncSession, limit: int = 1000) -> Sequence[dbm.IncidentMemory]:
    res = await session.execute(
        select(dbm.IncidentMemory).order_by(dbm.IncidentMemory.created_at.desc()).limit(limit)
    )
    return res.scalars().all()


# ---------- Agent Reliability ----------

async def get_agent_reliability(
    session: AsyncSession, agent_name: str
) -> Optional[dbm.AgentReliability]:
    res = await session.execute(
        select(dbm.AgentReliability).where(dbm.AgentReliability.agent_name == agent_name)
    )
    return res.scalars().first()


async def upsert_agent_reliability(
    session: AsyncSession, agent_name: str, **fields: Any
) -> dbm.AgentReliability:
    ar = await get_agent_reliability(session, agent_name)
    if ar is None:
        ar = dbm.AgentReliability(id=agent_name, agent_name=agent_name, **fields)
        session.add(ar)
    else:
        for k, v in fields.items():
            setattr(ar, k, v)
    await session.flush()
    return ar


async def list_agent_reliabilities(session: AsyncSession) -> Sequence[dbm.AgentReliability]:
    res = await session.execute(select(dbm.AgentReliability))
    return res.scalars().all()


# ---------- Predictions ----------

async def save_prediction(session: AsyncSession, **kwargs: Any) -> dbm.Prediction:
    # Check for existing prediction for the same service and predicted_failure_type
    existing_prediction = await session.execute(
        select(dbm.Prediction)
        .where(
            dbm.Prediction.service == kwargs["service"],
            dbm.Prediction.predicted_failure_type == kwargs["predicted_failure_type"],
        )
    )
    existing_prediction = existing_prediction.scalars().first()

    if existing_prediction:
        # Update existing prediction
        for key, value in kwargs.items():
            setattr(existing_prediction, key, value)
        await session.flush()
        return existing_prediction
    else:
        # Create new prediction
        p = dbm.Prediction(**kwargs)
        session.add(p)
        await session.flush()
        return p


async def list_predictions(session: AsyncSession, limit: int = 50) -> Sequence[dbm.Prediction]:
    res = await session.execute(
        select(dbm.Prediction).order_by(dbm.Prediction.updated_at.desc()).limit(limit)
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
    res = await session.execute(
        select(dbm.InvestigationJob).where(dbm.InvestigationJob.id == job_id)
    )
    job = res.scalars().first()
    if job is None or (tenant_id and job.tenant_id != tenant_id):
        return None
    return job


async def get_job_by_idempotency_key(
    session: AsyncSession,
    key: str,
    *,
    tenant_id: Optional[str] = None,
) -> Optional[dbm.InvestigationJob]:
    res = await session.execute(
        select(dbm.InvestigationJob).where(dbm.InvestigationJob.idempotency_key == key)
    )
    job = res.scalars().first()
    if job is None or (tenant_id and job.tenant_id != tenant_id):
        return None
    return job


async def list_jobs(
    session: AsyncSession,
    limit: int = 50,
    *,
    tenant_id: Optional[str] = None,
) -> Sequence[dbm.InvestigationJob]:
    stmt = select(dbm.InvestigationJob)
    if tenant_id:
        stmt = stmt.where(dbm.InvestigationJob.tenant_id == tenant_id)
    res = await session.execute(
        stmt.order_by(dbm.InvestigationJob.created_at.desc()).limit(limit)
    )
    return res.scalars().all()


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
) -> bool:
    """Cancel a queued/running job.  Returns False if already terminal."""
    job = await get_job(session, job_id, tenant_id=tenant_id)
    if job is None:
        return False
    if job.status in {"queued", "running"}:
        job.status = "cancelled"
        await session.flush()
        return True
    return False