"""Tenant-safe async data-access helpers for WayPoint."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence

from sqlalchemy import and_, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from auth.tenant_context import get_tenant
from config.settings import settings
from database import models as dbm


def _effective_tenant(tenant_id: Optional[str]) -> Optional[str]:
    value = (tenant_id or get_tenant() or "").strip()
    if value:
        return value
    return "test-tenant" if settings.is_test else None


def _require_tenant(tenant_id: Optional[str]) -> str:
    value = _effective_tenant(tenant_id)
    if not value:
        raise RuntimeError("Tenant context is required for tenant-scoped repository access")
    return value


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _title_tokens(title: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(t) >= 3}


def _mutually_similar(title_a: str, title_b: str) -> bool:
    la, lb = (title_a or "").lower(), (title_b or "").lower()
    oo_merge = ("oom" in la or "out of memory" in la) and ("oom" in lb or "out of memory" in lb)
    ta, tb = _title_tokens(title_a), _title_tokens(title_b)
    if oo_merge:
        ta.add("oom")
        tb.add("oom")
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.4


async def get_similar_incident(session: AsyncSession, title: str, affected_services: List[str], started_at: Optional[datetime], time_window_minutes: int = 5, *, tenant_id: Optional[str] = None) -> Optional[dbm.Incident]:
    tenant = _require_tenant(tenant_id)
    if not affected_services:
        return None
    window = timedelta(minutes=max(1, time_window_minutes))
    conditions = and_(dbm.Incident.tenant_id == tenant, or_(*[dbm.Incident.affected_services.contains([svc]) for svc in affected_services]))
    res = await session.execute(select(dbm.Incident).where(conditions).order_by(dbm.Incident.created_at.desc()))
    candidates: Sequence[dbm.Incident] = res.scalars().all()
    now = datetime.now(timezone.utc)
    for cand in candidates:
        created = _as_utc(cand.created_at) or _as_utc(cand.started_at) or now
        if (cand.status or "") in {"resolved", "closed"} and (now - created) > window:
            continue
        if _mutually_similar(cand.title, title) and (set(cand.affected_services or []) & set(affected_services)):
            return cand
    return None


async def create_incident(session: AsyncSession, *, tenant_id: Optional[str] = None, **kwargs: Any) -> dbm.Incident:
    tenant = _require_tenant(tenant_id)
    existing = await get_similar_incident(session, title=kwargs["title"], affected_services=kwargs["affected_services"], started_at=kwargs.get("started_at"), tenant_id=tenant)
    if existing:
        await update_incident(session, existing.id, tenant_id=tenant, **kwargs)
        await session.refresh(existing)
        return existing
    incident = dbm.Incident(tenant_id=tenant, **kwargs)
    session.add(incident)
    await session.flush()
    return incident


async def get_incident(session: AsyncSession, incident_id: str, *, tenant_id: Optional[str] = None) -> Optional[dbm.Incident]:
    tenant = _require_tenant(tenant_id)
    res = await session.execute(select(dbm.Incident).where(dbm.Incident.id == incident_id, dbm.Incident.tenant_id == tenant))
    return res.scalars().first()


async def get_incident_by_idempotency_key(session: AsyncSession, idempotency_key: str, *, tenant_id: Optional[str] = None) -> Optional[dbm.Incident]:
    tenant = _require_tenant(tenant_id)
    res = await session.execute(select(dbm.Incident).where(dbm.Incident.idempotency_key == idempotency_key, dbm.Incident.tenant_id == tenant).order_by(dbm.Incident.created_at.desc()))
    return res.scalars().first()


async def list_incidents(session: AsyncSession, limit: int = 50, *, tenant_id: Optional[str] = None) -> Sequence[dbm.Incident]:
    tenant = _require_tenant(tenant_id)
    res = await session.execute(select(dbm.Incident).where(dbm.Incident.tenant_id == tenant).order_by(dbm.Incident.created_at.desc()).limit(limit))
    return res.scalars().all()


async def update_incident(session: AsyncSession, incident_id: str, *, tenant_id: Optional[str] = None, **fields: Any) -> bool:
    tenant = _require_tenant(tenant_id)
    fields.pop("tenant_id", None)
    result = await session.execute(update(dbm.Incident).where(dbm.Incident.id == incident_id, dbm.Incident.tenant_id == tenant).values(**fields))
    return bool(result.rowcount)


async def add_finding(session: AsyncSession, **kwargs: Any) -> dbm.Finding:
    f = dbm.Finding(**kwargs)
    session.add(f)
    await session.flush()
    return f


async def list_findings(session: AsyncSession, incident_id: str, *, tenant_id: Optional[str] = None) -> Sequence[dbm.Finding]:
    tenant = _require_tenant(tenant_id)
    res = await session.execute(select(dbm.Finding).join(dbm.Incident, dbm.Incident.id == dbm.Finding.incident_id).where(dbm.Finding.incident_id == incident_id, dbm.Incident.tenant_id == tenant).order_by(dbm.Finding.created_at))
    return res.scalars().all()


async def save_root_cause(session: AsyncSession, **kwargs: Any) -> dbm.RootCause:
    tenant = _require_tenant(kwargs.pop("tenant_id", None))
    incident_id = kwargs.get("incident_id")
    if not incident_id or await get_incident(session, incident_id, tenant_id=tenant) is None:
        raise LookupError("Incident does not belong to the authenticated tenant")
    rc = dbm.RootCause(**kwargs)
    session.add(rc)
    await session.flush()
    return rc


async def get_root_cause(session: AsyncSession, incident_id: str, *, tenant_id: Optional[str] = None) -> Optional[dbm.RootCause]:
    tenant = _require_tenant(tenant_id)
    res = await session.execute(select(dbm.RootCause).join(dbm.Incident, dbm.Incident.id == dbm.RootCause.incident_id).where(dbm.RootCause.incident_id == incident_id, dbm.Incident.tenant_id == tenant))
    return res.scalars().first()


async def save_resolution(session: AsyncSession, **kwargs: Any) -> dbm.Resolution:
    tenant = _require_tenant(kwargs.pop("tenant_id", None))
    incident_id = kwargs.get("incident_id")
    if not incident_id or await get_incident(session, incident_id, tenant_id=tenant) is None:
        raise LookupError("Incident does not belong to the authenticated tenant")
    r = dbm.Resolution(**kwargs)
    session.add(r)
    await session.flush()
    return r


async def get_resolution(session: AsyncSession, incident_id: str, *, tenant_id: Optional[str] = None) -> Optional[dbm.Resolution]:
    tenant = _require_tenant(tenant_id)
    res = await session.execute(select(dbm.Resolution).join(dbm.Incident, dbm.Incident.id == dbm.Resolution.incident_id).where(dbm.Resolution.incident_id == incident_id, dbm.Incident.tenant_id == tenant))
    return res.scalars().first()


async def add_lesson(session: AsyncSession, **kwargs: Any) -> dbm.LessonLearned:
    lesson = dbm.LessonLearned(**kwargs)
    session.add(lesson)
    await session.flush()
    return lesson


async def create_pattern(session: AsyncSession, **kwargs: Any) -> dbm.Pattern:
    p = dbm.Pattern(**kwargs)
    session.add(p)
    await session.flush()
    return p


async def get_pattern_by_signature(session: AsyncSession, signature: str, *, tenant_id: Optional[str] = None) -> Optional[dbm.Pattern]:
    stmt = select(dbm.Pattern).where(dbm.Pattern.pattern_signature == signature)
    if tenant_id and hasattr(dbm.Pattern, "tenant_id"):
        stmt = stmt.where(dbm.Pattern.tenant_id == tenant_id)
    return (await session.execute(stmt)).scalars().first()


async def list_approved_patterns(session: AsyncSession, *, tenant_id: Optional[str] = None) -> Sequence[dbm.Pattern]:
    stmt = select(dbm.Pattern).where(dbm.Pattern.approved.is_(True))
    if tenant_id and hasattr(dbm.Pattern, "tenant_id"):
        stmt = stmt.where(dbm.Pattern.tenant_id == tenant_id)
    return (await session.execute(stmt)).scalars().all()


async def list_pending_patterns(session: AsyncSession, *, tenant_id: Optional[str] = None) -> Sequence[dbm.Pattern]:
    stmt = select(dbm.Pattern).where(dbm.Pattern.approved.is_(False))
    if tenant_id and hasattr(dbm.Pattern, "tenant_id"):
        stmt = stmt.where(dbm.Pattern.tenant_id == tenant_id)
    return (await session.execute(stmt)).scalars().all()


async def approve_pattern(session: AsyncSession, pattern_id: str, approver: str, *, tenant_id: Optional[str] = None) -> bool:
    stmt = update(dbm.Pattern).where(dbm.Pattern.id == pattern_id)
    if tenant_id and hasattr(dbm.Pattern, "tenant_id"):
        stmt = stmt.where(dbm.Pattern.tenant_id == tenant_id)
    res = await session.execute(stmt.values(approved=True, approved_by=approver))
    await session.commit()
    return bool(res.rowcount)


async def add_memory(session: AsyncSession, **kwargs: Any) -> dbm.IncidentMemory:
    m = dbm.IncidentMemory(**kwargs)
    session.add(m)
    await session.flush()
    return m


async def list_memory(session: AsyncSession, limit: int = 1000, *, tenant_id: Optional[str] = None) -> Sequence[dbm.IncidentMemory]:
    stmt = select(dbm.IncidentMemory).order_by(dbm.IncidentMemory.created_at.desc()).limit(limit)
    if tenant_id and hasattr(dbm.IncidentMemory, "tenant_id"):
        stmt = stmt.where(dbm.IncidentMemory.tenant_id == tenant_id)
    return (await session.execute(stmt)).scalars().all()


async def get_agent_reliability(session: AsyncSession, agent_name: str) -> Optional[dbm.AgentReliability]:
    return (await session.execute(select(dbm.AgentReliability).where(dbm.AgentReliability.agent_name == agent_name))).scalars().first()


async def upsert_agent_reliability(session: AsyncSession, agent_name: str, **fields: Any) -> dbm.AgentReliability:
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
    return (await session.execute(select(dbm.AgentReliability))).scalars().all()


async def save_prediction(session: AsyncSession, **kwargs: Any) -> dbm.Prediction:
    result = await session.execute(select(dbm.Prediction).where(dbm.Prediction.service == kwargs["service"], dbm.Prediction.predicted_failure_type == kwargs["predicted_failure_type"]))
    existing = result.scalars().first()
    if existing:
        for key, value in kwargs.items():
            setattr(existing, key, value)
        await session.flush()
        return existing
    p = dbm.Prediction(**kwargs)
    session.add(p)
    await session.flush()
    return p


async def list_predictions(session: AsyncSession, limit: int = 50) -> Sequence[dbm.Prediction]:
    return (await session.execute(select(dbm.Prediction).order_by(dbm.Prediction.updated_at.desc().limit(limit))).scalars().all())


async def save_meta_reasoning(session: AsyncSession, **kwargs: Any) -> dbm.MetaReasoningRecord:
    m = dbm.MetaReasoningRecord(**kwargs)
    session.add(m)
    await session.flush()
    return m


async def create_job(session: AsyncSession, *, tenant_id: Optional[str], idempotency_key: Optional[str], payload: dict, attempts_max: int = 3) -> dbm.InvestigationJob:
    tenant = _require_tenant(tenant_id)
    job = dbm.InvestigationJob(tenant_id=tenant, idempotency_key=idempotency_key, payload=payload, status="queued", attempts=0, attempts_max=max(1, int(attempts_max)))
    session.add(job)
    await session.flush()
    return job


async def get_job(session: AsyncSession, job_id: str, *, tenant_id: Optional[str] = None) -> Optional[dbm.InvestigationJob]:
    tenant = _require_tenant(tenant_id)
    return (await session.execute(select(dbm.InvestigationJob).where(dbm.InvestigationJob.id == job_id, dbm.InvestigationJob.tenant_id == tenant))).scalars().first()


async def get_job_by_idempotency_key(session: AsyncSession, key: str, *, tenant_id: Optional[str] = None) -> Optional[dbm.InvestigationJob]:
    tenant = _require_tenant(tenant_id)
    res = await session.execute(select(dbm.InvestigationJob).where(dbm.InvestigationJob.idempotency_key == key, dbm.InvestigationJob.tenant_id == tenant))
    return res.scalars().first()


async def list_jobs(session: AsyncSession, limit: int = 50, *, tenant_id: Optional[str] = None) -> Sequence[dbm.InvestigationJob]:
    tenant = _require_tenant(tenant_id)
    return (await session.execute(select(dbm.InvestigationJob).where(dbm.InvestigationJob.tenant_id == tenant).order_by(dbm.InvestigationJob.created_at.desc().limit(limit)))).scalars().all()


async def claim_next_job(session: AsyncSession, worker_id: str) -> Optional[dbm.InvestigationJob]:
    now = datetime.now(timezone.utc)
    stmt = select(dbm.InvestigationJob).where(dbm.InvestigationJob.status == "queued", or_(dbm.InvestigationJob.next_attempt_at.is_(None), dbm.InvestigationJob.next_attempt_at <= now)).order_by(dbm.InvestigationJob.created_at.asc()).limit(1)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = (await session.execute(stmt)).scalars().first()
    if job is None:
        return None
    job.status = "running"
    job.attempts = (job.attempts or 0) + 1
    job.progress = 0.1
    job.locked_at = now
    job.next_attempt_at = None
    await session.flush()
    return job


async def update_job(session: AsyncSession, job_id: str, *, status: str, result_incident_id: Optional[str] = None, error: Optional[str] = None, progress: Optional[float] = None, next_attempt_at: Optional[Any] = None) -> None:
    values: dict[str, Any] = {"status": status, "next_attempt_at": next_attempt_at}
    if result_incident_id is not None:
        values["result_incident_id"] = result_incident_id
    if error is not None:
        values["error"] = error
    if progress is not None:
        values["progress"] = progress
    values["locked_at"] = None if status in {"queued", "completed", "failed", "cancelled"} else datetime.now(timezone.utc)
    stmt = update(dbm.InvestigationJob).where(dbm.InvestigationJob.id == job_id)
    if status != "cancelled":
        stmt = stmt.where(dbm.InvestigationJob.status.not_in({"cancelled", "completed", "failed"}))
    await session.execute(stmt.values(**values))


async def cancel_job(session: AsyncSession, job_id: str, *, tenant_id: Optional[str] = None) -> bool:
    tenant = _require_tenant(tenant_id)
    result = await session.execute(update(dbm.InvestigationJob).where(dbm.InvestigationJob.id == job_id, dbm.InvestigationJob.tenant_id == tenant, dbm.InvestigationJob.status.in_({"queued", "running"})).values(status="cancelled", locked_at=None))
    await session.flush()
    return bool(result.rowcount)
