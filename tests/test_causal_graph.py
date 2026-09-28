"""
tests.test_causal_graph
=======================

Unit tests for the causal graph engine and confidence propagation.
"""
from __future__ import annotations


from causal_graph.engine import CausalGraph, CausalGraphBuilder, CausalNode, CausalEdge
from confidence.engine import propagate


def _build_simple_graph() -> CausalGraph:
    g = CausalGraph()
    g.add_node(CausalNode(id="deploy", kind="event", label="deployment v1.2", confidence=0.7, timestamp=1.0))
    g.add_node(CausalNode(id="bug", kind="event", label="bug introduced", confidence=0.6, timestamp=2.0))
    g.add_node(CausalNode(id="errors", kind="event", label="5xx errors spike", confidence=0.9, timestamp=3.0))
    g.add_node(CausalNode(id="alarm", kind="event", label="pager fires", confidence=0.95, timestamp=4.0))
    g.add_edge(CausalEdge(source="deploy", target="bug", weight=0.9))
    g.add_edge(CausalEdge(source="bug", target="errors", weight=0.8))
    g.add_edge(CausalEdge(source="errors", target="alarm", weight=1.0))
    return g


def test_causal_graph_root_cause():
    g = _build_simple_graph()
    candidates = g.find_root_causes(top_k=3)
    assert candidates, "expected at least one root-cause candidate"
    # The deployment node should be the highest-scored root cause
    assert candidates[0]["node_id"] == "deploy"


def test_causal_chain_to_leaf():
    g = _build_simple_graph()
    chain = g.causal_chain_to("alarm")
    assert chain, "expected a non-empty chain"
    assert chain[0]["node_id"] == "deploy"
    assert chain[-1]["node_id"] == "alarm"


def test_confidence_propagation():
    g = _build_simple_graph()
    result = propagate(g, iterations=10)
    assert result.iterations >= 1
    # The "deploy" node should retain / increase its confidence as it
    # is supported by downstream evidence
    deploy_post = result.posteriors["deploy"]
    assert deploy_post >= 0.4


def test_causal_graph_builder_from_findings():
    findings = [
        {
            "agent_name": "log_analyzer",
            "finding_type": "log_analysis",
            "description": "detected OOM in payment-svc",
            "confidence": 0.8,
            "evidence": {"critical_events": ["OOM killed process"], "services": ["payment-svc"]},
            "root_cause_hint": "Memory exhaustion in payment-svc",
        },
    ]
    builder = CausalGraphBuilder()
    g = builder.build_from_findings(
        findings=findings,
        affected_services=["payment-svc"],
        logs=["2024-01-01 payment-svc ERROR OOM killed process"],
    )
    candidates = g.find_root_causes(top_k=3)
    assert candidates
    # Should mention payment-svc in some node label
    all_labels = " ".join(str(c.get("label", "")) for c in candidates)
    assert "payment-svc" in all_labels or "Memory" in all_labels
