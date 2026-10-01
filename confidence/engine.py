"""
confidence.engine
=================

Confidence Propagation Engine.

Walks the causal graph and propagates confidence scores from leaves
to roots (and vice-versa) using an iterative message-passing scheme
similar to a simplified belief-propagation.

The propagated value is a diagnostic support score, not a calibrated
probability. It is deliberately bounded below 1.0 so repeated graph
propagation cannot silently manufacture certainty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import networkx as nx

from causal_graph.engine import CausalGraph

# A propagation score must never be presented as absolute certainty.
# Confirmation is a separate, explicit workflow state.
MAX_PROPAGATED_CONFIDENCE = 0.95


@dataclass
class PropagationResult:
    """Result of a confidence-propagation run."""

    posteriors: Dict[str, float] = field(default_factory=dict)
    contributions: Dict[str, Dict[str, float]] = field(default_factory=dict)
    iterations: int = 0
    converged: bool = False


def _noisy_or(values: List[float]) -> float:
    """Combine support values with noisy-OR."""
    prod = 1.0
    for v in values:
        prod *= max(0.0, 1.0 - max(0.0, min(1.0, v)))
    return 1.0 - prod


def _bounded_confidence(value: float) -> float:
    """Return a finite confidence support score in [0, MAX_PROPAGATED_CONFIDENCE]."""
    if value != value:  # NaN
        return 0.0
    if value in (float("inf"), float("-inf")):
        # Infinity is invalid input, not evidence of certainty.
        return 0.0
    return max(0.0, min(MAX_PROPAGATED_CONFIDENCE, float(value)))


def propagate(
    graph: CausalGraph,
    *,
    iterations: int = 5,
    prior_weight: float = 0.5,
    propagation_weight: float = 0.5,
    convergence_threshold: float = 1e-3,
) -> PropagationResult:
    """
    Run belief-propagation-style confidence propagation on ``graph``.

    The returned values are support scores. They are intentionally capped
    at ``MAX_PROPAGATED_CONFIDENCE``; only an explicit confirmation workflow
    may represent a root cause as confirmed truth.
    """
    g: nx.DiGraph = graph.graph
    result = PropagationResult()

    if g.number_of_nodes() == 0:
        return result

    posteriors: Dict[str, float] = {
        n: _bounded_confidence(float(g.nodes[n].get("confidence", 0.5)))
        for n in g.nodes()
    }

    for it in range(max(0, iterations)):
        new_posteriors: Dict[str, float] = {}
        contributions: Dict[str, Dict[str, float]] = {}

        for node in g.nodes():
            prior = _bounded_confidence(float(g.nodes[node].get("confidence", 0.5)))

            incoming: List[float] = []
            incoming_contrib: Dict[str, float] = {}

            for pred in g.predecessors(node):
                edge = g.edges[pred, node]
                w = max(0.0, min(1.0, float(edge.get("weight", 1.0))))
                contribution = _bounded_confidence(posteriors[pred] * w)
                incoming.append(contribution)
                incoming_contrib[pred] = contribution

            for succ in g.successors(node):
                edge = g.edges[node, succ]
                w = max(0.0, min(1.0, float(edge.get("weight", 1.0))))
                contribution = _bounded_confidence(posteriors[succ] * w * 0.5)
                incoming.append(contribution)
                incoming_contrib[f"~{succ}"] = contribution

            propagated = _noisy_or(incoming) if incoming else prior
            posterior = prior_weight * prior + propagation_weight * propagated
            new_posteriors[node] = _bounded_confidence(posterior)
            contributions[node] = incoming_contrib

        max_delta = max(
            (abs(new_posteriors[n] - posteriors[n]) for n in g.nodes()),
            default=0.0,
        )
        posteriors = new_posteriors
        result.posteriors = dict(posteriors)
        result.contributions = contributions
        result.iterations = it + 1

        if max_delta < convergence_threshold:
            result.converged = True
            break

    return result


def explain_confidence(
    graph: CausalGraph,
    result: PropagationResult,
    node_id: str,
) -> Dict[str, float]:
    """Return a flat dict of (contributor -> contribution) for a node."""
    contribs = result.contributions.get(node_id, {})
    return dict(sorted(contribs.items(), key=lambda x: -x[1]))
