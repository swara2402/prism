"""
learning.continuous_learning
============================

Continuous Learning Engine.

**Authoritative trigger**: an incident must be *confirmed* before it may
update the pattern library, knowledge graph, agent reliability, or
semantic memory.  :func:`learn_from_incident` accepts a
:class:`LearningInput` whose ``root_cause`` (or ``ground_truth_root_cause``)
is the confirmed ground-truth cause and refuses to learn otherwise.

This is deliberate: PRISM's consensus is a *hypothesis*, not truth.
Learning from an unconfirmed RCA bakes wrong answers into patterns,
memory, knowledge-graph edges and agent reliability — and those wrong
answers then bias every future investigation.

Agent reliability updates additionally require an explicit
``ground_truth_source`` (``engineer_confirmed``, ``incident_postmortem``,
``external_system``, ``benchmark_label``).  Without it the engine cannot
grade agents against independently established truth, only against its own
consensus — which is circular and forbidden.

In ``LEARNING_MODE=frozen`` (see :data:`config.settings`) the entire
pipeline is a no-op so experiments stay independent and reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from config.logging import get_logger
from config.settings import settings
from database.repositories import (
    add_lesson,
    upsert_agent_reliability,
)
from knowledge_graph.store import KnowledgeGraphStore
from learning.pattern_generator import extract_patterns, persist_patterns
from memory.store import MemoryStore

logger = get_logger(__name__)

#: Provenance values that qualify as independent ground truth for agent grading.
ALLOWED_GROUND_TRUTH_SOURCES = {
    "engineer_confirmed",
    "incident_postmortem",
    "external_system",
    "benchmark_label",
}


@dataclass
class LearningInput:
    """Inputs for the continuous learning engine.

    ``root_cause`` / ``ground_truth_root_cause`` hold the **confirmed**
    root cause.  When ``settings.learning_require_confirmation`` is set
    (the default) learning is refused if no confirmed cause is present.
    """

    incident_id: str
    #: Tenant that owns the incident. Learning writes into tenant-scoped
    #: tables (patterns, agent reliability, lessons), so this is required:
    #: without it those writes cannot be scoped and are refused.
    tenant_id: Optional[str]
    root_cause: Optional[str]
    confidence: float
    affected_services: List[str]
    raw_logs: List[str]
    resolution: Optional[str]
    agents_used: List[str]
    findings: List[Dict[str, Any]]
    consensus: Dict[str, Any]
    lessons: List[str] = None  # type: ignore[assignment]
    ground_truth_root_cause: Optional[str] = None
    ground_truth_source: Optional[str] = None
    ground_truth_confidence: Optional[float] = None
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None


def confirmed_root_cause(inp: LearningInput) -> Optional[str]:
    """Return the confirmed ground-truth root cause, if one is available."""
    # A confirmed RCA is the only admissible learning label. Never fall
    # back to PRISM's own consensus/root_cause, because that would turn an
    # unverified hypothesis into training data.
    return (inp.ground_truth_root_cause or "").strip() or None


async def _update_pattern_library(
    incident_id: str, inp: LearningInput, rc: str
) -> List[str]:
    patterns = extract_patterns(
        logs=inp.raw_logs,
        root_cause=rc,
        confidence=inp.confidence,
    )
    if not patterns:
        return []
    return await persist_patterns(incident_id, patterns, tenant_id=inp.tenant_id)


async def _update_knowledge_graph(inp: LearningInput, rc: str) -> None:
    if not settings.enable_neo4j:
        logger.info("knowledge_graph_learning_skipped_disabled", extra={"incident_id": inp.incident_id})
        return
    store = KnowledgeGraphStore.get()
    await store.update_after_investigation(
        incident_id=inp.incident_id,
        affected_services=inp.affected_services,
        root_cause=rc,
        resolution=inp.resolution,
    )


async def _update_agent_reliability(inp: LearningInput, rc: str) -> bool:
    """
    Update agent reliability against **confirmed ground truth only**.

    Requires ``ground_truth_source`` to be set; without it there is no
    independent truth to grade against, so the update is skipped.  This
    breaks the old (circular) scheme that graded agents against PRISM's
    own consensus answer.
    """
    source = (inp.ground_truth_source or "").strip().lower()
    if source not in ALLOWED_GROUND_TRUTH_SOURCES:
        logger.info(
            "reliability_update_skipped_no_ground_truth_source",
            extra={
                "incident_id": inp.incident_id,
                "ground_truth_source": inp.ground_truth_source,
            },
        )
        return False

    from database.session import AsyncSessionLocal
    from utils.text import jaccard_similarity, tokenize

    final_tokens = set(tokenize(rc.lower()))

    async with AsyncSessionLocal() as session:
        for f in inp.findings:
            agent = f.get("agent_name")
            if not agent:
                continue
            hint = f.get("root_cause_hint") or ""
            conf = float(f.get("confidence", 0.0))
            hint_tokens = set(tokenize(hint.lower()))
            sim = jaccard_similarity(final_tokens, hint_tokens)

            predicted_positive = conf >= 0.4
            actually_positive = sim >= 0.3

            ar = await upsert_agent_reliability(session, agent, tenant_id=inp.tenant_id)
            ar.invocations = (ar.invocations or 0) + 1
            if predicted_positive and actually_positive:
                ar.true_positives = (ar.true_positives or 0) + 1
            elif predicted_positive and not actually_positive:
                ar.false_positives = (ar.false_positives or 0) + 1
            elif not predicted_positive and actually_positive:
                ar.false_negatives = (ar.false_negatives or 0) + 1
            else:
                ar.true_negatives = (ar.true_negatives or 0) + 1

            # Recompute aggregate metrics
            tp = ar.true_positives or 0
            fp = ar.false_positives or 0
            fn = ar.false_negatives or 0
            tn = ar.true_negatives or 0
            total = tp + fp + fn + tn
            ar.accuracy = (tp + tn) / total if total else 0.0
            ar.precision = tp / (tp + fp) if (tp + fp) else 0.0
            ar.recall = tp / (tp + fn) if (tp + fn) else 0.0
            # Reliability score = harmonic mean of precision & recall, blended with accuracy
            pr = ar.precision
            rc_recall = ar.recall
            f1 = 2 * pr * rc_recall / (pr + rc_recall) if (pr + rc_recall) else 0.0
            ar.reliability_score = 0.5 * f1 + 0.5 * ar.accuracy

        await session.commit()
    return True


async def _update_memory(inp: LearningInput, rc: str) -> None:
    """Add the confirmed incident to the semantic incident memory."""
    store = MemoryStore.get(inp.tenant_id)
    text_repr = (
        f"Title: {inp.incident_id}\n"
        f"Root cause: {rc}\n"
        f"Resolution: {inp.resolution or 'n/a'}\n"
        f"Services: {', '.join(inp.affected_services)}\n"
        f"Logs (sample): {' '.join(inp.raw_logs[:20])}"
    )
    await store.add(
        incident_id=inp.incident_id,
        text_repr=text_repr,
        root_cause=rc,
        resolution=inp.resolution,
        confidence=inp.confidence,
        lessons=inp.lessons or [],
        services=inp.affected_services,
        tenant_id=inp.tenant_id,
    )


async def _persist_lessons(incident_id: str, lessons: Sequence[str], *, tenant_id: str | None) -> None:
    if not lessons:
        return
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        for lesson in lessons:
            await add_lesson(
                session,
                incident_id=incident_id,
                tenant_id=tenant_id,
                lesson=lesson,
                category="confirmed_resolution",
                confidence=0.9,
            )
        await session.commit()


async def learn_from_incident(inp: LearningInput) -> Dict[str, Any]:
    """
    Run the confirmed-incident learning pipeline.

    Returns a dict summarising what was updated.  Learning is skipped
    (``learned=False``) when ``LEARNING_MODE=frozen``, or when no
    confirmed root cause is available and confirmation is required.
    """
    summary: Dict[str, Any] = {"incident_id": inp.incident_id, "learned": False}

    if (settings.learning_mode or "online").lower() != "online":
        summary["reason"] = "learning_mode_frozen"
        logger.info("learning_skipped_frozen", extra=summary)
        return summary

    # Learning writes into tenant-scoped tables, so a run without a tenant
    # could not be attributed to anyone. Refuse rather than write unscoped.
    if not inp.tenant_id:
        summary["reason"] = "missing_tenant"
        logger.info(
            "learning_skipped_missing_tenant", extra={"incident_id": inp.incident_id}
        )
        return summary

    rc = confirmed_root_cause(inp)
    if settings.learning_require_confirmation and not rc:
        summary["reason"] = "unconfirmed_incident"
        logger.info(
            "learning_skipped_unconfirmed",
            extra={
                "incident_id": inp.incident_id,
                "hint": (
                    "PRISM consensus is a hypothesis, not ground truth. "
                    "Confirm via POST /incidents/{id}/resolve with "
                    "confirmed_root_cause before learning."
                ),
            },
        )
        return summary

    pattern_ids = await _update_pattern_library(inp.incident_id, inp, rc)
    await _update_knowledge_graph(inp, rc)
    reliability_updated = await _update_agent_reliability(inp, rc)
    await _update_memory(inp, rc)
    await _persist_lessons(inp.incident_id, inp.lessons or [], tenant_id=inp.tenant_id)

    summary.update(
        {
            "learned": True,
            "patterns_created": len(pattern_ids),
            "pattern_ids": pattern_ids,
            "memory_updated": True,
            "knowledge_graph_updated": True,
            "agent_reliability_updated": reliability_updated,
        }
    )
    return summary