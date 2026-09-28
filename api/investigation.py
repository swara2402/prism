"""
api.investigation
=================

The master pipeline endpoint.

POST /investigate
    Accepts an :class:`IncidentCreate` and runs the complete
    pipeline:

        orchestrator -> causal graph -> confidence propagation
        -> consensus -> explainability -> meta-reasoning

    Continuous learning is deliberately NOT run here: PRISM's consensus
    is an unverified hypothesis.  Learning is deferred to
    ``POST /incidents/{id}/resolve``, which requires a confirmed
    (ground-truth) root cause before updating patterns, memory,
    knowledge-graph edges, or agent reliability.

GET /incidents/{id}
    Returns the persisted incident + root cause + resolution + lessons.

POST /incidents/{id}/resolve
    Records an engineer-supplied resolution and — when a confirmed root
    cause is provided — triggers continuous learning against that
    ground truth.  This is the authoritative learning trigger.

GET /incidents
    Lists recent incidents.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
import json

from api.deps import require_api_key
from causal_graph.engine import CausalGraphBuilder
from confidence.engine import propagate
from consensus.engine import reach_consensus
from config.logging import get_logger
from config.settings import settings
from database.repositories import (
    create_incident,
    get_incident,
    get_resolution,
    get_root_cause,
    list_findings,
    list_incidents,
    save_resolution,
    save_root_cause,
    update_incident,
)
from explainability.engine import build_explanation
from investigation.tree import run_tree
from learning.continuous_learning import LearningInput, learn_from_incident
from meta_reasoning.engine import evaluate as meta_evaluate
from models.schemas import (
    AgentStatus,
    IncidentCreate,
    IncidentOut,
    InvestigationResult,
    JobOut,
    ResolutionCreate,
    ResolutionOut,
    RootCauseOut,
    ExplanationOut,
    MetaReasoningOut,
    AlternativeHypothesis,
)
from sqlalchemy.exc import IntegrityError
from utils.rate_limit import build_rate_limiter

logger = get_logger(__name__)


def apply_graph_traversal_fallback(
    consensus: Any,
    candidates: List[Dict[str, Any]],
    enable_fallback: bool = False,
) -> Any:
    """Explicitly isolated graph traversal fallback helper.

    When enable_fallback is True and consensus root cause is undetermined,
    falls back to the highest confidence graph traversal candidate.
    """
    if enable_fallback and getattr(consensus, "root_cause", None) == "undetermined" and candidates:
        logger.warning(
            "graph_traversal_fallback_applied: candidate=%s",
            candidates[0].get("label"),
        )
        return consensus.__class__(
            root_cause=candidates[0].get("label") or "undetermined",
            confidence=candidates[0].get("confidence", 0.3),
            alternatives=consensus.alternatives,
            explanation=consensus.explanation + " (fallback to graph traversal)",
            voter_breakdown=consensus.voter_breakdown,
        )
    return consensus

router = APIRouter(prefix="/incidents", tags=["incidents"])

# Concurrency limiter for expensive investigations
_investigation_semaphore = asyncio.Semaphore(settings.max_concurrent_investigations)
_ACQUIRE_TIMEOUT = 5.0  # seconds to wait for a free slot

# Per-caller request rate limiter (settings.investigation_rate_limit).
# Redis-backed when settings.redis_url is set (shared across replicas).
_rate_limiter = build_rate_limiter(
    settings.investigation_rate_limit, settings.redis_url
)

# Reproducibility metadata for this PRISM build.
_PRISM_VERSION = "1.1.0"


def _build_agent_statuses(findings: List[Any]) -> List[Dict[str, Any]]:
    """Reduce findings into per-agent execution status (P1#21 failure semantics).

    ``finding_type == "error"`` (set by :class:`BaseAgent.run`) marks a
    degraded agent; anything else is a successful invocation.
    """
    statuses: List[Dict[str, Any]] = []
    for f in findings:
        name = getattr(f, "agent_name", None) or (f.get("agent_name") if isinstance(f, dict) else None) or "unknown"
        ftype = getattr(f, "finding_type", None) or (f.get("finding_type") if isinstance(f, dict) else None) or "analysis"
        latency = getattr(f, "latency_s", 0.0) or (f.get("latency_s", 0.0) if isinstance(f, dict) else 0.0)
        statuses.append({
            "agent_name": name,
            "status": "failed" if ftype == "error" else "ok",
            "execution_ms": round(float(latency or 0.0) * 1000, 2),
            "finding_type": ftype,
        })
    return statuses


def _runtime_metadata() -> Dict[str, Any]:
    """Capture the exact runtime / model / config versions (P2#22)."""
    return {
        "prism_version": _PRISM_VERSION,
        "llm_model": settings.ollama_model,
        "llm_host": settings.ollama_host,
        "embedding_model": settings.sentence_transformer_model,
        "learning_mode": settings.learning_mode,
        "learning_require_confirmation": settings.learning_require_confirmation,
        "mdv_threshold": settings.MDV_THRESHOLD,
        "max_investigation_steps": settings.MAX_INVESTIGATION_STEPS,
        "consensus_confidence_threshold": settings.consensus_confidence_threshold,
        "database_url_masked": _mask_database_url(settings.database_url),
    }


def _normalize_tenant(value: Optional[str]) -> Optional[str]:
    return (value or "").strip()[:64] or None


def _enforce_tenant(value: Optional[str]) -> Optional[str]:
    """Normalize and (optionally) require the X-Tenant-Id header.

    When ``settings.require_tenant_header`` is enabled, requests that omit the
    tenant header are rejected, giving auth-layer tenant isolation instead of
    soft scoping.  Disabled by default for backward compatibility.
    """
    tenant = _normalize_tenant(value)
    if settings.require_tenant_header and tenant is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-Id header is required",
        )
    return tenant


def _mask_database_url(url: str) -> str:
    """Mask credentials in a database URL for reproducibility logs."""
    try:
        scheme, _, rest = url.partition("://")
        if "@" in rest:
            auth, _, host = rest.rpartition("@")
            return f"{scheme}://***@{host}"
        return f"{scheme}://{rest}"
    except Exception:
        return "[redacted]"


def _rate_limit_key(
    request: Request,
    tenant: Optional[str],
    api_key: str,
) -> str:
    """Identity used for rate limiting: tenant > API-key hash > client IP."""
    if tenant:
        return f"tenant:{tenant}"
    digest = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    if api_key and api_key != "anonymous":
        return f"apikey:{digest}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def _job_to_out(job: Any) -> JobOut:
    """Build a :class:`JobOut` from a job row, embedding the incident when done."""
    from database.repositories import get_incident
    from database.session import AsyncSessionLocal

    incident_out = None
    if job.result_incident_id:
        async with AsyncSessionLocal() as session:
            inc = await get_incident(session, job.result_incident_id)
            incident_out = IncidentOut.model_validate(inc) if inc is not None else None
    return JobOut(
        id=job.id,
        tenant_id=job.tenant_id,
        status=job.status,
        progress=job.progress,
        attempts=job.attempts,
        attempts_max=job.attempts_max,
        result_incident_id=job.result_incident_id,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        incident=incident_out,
    )


# ---------------------------------------------------------------
# POST /incidents/investigate
# ---------------------------------------------------------------

@router.post("/investigate", response_model=InvestigationResult)
async def investigate(
    incident_in: IncidentCreate,
    request: Request,
    _api_key: str = Depends(require_api_key),
    x_idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> InvestigationResult:
    """
    Run the full agentic investigation pipeline for a new incident.
    Requires X-API-Key when API_KEY is configured.

    Sending the same ``Idempotency-Key`` header for the same incident
    returns the previously persisted investigation instead of running
    duplicate agent work.  The optional ``X-Tenant-Id`` header scopes the
    incident to a tenant.
    """
    request_id = getattr(request.state, "request_id", None) or "unknown"
    acquired = False

    # Idempotency: a previously recorded investigation under this key is
    # returned directly.  Enforced both by the in-process lock AND the
    # incident dedup in create_incident (key stored on the incident row).
    idem_key = (x_idempotency_key or "").strip()[:128] or None
    tenant = _enforce_tenant(x_tenant_id)

    if tenant:
        rate_key = f"tenant:{tenant}"
    elif _api_key:
        digest = hashlib.sha256(_api_key.encode()).hexdigest()[:16]
        rate_key = f"apikey:{digest}"
    else:
        rate_key = f"ip:{request.client.host if request.client else 'unknown'}"

    allowed, retry_after = await _rate_limiter.check(rate_key)
    if not allowed:
        logger.warning(
            "investigation_rate_limited",
            extra={"request_id": request_id, "rate_limit": settings.investigation_rate_limit},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded ({settings.investigation_rate_limit}). "
                "Try again shortly."
            ),
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )

    try:
        await asyncio.wait_for(
            _investigation_semaphore.acquire(),
            timeout=_ACQUIRE_TIMEOUT,
        )
        acquired = True
    except asyncio.TimeoutError:
        logger.warning(
            "investigation_capacity_exceeded",
            extra={
                "request_id": request_id,
                "max_concurrent": settings.max_concurrent_investigations,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Too many concurrent investigations "
                f"(limit={settings.max_concurrent_investigations}). Try again shortly."
            ),
        )

    try:
        return await _run_investigation(
            incident_in,
            request_id=request_id,
            idempotency_key=idem_key,
            tenant_id=tenant,
        )
    finally:
        if acquired:
            _investigation_semaphore.release()


async def _run_investigation(
    incident_in: IncidentCreate,
    request_id: str = "unknown",
    idempotency_key: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> InvestigationResult:
    """
    Internal investigation pipeline (runs under the concurrency semaphore).
    """
    # 1. Redact sensitive data from evidence BEFORE persistence or LLM exposure.
    from utils.redaction import scrub, scrub_collection, scrub_iterable
    safe_logs = scrub_iterable(incident_in.raw_logs)
    safe_metrics = scrub_collection(incident_in.metrics)
    safe_traces = scrub_collection(incident_in.traces)
    safe_topology = scrub_collection(incident_in.topology)
    safe_context = scrub_collection(incident_in.context)
    safe_description = scrub(incident_in.description or "") or None

    # 1. Persist the incident - use repository's create_incident which has built-in deduplication
    from database.session import AsyncSessionLocal
    from database.repositories import get_incident_by_idempotency_key

    try:
        async with AsyncSessionLocal() as session:
            # Idempotency: if an investigation was already run under this key, do
            # not run the pipeline again — return the existing result.
            if idempotency_key:
                existing = await get_incident_by_idempotency_key(
                    session, idempotency_key, tenant_id=tenant_id
                )
                if existing is not None:
                    logger.info(
                        "investigation_idempotent_hit",
                        extra={
                            "request_id": request_id,
                            "incident_id": existing.id,
                            "idempotency_key": idempotency_key,
                        },
                    )
                    await session.close()
                    return await _result_for_existing_incident(existing.id, reused=True)

            # create_incident automatically checks for similar existing incidents and updates them
            inc = await create_incident(
                session,
                title=incident_in.title,
                description=safe_description or None,
                severity=incident_in.severity,
                status="investigating",
                incident_type=incident_in.incident_type,
                affected_services=incident_in.affected_services,
                raw_logs=safe_logs,
                metrics=safe_metrics,
                traces=safe_traces,
                topology=safe_topology,
                context=safe_context,
                started_at=incident_in.started_at,
                tenant_id=tenant_id,
            )
            # Record the idempotency key for future dedup lookups.
            if idempotency_key and getattr(inc, "idempotency_key", None) != idempotency_key:
                from database.repositories import update_incident as _upd
                await _upd(session, inc.id, idempotency_key=idempotency_key)
            await session.commit()
            incident_id = inc.id
    except IntegrityError:
        # A concurrent request claimed this idempotency key onto another
        # incident row (or updated an existing one) — replay its result
        # instead of failing with a 500.
        logger.info(
            "investigation_idempotency_race_replayed",
            extra={"request_id": request_id, "idempotency_key": idempotency_key},
        )
        if idempotency_key:
            async with AsyncSessionLocal() as _sess:
                existing = await get_incident_by_idempotency_key(
                    _sess, idempotency_key, tenant_id=tenant_id
                )
                if existing is not None:
                    return await _result_for_existing_incident(existing.id, reused=True)
        raise

    # 2. Build investigation context (already redacted)
    context: Dict[str, Any] = {
        "logs": safe_logs,
        "metrics": safe_metrics,
        "traces": safe_traces,
        "topology": safe_topology,
        "affected_services": incident_in.affected_services,
        "incident_type": incident_in.incident_type,
    }

    start = time.perf_counter()

    # 3. Run dynamic investigation tree (which delegates to the orchestrator)
    tree_result = await run_tree(
        incident_id=incident_id,
        incident_type=incident_in.incident_type,
        context=context,
    )

    findings = tree_result.findings
    agents_used = tree_result.agents_used

    # 4. Build causal graph
    builder = CausalGraphBuilder()
    causal_graph = builder.build_from_findings(
        findings=findings,
        affected_services=incident_in.affected_services,
        logs=safe_logs,
        metrics=safe_metrics,
        traces=safe_traces,
    )

    # 5. Confidence propagation
    propagation = propagate(causal_graph, iterations=5)

    # 6. Find root cause candidates via graph traversal
    candidates = causal_graph.find_root_causes(top_k=5)
    root_node_id = candidates[0]["node_id"] if candidates else None
    # Map candidate label to confidence for consensus
    graph_candidates = {
        candidate["label"]: candidate.get("confidence", 0.0)
        for candidate in candidates
        if candidate.get("label")
    }

    # 7. Consensus engine
    consensus = await reach_consensus(
        findings=findings,
        reliability_scores=tree_result.reliability_scores,
        graph_candidates=graph_candidates,
    )

    # Graph traversal fallback is explicitly isolated and disabled by default
    consensus = apply_graph_traversal_fallback(consensus, candidates, enable_fallback=False)


    # 8. Persist root cause
    causal_chain = causal_graph.causal_chain_to(root_node_id) if root_node_id else []
    contributing: List[str] = [
        label
        for c in causal_chain
        if isinstance(label := c.get("label"), str) and label
    ]
    async with AsyncSessionLocal() as session:
        await save_root_cause(
            session,
            incident_id=incident_id,
            root_cause=consensus.root_cause,
            confidence=consensus.confidence,
            alternatives=[
                {"cause": a.cause, "confidence": a.confidence, "evidence": a.evidence}
                for a in consensus.alternatives
            ],
            explanation=consensus.explanation,
            causal_chain=causal_chain,
            contributing_factors=contributing,
        )
        await session.commit()

    # 9. Explainability
    explanation = build_explanation(
        incident_id=incident_id,
        findings=findings,
        consensus=consensus,
        causal_graph=causal_graph,
        propagation=propagation,
        root_cause_node_id=root_node_id,
    )

    # 10. Meta-reasoning
    meta = meta_evaluate(
        incident_id=incident_id,
        findings=findings,
        agents_skipped=tree_result.agents_skipped,
        final_root_cause=consensus.root_cause,
        final_confidence=consensus.confidence,
        agents_used=agents_used,
    )

    # 11. Update incident status.  Findings were already persisted by the
    # orchestrator (with root_cause_hint provenance) so a later confirmed
    # resolution can grade each agent against ground truth.
    async with AsyncSessionLocal() as session:
        await update_incident(session, incident_id, status="investigating")
        await session.commit()

    # 12. NO continuous learning here.  PRISM's consensus is an unverified
    # hypothesis.  Learning is deferred to POST /incidents/{id}/resolve,
    # which requires a confirmed (ground-truth) root cause.

    duration = time.perf_counter() - start

    agent_statuses = _build_agent_statuses(findings)
    failed = [a["agent_name"] for a in agent_statuses if a["status"] == "failed"]

    return InvestigationResult(
        incident_id=incident_id,
        root_cause=RootCauseOut(
            incident_id=incident_id,
            root_cause=consensus.root_cause,
            confidence=consensus.confidence,
            alternatives=[
                AlternativeHypothesis(
                    cause=a.cause,
                    confidence=a.confidence,
                    evidence=a.evidence,
                )
                for a in consensus.alternatives
            ],
            explanation=consensus.explanation,
            causal_chain=causal_chain,
            contributing_factors=contributing,
        ),
        explanation=ExplanationOut(
            incident_id=incident_id,
            evidence_used=explanation.evidence_used,
            confidence_breakdown=explanation.confidence_breakdown,
            graph_reasoning=explanation.graph_reasoning,
            alternative_root_causes=[
                AlternativeHypothesis(
                    cause=a["cause"],
                    confidence=a["confidence"],
                    evidence=a.get("evidence", []),
                )
                for a in explanation.alternative_root_causes
            ],
            final_explanation=explanation.final_explanation,
        ),
        meta_reasoning=MetaReasoningOut(
            incident_id=incident_id,
            useful_agents=meta.useful_agents,
            unnecessary_agents=meta.unnecessary_agents,
            optimal_path=meta.optimal_path,
            suggestions=meta.suggestions,
            agent_scores=meta.agent_scores,
        ),
        agents_used=agents_used,
        duration_seconds=round(duration, 3),
        agent_statuses=[AgentStatus(**a) for a in agent_statuses],
        status="completed_with_degraded_agents" if failed else "completed",
        runtime=_runtime_metadata(),
    )


async def _result_for_existing_incident(
    incident_id: str,
    reused: bool = False,
) -> InvestigationResult:
    """Rebuild an :class:`InvestigationResult` from a previously persisted run.

    Used for idempotent replays so a repeat ``POST /incidents/investigate``
    with the same ``Idempotency-Key`` never re-runs the agent pipeline.
    """
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rc = await get_root_cause(session, incident_id)
        if rc is None:
            raise HTTPException(409, "Earlier investigation left no root cause to replay")
        findings = await list_findings(session, incident_id)

    agent_statuses = _build_agent_statuses(findings)
    failed = [a["agent_name"] for a in agent_statuses if a["status"] == "failed"]

    return InvestigationResult(
        incident_id=incident_id,
        root_cause=RootCauseOut(
            incident_id=incident_id,
            root_cause=rc.root_cause,
            confidence=rc.confidence,
            alternatives=[
                AlternativeHypothesis(
                    cause=a.get("cause", ""),
                    confidence=a.get("confidence", 0.0),
                    evidence=a.get("evidence", []),
                )
                for a in rc.alternatives
            ],
            explanation=rc.explanation,
            causal_chain=rc.causal_chain,
            contributing_factors=rc.contributing_factors,
        ),
        explanation=ExplanationOut(
            incident_id=incident_id,
            evidence_used=[],
            confidence_breakdown={},
            graph_reasoning=rc.explanation,
            alternative_root_causes=[],
            final_explanation=rc.explanation,
        ),
        meta_reasoning=MetaReasoningOut(
            incident_id=incident_id,
            useful_agents=[a["agent_name"] for a in agent_statuses if a["status"] == "ok"],
            unnecessary_agents=[],
            optimal_path="",
            suggestions=["Replayed from idempotent investigation."] if reused else [],
            agent_scores={},
        ),
        agents_used=[a["agent_name"] for a in agent_statuses],
        duration_seconds=0.0,
        agent_statuses=[AgentStatus(**a) for a in agent_statuses],
        status="completed_with_degraded_agents" if failed else "completed",
        runtime=_runtime_metadata(),
    )


async def _stream_investigation(
    incident_in: IncidentCreate,
    request_id: str = "unknown",
    idempotency_key: Optional[str] = None,
):
    """
    Generator that executes the investigation pipeline while yielding SSE events.
    """
    start = time.perf_counter()

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("pipeline_started", {
        "message": "Investigation pipeline initialized",
        "request_id": request_id,
        "title": incident_in.title,
    })

    # 1. Redact sensitive data before persistence / LLM exposure.
    from utils.redaction import scrub, scrub_collection, scrub_iterable
    safe_logs = scrub_iterable(incident_in.raw_logs)
    safe_metrics = scrub_collection(incident_in.metrics)
    safe_traces = scrub_collection(incident_in.traces)
    safe_topology = scrub_collection(incident_in.topology)
    safe_context = scrub_collection(incident_in.context)
    safe_description = scrub(incident_in.description or "") or None

    # 1. Persist the incident - use repository's create_incident which has built-in deduplication
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        # create_incident automatically checks for similar existing incidents and updates them
        inc = await create_incident(
            session,
            title=incident_in.title,
            description=safe_description,
            severity=incident_in.severity,
            status="investigating",
            incident_type=incident_in.incident_type,
            affected_services=incident_in.affected_services,
            raw_logs=safe_logs,
            metrics=safe_metrics,
            traces=safe_traces,
            topology=safe_topology,
            context=safe_context,
            started_at=incident_in.started_at,
        )
        if idempotency_key and getattr(inc, "idempotency_key", None) != idempotency_key:
            from database.repositories import update_incident as _upd
            await _upd(session, inc.id, idempotency_key=idempotency_key)
        await session.commit()
        incident_id = inc.id

    yield sse("incident_persisted", {
        "incident_id": incident_id,
        "title": incident_in.title,
        "severity": incident_in.severity,
        "affected_services": incident_in.affected_services,
    })

    # 2. Build context (already redacted)
    context: Dict[str, Any] = {
        "logs": safe_logs,
        "metrics": safe_metrics,
        "traces": safe_traces,
        "topology": safe_topology,
        "affected_services": incident_in.affected_services,
        "incident_type": incident_in.incident_type,
    }

    yield sse("agents_dispatched", {
        "incident_type": incident_in.incident_type,
        "affected_services": incident_in.affected_services,
        "message": f"Orchestrating investigation tree for {incident_in.incident_type} incident...",
    })

    # 3. Run tree
    tree_result = await run_tree(
        incident_id=incident_id,
        incident_type=incident_in.incident_type,
        context=context,
    )

    findings = tree_result.findings
    agents_used = tree_result.agents_used

    # Stream each agent finding
    for f in findings:
        agent_name = (f.get("agent_name") or f.get("agent")) if isinstance(f, dict) else getattr(f, "agent_name", getattr(f, "agent", "analyzer"))
        finding_type = f.get("finding_type", "analysis") if isinstance(f, dict) else getattr(f, "finding_type", "analysis")
        conf_val = f.get("confidence", 0.5) if isinstance(f, dict) else getattr(f, "confidence", 0.5)
        details = f.get("details", {}) if isinstance(f, dict) else getattr(f, "details", {})
        summary = f.get("summary", "") if isinstance(f, dict) else getattr(f, "summary", "")
        if not summary and isinstance(details, dict):
            summary = details.get("description", "")

        yield sse("agent_completed", {
            "agent": agent_name,
            "finding_type": finding_type,
            "confidence": round(float(conf_val or 0.5), 3),
            "summary": summary,
        })

    # 4. Build causal graph
    builder = CausalGraphBuilder()
    causal_graph = builder.build_from_findings(
        findings=findings,
        affected_services=incident_in.affected_services,
        logs=safe_logs,
        metrics=safe_metrics,
        traces=safe_traces,
    )

    nodes_list = causal_graph.nodes() if callable(causal_graph.nodes) else causal_graph.nodes
    edges_list = causal_graph.edges() if callable(causal_graph.edges) else causal_graph.edges

    yield sse("causal_graph_built", {
        "nodes_count": len(nodes_list),
        "edges_count": len(edges_list),
        "message": f"Constructed causal graph with {len(nodes_list)} nodes and {len(edges_list)} edges",
    })

    # 5. Confidence propagation
    propagation = propagate(causal_graph, iterations=5)

    # 6. Graph candidates
    candidates = causal_graph.find_root_causes(top_k=5)
    root_node_id = candidates[0]["node_id"] if candidates else None
    graph_candidates = {
        candidate["label"]: candidate.get("confidence", 0.0)
        for candidate in candidates
        if candidate.get("label")
    }

    yield sse("confidence_propagated", {
        "iterations": 5,
        "candidate_count": len(candidates),
        "top_candidate": candidates[0].get("label") if candidates else None,
    })

    # 7. Consensus
    consensus = await reach_consensus(
        findings=findings,
        reliability_scores=tree_result.reliability_scores,
        graph_candidates=graph_candidates,
    )
    consensus = apply_graph_traversal_fallback(consensus, candidates, enable_fallback=False)

    yield sse("consensus_reached", {
        "root_cause": consensus.root_cause,
        "confidence": round(float(consensus.confidence), 3),
        "alternatives_count": len(consensus.alternatives),
    })
    
    # Emit verdict event for backward compatibility with tests
    yield sse("verdict", {
        "root_cause": consensus.root_cause,
        "confidence": round(float(consensus.confidence), 3),
        "explanation": consensus.explanation,
    })

    # 8. Persist root cause
    causal_chain = causal_graph.causal_chain_to(root_node_id) if root_node_id else []
    contributing: List[str] = [
        label
        for c in causal_chain
        if isinstance(label := c.get("label"), str) and label
    ]
    async with AsyncSessionLocal() as session:
        await save_root_cause(
            session,
            incident_id=incident_id,
            root_cause=consensus.root_cause,
            confidence=consensus.confidence,
            alternatives=[
                {"cause": a.cause, "confidence": a.confidence, "evidence": a.evidence}
                for a in consensus.alternatives
            ],
            explanation=consensus.explanation,
            causal_chain=causal_chain,
            contributing_factors=contributing,
        )
        await session.commit()

    # 9. Explainability
    explanation = build_explanation(
        incident_id=incident_id,
        findings=findings,
        consensus=consensus,
        causal_graph=causal_graph,
        propagation=propagation,
        root_cause_node_id=root_node_id,
    )

    yield sse("explanation_built", {
        "summary": consensus.root_cause,
    })

    # 10. Meta-reasoning
    meta = meta_evaluate(
        incident_id=incident_id,
        findings=findings,
        agents_skipped=tree_result.agents_skipped,
        final_root_cause=consensus.root_cause,
        final_confidence=consensus.confidence,
        agents_used=agents_used,
    )

    # 11. Update incident status.  Findings were already persisted by the
    # orchestrator (with root_cause_hint provenance).
    async with AsyncSessionLocal() as session:
        await update_incident(session, incident_id, status="investigating")
        await session.commit()

    # 12. NO learning here (unverified consensus).  Learning only runs after
    # the incident is resolved with a confirmed root cause.
    yield sse("learning_deferred", {
        "detail": (
            "Continuous learning is deferred until the incident is resolved "
            "with a confirmed (ground-truth) root cause."
        ),
    })

    duration = time.perf_counter() - start

    agent_statuses = _build_agent_statuses(findings)
    failed = [a["agent_name"] for a in agent_statuses if a["status"] == "failed"]

    final_result = InvestigationResult(
        incident_id=incident_id,
        root_cause=RootCauseOut(
            incident_id=incident_id,
            root_cause=consensus.root_cause,
            confidence=consensus.confidence,
            alternatives=[
                AlternativeHypothesis(
                    cause=a.cause,
                    confidence=a.confidence,
                    evidence=a.evidence,
                )
                for a in consensus.alternatives
            ],
            explanation=consensus.explanation,
            causal_chain=causal_chain,
            contributing_factors=contributing,
        ),
        explanation=ExplanationOut(
            incident_id=incident_id,
            evidence_used=explanation.evidence_used,
            confidence_breakdown=explanation.confidence_breakdown,
            graph_reasoning=explanation.graph_reasoning,
            alternative_root_causes=[
                AlternativeHypothesis(
                    cause=a["cause"],
                    confidence=a["confidence"],
                    evidence=a.get("evidence", []),
                )
                for a in explanation.alternative_root_causes
            ],
            final_explanation=explanation.final_explanation,
        ),
        meta_reasoning=MetaReasoningOut(
            incident_id=incident_id,
            useful_agents=meta.useful_agents,
            unnecessary_agents=meta.unnecessary_agents,
            optimal_path=meta.optimal_path,
            suggestions=meta.suggestions,
            agent_scores=meta.agent_scores,
        ),
        agents_used=agents_used,
        duration_seconds=round(duration, 3),
        agent_statuses=[AgentStatus(**a) for a in agent_statuses],
        status="completed_with_degraded_agents" if failed else "completed",
        runtime=_runtime_metadata(),
    )

    yield sse("investigation_result", final_result.model_dump())


# ---------------------------------------------------------------
# POST /incidents/investigate/stream (SSE)
# ---------------------------------------------------------------

@router.post("/investigate/stream")
async def investigate_stream(
    incident_in: IncidentCreate,
    request: Request,
    _api_key: str = Depends(require_api_key),
    x_idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    """
    Run the full agentic investigation pipeline while streaming real-time SSE events.
    """
    request_id = getattr(request.state, "request_id", None) or "unknown"
    idem_key = (x_idempotency_key or "").strip()[:128] or None

    async def event_generator():
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    _investigation_semaphore.acquire(),
                    timeout=_ACQUIRE_TIMEOUT,
                )
                acquired = True
            except asyncio.TimeoutError:
                err_data = json.dumps({"error": f"Too many concurrent investigations (limit={settings.max_concurrent_investigations})."})
                yield f"event: error\ndata: {err_data}\n\n"
                return

            # Idempotency replay: return the already-persisted result as the
            # final SSE event without re-running any agents.
            if idem_key:
                from database.repositories import get_incident_by_idempotency_key
                from database.session import AsyncSessionLocal

                async with AsyncSessionLocal() as session:
                    existing = await get_incident_by_idempotency_key(session, idem_key)
                    existing_id = existing.id if existing is not None else None
                if existing_id is not None:
                    yield f"event: idempotent_replay\ndata: {json.dumps({'incident_id': existing_id, 'detail': 'Investigation previously run under this Idempotency-Key.'})}\n\n"
                    result = await _result_for_existing_incident(existing_id, reused=True)
                    yield f"event: investigation_result\ndata: {json.dumps(result.model_dump())}\n\n"
                    return

            async for chunk in _stream_investigation(incident_in, request_id=request_id, idempotency_key=idem_key):
                yield chunk
        finally:
            if acquired:
                _investigation_semaphore.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------
# POST /incidents/investigate/async  (background job queue)
# ---------------------------------------------------------------

@router.post("/investigate/async", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def investigate_async(
    incident_in: IncidentCreate,
    request: Request,
    _api_key: str = Depends(require_api_key),
    x_idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> JobOut:
    """
    Enqueue an investigation and return immediately (``202 Accepted``).

    A worker drains the ``investigation_jobs`` table and runs the pipeline in
    the background; poll ``GET /incidents/jobs/{id}`` for status.  Reusing an
    ``Idempotency-Key`` returns the already-enqueued job instead of creating a
    second one.  The synchronous and SSE endpoints are unchanged.
    """
    from datetime import datetime, timezone

    from database.repositories import get_job_by_idempotency_key
    from database.session import AsyncSessionLocal
    from utils.job_queue import enqueue_investigation

    request_id = getattr(request.state, "request_id", None) or "unknown"
    idem_key = (x_idempotency_key or "").strip()[:128] or None
    tenant = _enforce_tenant(x_tenant_id)

    allowed, retry_after = await _rate_limiter.check(
        _rate_limit_key(request, tenant, _api_key)
    )
    if not allowed:
        logger.warning(
            "investigation_rate_limited",
            extra={"request_id": request_id, "route": "/investigate/async"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded ({settings.investigation_rate_limit}). "
                "Try again shortly."
            ),
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )

    payload = {
        "incident": incident_in.model_dump(mode="json"),
        "idempotency_key": idem_key,
        "tenant_id": tenant,
        "request_id": request_id,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        job = await enqueue_investigation(
            payload,
            idempotency_key=idem_key,
            tenant_id=tenant,
            attempts_max=settings.job_attempts_max,
        )
    except IntegrityError:
        # A concurrent enqueue claimed this idempotency key first — replay it.
        async with AsyncSessionLocal() as session:
            job = await get_job_by_idempotency_key(session, idem_key, tenant_id=tenant)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key already in use",
            )
        logger.info(
            "job_idempotency_replay",
            extra={"request_id": request_id, "job_id": job.id},
        )

    return await _job_to_out(job)


# ---------------------------------------------------------------
# GET /incidents/jobs and /incidents/jobs/{id}, cancel
# ---------------------------------------------------------------

@router.get("/jobs", response_model=List[JobOut])
async def list_jobs_endpoint(
    limit: int = Query(default=50, ge=1, le=settings.max_pagination_limit),
    _api_key: str = Depends(require_api_key),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> List[JobOut]:
    from database.repositories import list_jobs
    from database.session import AsyncSessionLocal

    tenant = _enforce_tenant(x_tenant_id)
    async with AsyncSessionLocal() as session:
        jobs = await list_jobs(session, limit=limit, tenant_id=tenant)
    return [await _job_to_out(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_endpoint(
    job_id: str,
    _api_key: str = Depends(require_api_key),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> JobOut:
    from database.repositories import get_job
    from database.session import AsyncSessionLocal

    tenant = _enforce_tenant(x_tenant_id)
    async with AsyncSessionLocal() as session:
        job = await get_job(session, job_id, tenant_id=tenant)
    if job is None:
        raise HTTPException(404, "Job not found")
    return await _job_to_out(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job_endpoint(
    job_id: str,
    _api_key: str = Depends(require_api_key),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> JobOut:
    from database.repositories import cancel_job, get_job
    from database.session import AsyncSessionLocal

    tenant = _enforce_tenant(x_tenant_id)
    async with AsyncSessionLocal() as session:
        job = await get_job(session, job_id, tenant_id=tenant)
        if job is None:
            raise HTTPException(404, "Job not found")
        await cancel_job(session, job_id, tenant_id=tenant)
        await session.commit()
        # updated_at is refreshed server-side on commit; re-read it while the
        # row is still attached so _job_to_out never touches an expired attr.
        await session.refresh(job)
    return await _job_to_out(job)



# ---------------------------------------------------------------
# GET /incidents
# ---------------------------------------------------------------

@router.get("", response_model=List[IncidentOut])
async def list_recent_incidents(
    limit: int = Query(default=50, ge=1, le=settings.max_pagination_limit),
    _api_key: str = Depends(require_api_key),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> List[IncidentOut]:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = await list_incidents(
            session, limit=limit, tenant_id=_enforce_tenant(x_tenant_id)
        )
    return [IncidentOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------
# GET /incidents/{id}
# ---------------------------------------------------------------

@router.get("/{incident_id}", response_model=IncidentOut)
async def get_incident_by_id(
    incident_id: str,
    _api_key: str = Depends(require_api_key),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> IncidentOut:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        inc = await get_incident(
            session, incident_id, tenant_id=_enforce_tenant(x_tenant_id)
        )
    if inc is None:
        raise HTTPException(404, "Incident not found")
    return IncidentOut.model_validate(inc)


# ---------------------------------------------------------------
# GET /incidents/{id}/root-cause
# ---------------------------------------------------------------

@router.get("/{incident_id}/root-cause", response_model=RootCauseOut)
async def get_root_cause_for_incident(
    incident_id: str,
    _api_key: str = Depends(require_api_key),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> RootCauseOut:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        inc = await get_incident(
            session, incident_id, tenant_id=_enforce_tenant(x_tenant_id)
        )
        if inc is None:
            raise HTTPException(404, "Incident not found")
        rc = await get_root_cause(session, incident_id)
    if rc is None:
        raise HTTPException(404, "Root cause not found")
    return RootCauseOut(
        incident_id=rc.incident_id,
        root_cause=rc.root_cause,
        confidence=rc.confidence,
        alternatives=[
            AlternativeHypothesis(
                cause=a.get("cause", ""),
                confidence=a.get("confidence", 0.0),
                evidence=a.get("evidence", []),
            )
            for a in rc.alternatives
        ],
        explanation=rc.explanation,
        causal_chain=rc.causal_chain,
        contributing_factors=rc.contributing_factors,
    )


# ---------------------------------------------------------------
# POST /incidents/{id}/resolve
# ---------------------------------------------------------------

@router.post("/{incident_id}/resolve", response_model=ResolutionOut)
async def resolve_incident(
    incident_id: str,
    body: ResolutionCreate,
    _api_key: str = Depends(require_api_key),
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
) -> ResolutionOut:
    """
    Record an engineer-supplied resolution.

    This is the **authoritative learning trigger**: when ``body`` includes
    a ``confirmed_root_cause`` (with ``ground_truth_source`` / ``confirmed_by``),
    continuous learning runs against that ground truth — updating patterns,
    memory, knowledge-graph edges, and agent reliability.  Without a
    confirmed root cause, nothing is learned (PRISM's own consensus is never
    treated as truth).
    """
    from datetime import datetime, timezone

    from database.session import AsyncSessionLocal

    tenant = _enforce_tenant(x_tenant_id)

    async def _existing_out() -> ResolutionOut:
        """Build the response from an already-persisted resolution (idempotent replay)."""
        async with AsyncSessionLocal() as _s:
            existing = await get_resolution(_s, incident_id)
        meta = (existing.metadata_ or {}) if existing else {}
        return ResolutionOut(
            incident_id=incident_id,
            action=existing.action if existing else body.action,
            steps=(existing.steps or []) if existing else body.steps,
            verified=existing.verified if existing else body.verified,
            confirmed_root_cause=meta.get("confirmed_root_cause"),
            ground_truth_source=meta.get("ground_truth_source"),
            ground_truth_confidence=meta.get("ground_truth_confidence"),
            confirmed_by=meta.get("confirmed_by"),
        )

    try:
        async with AsyncSessionLocal() as session:
            inc = await get_incident(session, incident_id, tenant_id=tenant)
            if inc is None:
                raise HTTPException(404, "Incident not found")
            if inc.status == "resolved":
                # Idempotent replay: a repeated resolve returns the existing
                # resolution instead of failing with 409.
                return await _existing_out()
            rc = await get_root_cause(session, incident_id)
            findings = await list_findings(session, incident_id)
            affected_services = list(inc.affected_services or [])
            raw_logs = list(inc.raw_logs or [])
            rc_confidence = rc.confidence if rc else 0.5

            meta: Dict[str, Any] = {
                "confirmed_root_cause": body.confirmed_root_cause,
                "ground_truth_source": body.ground_truth_source,
                "ground_truth_confidence": body.ground_truth_confidence,
                "confirmed_by": body.confirmed_by,
                "confirmed_at": datetime.now(timezone.utc).isoformat() if body.confirmed_root_cause else None,
            }
            res = await save_resolution(
                session,
                incident_id=incident_id,
                action=body.action,
                steps=body.steps,
                verified=body.verified,
                metadata_=meta,
            )
            await update_incident(
                session,
                incident_id,
                status="resolved",
                resolved_at=datetime.now(timezone.utc),
            )
            await session.commit()
    except IntegrityError:
        # Concurrent resolve race: another request committed first.  The
        # unique(incident_id) constraint made us lose — the winner already
        # recorded the resolution and ran learning, so replay their result.
        return await _existing_out()

    # Continuous learning runs ONLY against confirmed ground truth.
    confirmed_rc = (body.confirmed_root_cause or "").strip()
    if confirmed_rc:
        learning_findings = [
            {
                "agent_name": f.agent_name,
                "root_cause_hint": (f.metadata_ or {}).get("root_cause_hint"),
                "confidence": f.confidence,
            }
            for f in findings
        ]
        learning_input = LearningInput(
            incident_id=incident_id,
            root_cause=confirmed_rc,
            confidence=body.ground_truth_confidence or rc_confidence,
            affected_services=affected_services,
            raw_logs=raw_logs,
            resolution=body.action,
            agents_used=[],
            findings=learning_findings,
            consensus={},
            lessons=[
                f"Resolution '{body.action}' resolved incident confirmed as "
                f"'{confirmed_rc}' ({body.ground_truth_source or 'confirmed'} "
                f"by {body.confirmed_by or 'unknown'})."
            ],
            ground_truth_root_cause=confirmed_rc,
            ground_truth_source=body.ground_truth_source,
            ground_truth_confidence=body.ground_truth_confidence,
            confirmed_by=body.confirmed_by,
            confirmed_at=datetime.now(timezone.utc),
        )
        try:
            await learn_from_incident(learning_input)
        except Exception as exc:
            logger.warning(
                "learning_failed",
                extra={"incident_id": incident_id, "error": repr(exc)},
            )
    else:
        logger.info(
            "learning_skipped_unconfirmed_resolution",
            extra={
                "incident_id": incident_id,
                "hint": "No confirmed_root_cause supplied; nothing was learned.",
            },
        )

    return ResolutionOut(
        incident_id=incident_id,
        action=res.action,
        steps=res.steps,
        verified=res.verified,
        confirmed_root_cause=meta.get("confirmed_root_cause"),
        ground_truth_source=meta.get("ground_truth_source"),
        ground_truth_confidence=meta.get("ground_truth_confidence"),
        confirmed_by=meta.get("confirmed_by"),
    )