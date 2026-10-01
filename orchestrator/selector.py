"""
orchestrator.selector
=====================

Decides *which* agents to activate for a given incident.  The
selection is adaptive — it depends on:

1. The incident type
2. The evidence that is actually present (logs? metrics? traces? topology?)
3. Each agent's current reliability score (from the database)
4. Each agent's declared requirements & supported incident types

Agents that cannot run (missing evidence) or that have very low
reliability are filtered out so we never execute agents that would
just produce noise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agents.base import BaseAgent
from investigation.state import InvestigationState
from investigation.hypothesis_manager import competition_intensity

from orchestrator.agent_registry import AGENT_REGISTRY
from agents.registry import instantiate_all
from config.settings import settings


@dataclass
class AgentSelection:
    """Result of agent selection."""

    selected: List[BaseAgent]
    skipped: List[Tuple[str, str]]  # (agent_name, reason)
    reliability_scores: Dict[str, float]
    remaining_actions: List[Dict[str, Any]] = field(default_factory=list)


def _evidence_keys(context: Dict[str, Any]) -> Dict[str, bool]:
    """Map of (required-evidence-key -> present?)."""
    return {
        "logs": bool(context.get("logs")),
        "metrics": bool(context.get("metrics")),
        "traces": bool(context.get("traces")),
        "topology": bool(context.get("topology")),
        "history": True,  # always available via memory store
        "kg": True,
        "affected_services": bool(context.get("affected_services")),
    }


async def _load_reliability(agent_names: Sequence[str], tenant_id: Optional[str]) -> Dict[str, float]:
    """Load tenant-authoritative reliability from PostgreSQL."""
    from database.session import AsyncSessionLocal
    from database.repositories import list_agent_reliabilities
    if not tenant_id:
        raise ValueError("Agent selection requires an explicit tenant_id")
    async with AsyncSessionLocal() as session:
        rows = await list_agent_reliabilities(session, tenant_id=tenant_id)
    by_name = {row.agent_name: float(row.reliability_score) for row in rows}
    return {name: max(0.0, min(1.0, by_name.get(name, settings.agent_default_reliability))) for name in agent_names}


async def select_agents(
    incident_type: Optional[str],
    context: Dict[str, Any],
    *,
    min_reliability: Optional[float] = None,
    force_agents: Optional[List[str]] = None,
    skip_agents: Optional[List[str]] = None,
    state: Optional[InvestigationState] = None,
    executed_actions: Optional[List[str]] = None,
) -> AgentSelection:
    """Select agents based on incident type, evidence, reliability, and MDV components.

    The *context* dict must contain an ``incident_id`` and optionally a ``hypotheses`` mapping
    of ``{name: probability}``.
    """
    min_rel = min_reliability if min_reliability is not None else settings.agent_min_reliability
    force_agents = force_agents or []
    skip_agents = list(skip_agents or [])

    # Collect executed actions to enforce repeat protection (Priority 6)
    executed_list: List[str] = list(executed_actions or [])
    if state and hasattr(state, "executed_actions") and state.executed_actions:
        for act in state.executed_actions:
            if isinstance(act, dict):
                aname = act.get("agent_name") or act.get("action")
                if aname:
                    executed_list.append(aname)
            elif isinstance(act, str):
                executed_list.append(act)

    already_executed: set[str] = set(executed_list)

    all_agents = instantiate_all()
    evidence = _evidence_keys(context)
    tenant_id = context.get("tenant_id")
    reliability = await _load_reliability(list(all_agents.keys()), tenant_id)

    selected: List[BaseAgent] = []
    skipped: List[Tuple[str, str]] = []

    # ----- Compute competition intensity from current hypotheses -----
    hypotheses_map: Dict[str, float] = context.get("hypotheses", {})
    if state and hasattr(state, "hypotheses") and state.hypotheses:
        hypotheses_map = state.hypotheses
    sorted_hyp = sorted(hypotheses_map.items(), key=lambda kv: kv[1], reverse=True)
    if len(sorted_hyp) >= 2:
        (h1, p1), (h2, p2) = sorted_hyp[:2]
        competition = competition_intensity(p1, p2)
        top_pair = (h1, h2)
    else:
        competition = 0.0
        top_pair = (None, None)

    # ----- Build raw action list with dynamic scores -----
    raw_actions: List[Dict[str, Any]] = []
    inc_context = incident_type or context.get("incident_type") or "default"

    for cap in AGENT_REGISTRY:
        # Eligibility filtering before adding to raw_actions
        # Repeat protection (Priority 6)
        if cap.agent_name in already_executed and not cap.repeatable:
            skipped.append((cap.agent_name, "non-repeatable action already executed"))
            continue
        # Explicit skip list
        if cap.agent_name in skip_agents:
            skipped.append((cap.agent_name, "explicitly skipped"))
            continue
        # Evidence requirement gate – at least one required evidence present.
        if cap.supported_evidence and not any(evidence.get(ev, False) for ev in cap.supported_evidence):
            continue
        # Incident type support gate (using instantiated agent)
        agent_obj = all_agents.get(cap.agent_name)
        if agent_obj and not agent_obj.supports(incident_type):
            continue
        # Reliability gate – enforce minimum reliability
        rel_score = reliability.get(cap.agent_name, settings.agent_default_reliability)
        if rel_score < min_rel:
            continue

        # Discrimination capability for the current top hypothesis pair.
        can_discriminate = False
        if top_pair[0] is not None:
            for pair in cap.discriminates_between:
                if (pair[0] == top_pair[0] and pair[1] == top_pair[1]) or (
                    pair[0] == top_pair[1] and pair[1] == top_pair[0]
                ):
                    can_discriminate = True
                    break
        discrimination_score = competition * (1.0 if can_discriminate else 0.0)

        # Expected uncertainty reduction via EMA store (using incident_context)
        from investigation.action_effectiveness import get_expected_effectiveness

        expected_red = get_expected_effectiveness(inc_context, cap.agent_name)
        # Apply decay for repeated executions of repeatable actions
        exec_count = executed_list.count(cap.agent_name)
        if exec_count > 0:
            expected_red = expected_red * (0.5 ** exec_count)

        raw_actions.append(
            {
                "agent_name": cap.agent_name,
                "supported_evidence": cap.supported_evidence,
                "discriminates_between": cap.discriminates_between,
                "supported_hypotheses": cap.supported_hypotheses,
                "execution_cost": cap.execution_cost,
                "repeatable": cap.repeatable,
                "discrimination_score": discrimination_score,
                "expected_uncertainty_reduction": expected_red,
                "reliability_score": rel_score,
                "is_supported": True,
            }
        )


    # MDV is the canonical selector. The selected list is a compatibility
    # view only. The adaptive tree executes exactly one top-ranked action.
    selected = [all_agents[a["agent_name"]] for a in raw_actions if a["agent_name"] in all_agents]
    selected.sort(key=lambda a: a.priority)

    # Normalize execution cost to [0, 1]
    max_cost = max((a["execution_cost"] for a in raw_actions), default=1.0)
    for a in raw_actions:
        a["execution_cost"] = a["execution_cost"] / max_cost if max_cost > 0 else 0.0

    # Compute MDV for each action and sort descending
    from investigation.value_engine import compute_mdv, DEFAULT_MDV_WEIGHTS

    for a in raw_actions:
        a["mdv"] = compute_mdv(a, state, DEFAULT_MDV_WEIGHTS)
    raw_actions.sort(key=lambda x: x["mdv"], reverse=True)

    if state is not None and hasattr(state, "remaining_actions"):
        state.remaining_actions = list(raw_actions)

    return AgentSelection(
        selected=selected,
        skipped=skipped,
        reliability_scores=reliability,
        remaining_actions=raw_actions,
    )