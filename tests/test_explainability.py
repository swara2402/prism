"""
tests.test_explainability
=========================

Unit tests for the explainability engine.
"""
from __future__ import annotations

from causal_graph.engine import CausalGraph, CausalNode, CausalEdge
from confidence.engine import propagate
from consensus.engine import ConsensusResult, AlternativeHypothesis
from explainability.engine import build_explanation


def _make_graph() -> CausalGraph:
    g = CausalGraph()
    g.add_node(CausalNode(id="deploy", kind="event", label="deployment v1.2", confidence=0.7, timestamp=1.0))
    g.add_node(CausalNode(id="bug", kind="event", label="bug introduced", confidence=0.6, timestamp=2.0))
    g.add_node(CausalNode(id="errors", kind="event", label="5xx errors spike", confidence=0.9, timestamp=3.0))
    g.add_edge(CausalEdge(source="deploy", target="bug", weight=0.9))
    g.add_edge(CausalEdge(source="bug", target="errors", weight=0.8))
    return g


def test_build_explanation_assembles_fields():
    g = _make_graph()
    prop = propagate(g, iterations=5)
    consensus = ConsensusResult(
        root_cause="deployment v1.2",
        confidence=0.82,
        alternatives=[
            AlternativeHypothesis(
                cause="bug introduced",
                confidence=0.5,
                evidence=["log_analyzer"],
                voters=["log_analyzer"],
            )
        ],
        explanation="Consensus reached.",
        voter_breakdown={"log_analyzer": {"hint": "deployment", "confidence": 0.8, "reliability": 0.7, "voted_for_winner": True}},
    )
    findings = [
        {
            "agent_name": "log_analyzer",
            "finding_type": "log_analysis",
            "description": "detected deployment marker",
            "confidence": 0.8,
            "evidence": {"critical_events": ["deployment v1.2"]},
        }
    ]
    exp = build_explanation(
        incident_id="inc-1",
        findings=findings,
        consensus=consensus,
        causal_graph=g,
        propagation=prop,
        root_cause_node_id="deploy",
    )
    assert exp.incident_id == "inc-1"
    assert len(exp.evidence_used) == 1
    assert exp.final_explanation.startswith("Root cause: deployment v1.2")
    assert len(exp.alternative_root_causes) == 1
    assert "Causal chain" in exp.graph_reasoning
