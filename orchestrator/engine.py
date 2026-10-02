"""
orchestrator.engine
===================

The Adaptive Agent Orchestrator.

Responsibilities:
* Given an incident context, ask :mod:`orchestrator.selector` which
  agents to run.
* Run them concurrently (asyncio.gather).
* Reliability-adjust each agent's confidence.
* Persist findings to PostgreSQL.
* Return a structured :class:`OrchestrationResult` ready for the
  consensus engine.

Adding a new agent is a one-liner — just register it in
:mod:`agents.registry` and the orchestrator will pick it up
automatically when the incident context matches.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents.base import BaseAgent, FindingPayload
from config.logging import get_logger
from database.repositories import add_finding, upsert_agent_reliability
from orchestrator.selector import AgentSelection, select_agents

logger = get_logger(__name__)


@dataclass
class OrchestrationResult:
    """Output of an orchestration run."""

    findings: List[FindingPayload] = field(default_factory=list)
    agents_used: List[str] = field(default_factory=list)
    agents_skipped: List[Dict[str, str]] = field(default_factory=list)
    reliability_scores: Dict[str, float] = field(default_factory=dict)
    duration_s: float = 0.0
    incident_id: Optional[str] = None


async def _run_one(
    agent: BaseAgent,
    context: Dict[str, Any],
    reliability: float,
) -> FindingPayload:
    """Run a single agent and adjust its confidence by reliability."""
    payload = await agent.run(context)
    payload.confidence = agent.adjust_confidence(payload.confidence, reliability)
    # Structural provenance travels WITH the finding (not just its DB row) so
    # the consensus engine can discount correlated fallback voters.
    _attach_provenance(payload)
    return payload


def _attach_provenance(payload: FindingPayload) -> None:
    """Stamp provenance onto a finding's metadata (idempotent)."""
    payload.metadata.setdefault(
        "provenance",
        {
            "source_type": _source_type(payload.agent_name),
            "codepath": _codepath(payload.agent_name),
            "fallback_used": _fallback_used(payload),
            "execution_ms": round(float(payload.latency_s or 0.0) * 1000, 2),
            "model": payload.metadata.get("model")
            or _model_for(payload.agent_name),
        },
    )


async def _persist_finding(incident_id: str, payload: FindingPayload) -> None:
    """Persist a finding to PostgreSQL (best-effort)."""
    try:
        from database.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            _attach_provenance(payload)
            meta = dict(payload.metadata or {})
            # Preserve the semantic claim separately from generic prose so a
            # later confirmed resolution can grade the agent against ground
            # truth. If an agent omitted root_cause_hint but emitted
            # hypotheses, the first hypothesis is the explicit equivalent.
            hint = payload.root_cause_hint
            if not hint and payload.hypotheses:
                hint = payload.hypotheses[0]
            if hint:
                meta.setdefault("root_cause_hint", hint)
            meta.setdefault("hypotheses", list(payload.hypotheses or []))
            # Structural provenance (P1#18/19) is set by ``_attach_provenance``
            # on the live payload, so it is already inside ``meta``.
            await add_finding(
                session,
                incident_id=incident_id,
                agent_name=payload.agent_name,
                finding_type=payload.finding_type,
                description=payload.description,
                confidence=payload.confidence,
                evidence=payload.evidence,
                metadata_=meta,
            )
            await session.commit()
    except Exception as exc:
        logger.warning("persist_finding_failed", agent=payload.agent_name, error=repr(exc))


_EXTERNAL_AGENTS = frozenset({
    "llm_analyzer",
    "log_analyzer",
    "historical_analyzer",
    "knowledge_graph_analyzer",
})


def _source_type(agent_name: str) -> str:
    """Classify the provenance source of an agent's finding.

    ``external`` means it consulted an LLM / Neo4j / memory (independent
    signal); ``rule`` means deterministic local heuristics.
    """
    return "external" if agent_name in _EXTERNAL_AGENTS else "rule"


def _codepath(agent_name: str) -> str:
    """Identify the code path an agent uses so correlated results can be
    de-duplicated (two agents running the same fallback aren't independent)."""
    from agents.registry import get_agent_class

    cls = get_agent_class(agent_name)
    requires_external = bool(cls and getattr(cls, "requires_external", False))
    return f"agents/{agent_name}:{'external' if requires_external else 'local'}"


def _model_for(agent_name: str) -> Optional[str]:
    if agent_name in {"llm_analyzer", "log_analyzer"}:
        try:
            from config.settings import settings

            return settings.ollama_model
        except Exception:
            return None
    return None


def _fallback_used(payload: FindingPayload) -> bool:
    """True when an agent fell back to rule-based heuristics (correlated)."""
    if payload.finding_type == "error":
        return True
    evidence = payload.evidence or {}
    if isinstance(evidence.get("raw_response"), dict) and (
        isinstance(evidence["raw_response"].get("source"), str)
        and "fallback" in evidence["raw_response"]["source"]
    ):
        return True
    meta = payload.metadata or {}
    return bool(meta.get("fallback_used"))


async def _update_agent_invocation(agent_name: str, latency: float, confidence: float, tenant_id: Optional[str]) -> None:
    """Track invocation telemetry (count, last run, observed latency) for an agent.

    Confidence is deliberately NOT written here: it is unverified model
    output.  Trusted confidence_avg is only updated when grading against
    confirmed ground truth (learning.continuous_learning).
    """
    if not tenant_id:
        raise ValueError("Agent invocation telemetry requires an explicit tenant_id")
    try:
        from database.session import AsyncSessionLocal
        import datetime as _dt

        async with AsyncSessionLocal() as session:
            ar = await upsert_agent_reliability(
                session,
                agent_name,
                tenant_id=tenant_id,
                latency_avg=latency,
            )
            # Ensure the row has a stable identity before mutating it
            await session.flush()
            ar.invocations = (ar.invocations or 0) + 1
            ar.last_invocation = _dt.datetime.now(_dt.timezone.utc)
            await session.commit()
    except Exception as exc:
        logger.warning("update_invocation_failed", agent=agent_name, error=repr(exc))


async def execute_action(
    agent_name: str,
    context: Dict[str, Any],
    incident_id: str,
    reliability: float = 0.5,
    persist: bool = True,
) -> FindingPayload:
    """Execute ONE single agent/action, adjust confidence by reliability, and optionally persist."""
    from agents.registry import get_agent_class

    agent_cls = get_agent_class(agent_name)
    if not agent_cls:
        raise ValueError(f"Agent '{agent_name}' is not registered.")

    agent = agent_cls()
    finding = await _run_one(agent, context, reliability)

    if persist:
        await asyncio.gather(
            _persist_finding(incident_id, finding),
            _update_agent_invocation(finding.agent_name, finding.latency_s, finding.confidence, context.get("tenant_id")),
            return_exceptions=True,
        )

    return finding


def _build_evidence_package(findings: List[FindingPayload]) -> str:
    """Build a bounded, structured evidence package for the reasoning agent."""
    parts: List[str] = []
    for finding in findings:
        evidence = finding.evidence or {}
        parts.append(
            f"Agent: {finding.agent_name}\\n"
            f"Type: {finding.finding_type}\\n"
            f"Confidence: {finding.confidence:.3f}\\n"
            f"Finding: {finding.description[:1200]}\\n"
            f"Root-cause hint: {(finding.root_cause_hint or 'none')[:500]}\\n"
            f"Evidence: {str(evidence)[:1800]}"
        )
    return "\\n\\n".join(parts)[:12000]


async def orchestrate(
    incident_id: str,
    incident_type: Optional[str],
    context: Dict[str, Any],
    *,
    force_agents: Optional[List[str]] = None,
    skip_agents: Optional[List[str]] = None,
    persist: bool = True,
) -> OrchestrationResult:
    """
    Run the adaptive orchestrator.

    Returns an :class:`OrchestrationResult` containing findings from
    every activated agent.
    """
    start = time.perf_counter()

    selection: AgentSelection = await select_agents(
        incident_type=incident_type,
        context=context,
        force_agents=force_agents,
        skip_agents=skip_agents,
    )

    logger.info(
        "orchestrator_selection",
        incident_id=incident_id,
        selected=[a.name for a in selection.selected],
        skipped=selection.skipped,
    )

    if not selection.selected:
        logger.warning("orchestrator_no_agents", incident_id=incident_id)
        return OrchestrationResult(
            incident_id=incident_id,
            agents_used=[],
            agents_skipped=[{"agent": n, "reason": r} for n, r in selection.skipped],
            reliability_scores=selection.reliability_scores,
            duration_s=time.perf_counter() - start,
        )

    # Specialist agents produce the evidence first. The LLM reasoning agent
    # is deliberately downstream of that package so it synthesizes evidence
    # instead of acting as a second, opaque evidence collector.
    llm_agents = [a for a in selection.selected if a.name == "llm_analyzer"]
    specialist_agents = [a for a in selection.selected if a.name != "llm_analyzer"]

    findings: List[FindingPayload] = []
    if specialist_agents:
        specialist_tasks = [
            _run_one(agent, context, selection.reliability_scores.get(agent.name, 0.5))
            for agent in specialist_agents
        ]
        findings.extend(await asyncio.gather(*specialist_tasks, return_exceptions=False))

    if llm_agents:
        reasoning_context = dict(context)
        reasoning_context["_evidence_package"] = _build_evidence_package(findings)
        llm_tasks = [
            _run_one(agent, reasoning_context, selection.reliability_scores.get(agent.name, 0.5))
            for agent in llm_agents
        ]
        findings.extend(await asyncio.gather(*llm_tasks, return_exceptions=False))

    # Persist + update invocation counters (best-effort, parallel)
    if persist:
        persist_tasks: List[Any] = []
        for f in findings:
            persist_tasks.append(_persist_finding(incident_id, f))
            persist_tasks.append(
                _update_agent_invocation(
                    f.agent_name,
                    f.latency_s,
                    f.confidence,
                    context.get("tenant_id"),
                )
            )
        await asyncio.gather(*persist_tasks, return_exceptions=True)

    duration = time.perf_counter() - start
    return OrchestrationResult(
        findings=list(findings),
        agents_used=[f.agent_name for f in findings],
        agents_skipped=[{"agent": n, "reason": r} for n, r in selection.skipped],
        reliability_scores=selection.reliability_scores,
        duration_s=duration,
        incident_id=incident_id,
    )


async def execute_multi_agent_fallback(
    incident_id: str,
    incident_type: Optional[str],
    context: Dict[str, Any],
    *,
    persist: bool = True,
) -> OrchestrationResult:
    """Explicitly isolated multi-agent fallback runner.

    Executes all eligible agents concurrently as a fallback mechanism when
    step-by-step investigation loops return empty findings.
    """
    logger.warning("multi_agent_fallback_triggered", incident_id=incident_id, incident_type=incident_type)
    return await orchestrate(
        incident_id=incident_id,
        incident_type=incident_type,
        context=context,
        persist=persist,
    )


