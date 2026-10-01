"""
learning.pattern_generator
==========================

Tenant-safe self-learning pattern generator.

Patterns remain stored in the existing table for backwards compatibility.
Ownership is derived from the incident IDs attached to each pattern, so this
change does not require a destructive schema rewrite. A pattern may only be
created, approved, or matched when at least one of its source incidents belongs
to the authenticated tenant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from config.logging import get_logger
from database.repositories import (
    approve_pattern,
    create_pattern,
    get_pattern_by_signature,
    list_approved_patterns,
)
from utils.text import hash_text, normalize_log_line

logger = get_logger(__name__)


@dataclass
class GeneratedPattern:
    pattern_signature: str
    pattern_text: str
    root_cause_hint: str
    confidence: float


def _extract_signature(line: str) -> str:
    sig = normalize_log_line(line)
    sig = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<IP>", sig)
    sig = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<UUID>", sig)
    sig = re.sub(r"\b[0-9a-fA-F]{16,}\b", "<HEX>", sig)
    sig = re.sub(r"\b\d+\b", "<N>", sig)
    return re.sub(r"\s+", " ", sig).strip()


def extract_patterns(logs: Sequence[str], root_cause: str, confidence: float,
                     severity_threshold: float = 0.5, max_patterns: int = 20) -> List[GeneratedPattern]:
    from utils.text import classify_log_severity
    seen: Dict[str, GeneratedPattern] = {}
    for line in logs:
        if not line or not line.strip():
            continue
        _, sev = classify_log_severity(line)
        if sev < severity_threshold:
            continue
        sig = _extract_signature(line)
        if not sig or sig in seen:
            continue
        seen[sig] = GeneratedPattern(
            pattern_signature=hash_text(sig),
            pattern_text=sig,
            root_cause_hint=root_cause,
            confidence=min(1.0, confidence * sev),
        )
        if len(seen) >= max_patterns:
            break
    return list(seen.values())


async def _incident_belongs_to_tenant(session: Any, incident_id: str, tenant_id: str | None) -> bool:
    from database import models as dbm
    incident = await session.get(dbm.Incident, incident_id)
    return incident is not None and incident.tenant_id == tenant_id


async def persist_patterns(incident_id: str, patterns: Sequence[GeneratedPattern], *, tenant_id: str | None = None) -> List[str]:
    """Persist only patterns sourced from an incident in the active tenant."""
    from database.session import AsyncSessionLocal
    created: List[str] = []
    async with AsyncSessionLocal() as session:
        if not await _incident_belongs_to_tenant(session, incident_id, tenant_id):
            raise ValueError("Incident does not belong to the requested tenant")
        for p in patterns:
            existing = await get_pattern_by_signature(session, p.pattern_signature, tenant_id=tenant_id)
            if existing is not None:
                source_ids = set(existing.incident_ids or [])
                if not any(await _incident_belongs_to_tenant(session, iid, tenant_id) for iid in source_ids):
                    continue
                existing.occurrence_count = (existing.occurrence_count or 1) + 1
                existing.incident_ids = list(source_ids | {incident_id})
                created.append(existing.id)
                continue
            rec = await create_pattern(
                session,
                pattern_signature=p.pattern_signature,
                pattern_text=p.pattern_text,
                root_cause_hint=p.root_cause_hint,
                confidence=p.confidence,
                approved=False,
                incident_ids=[incident_id],
                tenant_id=tenant_id,
            )
            created.append(rec.id)
        await session.commit()
    return created


async def approve_pattern_by_id(pattern_id: str, approver: str, *, tenant_id: str | None = None) -> bool:
    from database.session import AsyncSessionLocal
    from database import models as dbm
    async with AsyncSessionLocal() as session:
        pattern = await session.get(dbm.Pattern, pattern_id)
        if pattern is None:
            return False
        if tenant_id is not None and not any(
            await _incident_belongs_to_tenant(session, iid, tenant_id)
            for iid in (pattern.incident_ids or [])
        ):
            return False
        return await approve_pattern(session, pattern_id, approver, tenant_id=tenant_id)


async def match_approved_patterns(logs: Sequence[str], *, tenant_id: str | None = None) -> List[Dict[str, Any]]:
    from database.session import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        # Scoped in the query. The previous version loaded every tenant's
        # approved patterns and then ran a per-pattern, per-incident existence
        # check in Python -- O(patterns x incidents) round trips, on a hot
        # path, before the first log line could be matched.
        approved = await list_approved_patterns(session, tenant_id=tenant_id)

    sig_to_pattern: Dict[str, Any] = {p.pattern_signature: p for p in approved}
    matches: List[Dict[str, Any]] = []
    for line in logs:
        sig = hash_text(_extract_signature(line))
        if sig in sig_to_pattern:
            p = sig_to_pattern[sig]
            matches.append({
                "pattern_id": p.id,
                "root_cause_hint": p.root_cause_hint,
                "confidence": p.confidence,
                "matched_signature": p.pattern_signature,
                "provenance": "approved_historical_pattern",
                "epistemic_status": "evidence",
            })
    return matches
