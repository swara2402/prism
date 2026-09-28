"""
learning.pattern_generator
==========================

Self-Learning Pattern Generator.

After every solved incident this engine extracts normalized log
patterns and links them to the confirmed root cause.  Patterns are
stored in the ``patterns`` PostgreSQL table with ``approved=False``
by default.  After an engineer approves a pattern (via the API),
future investigations can short-circuit directly to the linked root
cause whenever the pattern recurs.
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
    """
    Convert a raw log line into a stable *signature*.

    The signature replaces:

    * numbers (incl. IPs, ports, hex, percentages) with ``<N>``
    * UUIDs with ``<UUID>``
    * ISO timestamps with ``<TS>``

    So that two log lines differing only in their dynamic tokens
    collapse to the same signature.
    """
    sig = normalize_log_line(line)
    sig = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<IP>", sig)
    sig = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<UUID>", sig)
    sig = re.sub(r"\b[0-9a-fA-F]{16,}\b", "<HEX>", sig)
    sig = re.sub(r"\b\d+\b", "<N>", sig)
    sig = re.sub(r"\s+", " ", sig).strip()
    return sig


def extract_patterns(
    logs: Sequence[str],
    root_cause: str,
    confidence: float,
    severity_threshold: float = 0.5,
    max_patterns: int = 20,
) -> List[GeneratedPattern]:
    """
    Extract learnable patterns from ``logs``.

    Returns a list of :class:`GeneratedPattern`.
    """
    from utils.text import classify_log_severity

    seen: Dict[str, GeneratedPattern] = {}
    for line in logs:
        if not line or not line.strip():
            continue
        _, sev = classify_log_severity(line)
        if sev < severity_threshold:
            continue
        sig = _extract_signature(line)
        if not sig:
            continue
        if sig in seen:
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


async def persist_patterns(
    incident_id: str,
    patterns: Sequence[GeneratedPattern],
) -> List[str]:
    """Persist patterns to the DB.  Returns the created pattern IDs."""
    from database.session import AsyncSessionLocal

    created: List[str] = []
    async with AsyncSessionLocal() as session:
        for p in patterns:
            existing = await get_pattern_by_signature(session, p.pattern_signature)
            if existing is not None:
                # Bump occurrence_count
                existing.occurrence_count = (existing.occurrence_count or 1) + 1
                existing.incident_ids = list(set((existing.incident_ids or []) + [incident_id]))
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
            )
            created.append(rec.id)
        await session.commit()
    return created


async def approve_pattern_by_id(pattern_id: str, approver: str) -> bool:
    """Mark a pattern as approved by ``approver``."""
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        ok = await approve_pattern(session, pattern_id, approver)
        await session.commit()
        return ok


async def match_approved_patterns(logs: Sequence[str]) -> List[Dict[str, Any]]:
    """
    Match ``logs`` against approved patterns.

    Returns a list of dicts with keys ``pattern_id``, ``root_cause_hint``,
    ``confidence``, ``matched_signature``.
    """
    from database.session import AsyncSessionLocal

    approved = []
    async with AsyncSessionLocal() as session:
        approved = await list_approved_patterns(session)

    sig_to_pattern: Dict[str, Any] = {p.pattern_signature: p for p in approved}
    matches: List[Dict[str, Any]] = []
    for line in logs:
        sig = hash_text(_extract_signature(line))
        if sig in sig_to_pattern:
            p = sig_to_pattern[sig]
            matches.append(
                {
                    "pattern_id": p.id,
                    "root_cause_hint": p.root_cause_hint,
                    "confidence": p.confidence,
                    "matched_signature": p.pattern_signature,
                }
            )
    return matches
