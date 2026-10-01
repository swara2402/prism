"""
agents.knowledge_graph_analyzer
===============================

Queries the Neo4j enterprise knowledge graph for related services,
APIs, recent changes, dependencies, and historical root causes linked
to the affected services.  Falls back to an in-memory subgraph if
Neo4j is unreachable.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload
from config.settings import settings


class KnowledgeGraphAnalyzerAgent(BaseAgent):
    """Agent that consults the enterprise knowledge graph."""

    name = "knowledge_graph_analyzer"
    supported_incident_types = ["*"]
    requires = ["affected_services"]
    priority = 25
    requires_external = True

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        affected: List[str] = list(context.get("affected_services", []))
        if not affected:
            return FindingPayload(
                agent_name=self.name,
                finding_type="kg_search",
                description="No affected services provided; KG query skipped.",
                confidence=0.1,
                evidence={},
            )

        # Do not silently use the process-global in-memory fallback in a
        # multi-tenant production deployment. Until the KG backend is explicitly
        # enabled and tenant-namespaced, report a degraded signal instead.
        if not settings.enable_neo4j:
            return FindingPayload(
                agent_name=self.name,
                finding_type="degraded",
                description="Knowledge graph is disabled in this deployment; no cross-tenant fallback graph is consulted.",
                confidence=0.0,
                evidence={"backend": "disabled"},
                metadata={"provenance": {"source_type": "system", "codepath": self.name, "status": "disabled"}},
            )
        # Lazy import so the agent can run even if Neo4j driver is missing.
        try:
            from knowledge_graph.store import KnowledgeGraphStore

            store = KnowledgeGraphStore.get()
            subgraph = await store.query_service_subgraph(affected)
        except Exception as exc:  # pragma: no cover
            subgraph = {
                "nodes": [{"id": s, "type": "service"} for s in affected],
                "edges": [],
                "recent_changes": [],
                "historical_root_causes": [],
                "error": repr(exc),
            }

        hypotheses: List[str] = []
        recent_changes: List[Dict[str, Any]] = subgraph.get("recent_changes", [])
        historical_causes: List[Dict[str, Any]] = subgraph.get(
            "historical_root_causes", []
        )

        if recent_changes:
            for ch in recent_changes[:3]:
                hypotheses.append(
                    f"Recent change on '{ch.get('service')}' ({ch.get('change_type')}) "
                    f"at {ch.get('timestamp')}."
                )
        if historical_causes:
            for hc in historical_causes[:3]:
                hypotheses.append(
                    f"Service '{hc.get('service')}' previously failed due to: "
                    f"{hc.get('root_cause')}."
                )

        confidence = min(1.0, 0.2 + 0.2 * len(recent_changes) + 0.2 * len(historical_causes))
        root_cause_hint = hypotheses[0] if hypotheses else None

        return FindingPayload(
            agent_name=self.name,
            finding_type="kg_search",
            description=(
                f"KG returned {len(subgraph.get('nodes', []))} nodes, "
                f"{len(subgraph.get('edges', []))} edges, "
                f"{len(recent_changes)} recent changes."
            ),
            confidence=confidence,
            evidence=subgraph,
            root_cause_hint=root_cause_hint,
            hypotheses=hypotheses,
        )
