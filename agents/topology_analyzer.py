"""
agents.topology_analyzer
========================

Inspects the service topology (dependency graph) to identify blast
radius, critical-path dependencies, and single points of failure.

Expects ``context['topology']`` to be a dict with two keys:

* ``nodes``: list of ``{"id": str, "type": "service"|"api"|"db", ...}``
* ``edges``: list of ``{"source": str, "target": str, "weight": float}``
"""
from __future__ import annotations

from typing import Any, Dict, List

import networkx as nx

from agents.base import BaseAgent, FindingPayload


class TopologyAnalyzerAgent(BaseAgent):
    """Agent that analyzes service topology for blast-radius / SPOF."""

    name = "topology_analyzer"
    supported_incident_types = ["availability", "latency", "error_rate", "*"]
    requires = ["topology"]
    priority = 40

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        topology: Dict[str, Any] = context.get("topology", {}) or {}
        affected: List[str] = list(context.get("affected_services", []) or [])

        nodes = topology.get("nodes", [])
        edges = topology.get("edges", [])

        g = nx.DiGraph()
        for n in nodes:
            g.add_node(n.get("id"), **{k: v for k, v in n.items() if k != "id"})
        for e in edges:
            g.add_edge(
                e.get("source"),
                e.get("target"),
                weight=float(e.get("weight", 1.0)),
            )

        hypotheses: List[str] = []
        blast_radius: List[str] = []
        spof: List[str] = []

        # Blast radius = downstream services reachable from any affected service
        for svc in affected:
            if svc in g:
                reachable = nx.descendants(g, svc)
                blast_radius.extend(reachable)

        blast_radius = list(dict.fromkeys(blast_radius))  # dedup preserve order

        # Single points of failure: nodes with high in-degree and high out-degree
        in_deg = dict(g.in_degree())
        out_deg = dict(g.out_degree())
        for node in g.nodes():
            if in_deg.get(node, 0) >= 2 and out_deg.get(node, 0) >= 2:
                spof.append(node)

        # Identify cut-vertices (articulation points) on undirected projection
        try:
            undirected = g.to_undirected()
            aps = list(nx.articulation_points(undirected))
            spof.extend([n for n in aps if n not in spof])
        except nx.NetworkXError:
            pass

        if blast_radius:
            hypotheses.append(
                f"Blast radius of affected services: {', '.join(blast_radius[:5])}."
            )
        if spof:
            hypotheses.append(
                f"Critical dependency / single point of failure: {', '.join(spof[:3])}."
            )

        # Confidence is higher if blast radius is large (more impact => more certainty)
        confidence = min(1.0, 0.3 + len(blast_radius) * 0.1 + (0.2 if spof else 0.0))
        root_cause_hint = (
            f"Impact centered around critical node(s): {', '.join(spof[:2])}"
            if spof
            else None
        )

        return FindingPayload(
            agent_name=self.name,
            finding_type="topology_analysis",
            description=(
                f"Topology has {g.number_of_nodes()} nodes, "
                f"{g.number_of_edges()} edges. "
                f"Blast radius={len(blast_radius)}, SPOF={len(spof)}."
            ),
            confidence=confidence,
            evidence={
                "blast_radius": blast_radius,
                "single_points_of_failure": spof,
                "affected_services": affected,
                "in_degree": in_deg,
                "out_degree": out_deg,
            },
            root_cause_hint=root_cause_hint,
            hypotheses=hypotheses,
        )
