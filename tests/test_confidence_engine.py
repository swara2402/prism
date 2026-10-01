from __future__ import annotations

from causal_graph.engine import CausalEdge, CausalGraph, CausalNode
from confidence.engine import MAX_PROPAGATED_CONFIDENCE, propagate


def test_propagated_confidence_is_bounded() -> None:
    graph = CausalGraph()
    graph.add_node(CausalNode(id="root", kind="finding", label="root", confidence=1.0))
    graph.add_node(CausalNode(id="a", kind="finding", label="a", confidence=1.0))
    graph.add_node(CausalNode(id="b", kind="finding", label="b", confidence=1.0))
    graph.add_node(CausalNode(id="c", kind="finding", label="c", confidence=1.0))
    graph.add_edge(CausalEdge(source="root", target="a", weight=1.0))
    graph.add_edge(CausalEdge(source="root", target="b", weight=1.0))
    graph.add_edge(CausalEdge(source="root", target="c", weight=1.0))

    result = propagate(graph, iterations=20)

    assert result.posteriors
    assert all(0.0 <= value <= MAX_PROPAGATED_CONFIDENCE for value in result.posteriors.values())
    assert result.posteriors["root"] <= MAX_PROPAGATED_CONFIDENCE


def test_non_finite_prior_does_not_escape_bounds() -> None:
    graph = CausalGraph()
    graph.add_node(CausalNode(id="nan", kind="finding", label="nan", confidence=float("nan")))
    graph.add_node(CausalNode(id="inf", kind="finding", label="inf", confidence=float("inf")))

    result = propagate(graph)

    assert result.posteriors["nan"] == 0.0
    assert result.posteriors["inf"] == 0.0
