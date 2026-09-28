"""
confidence.engine
=================

Confidence Propagation Engine.

Walks the causal graph and propagates confidence scores from leaves
to roots (and vice-versa) using an iterative message-passing scheme
similar to a simplified belief-propagation.

Rules:

* Each node starts with its own ``confidence`` prior.
* For every edge ``A -> B`` (A causes B), the *downstream* confidence
  of A receives a boost proportional to ``w(B) * edge_weight(B->A)``.
* For every edge ``A -> B``, the *upstream* confidence of B receives
  a small boost from A (because A's existence explains B).
* Multiple converging edges combine via a *noisy-OR*:

    P_combined = 1 - prod_i (1 - p_i * w_i)

* After propagation, each node's *posterior* confidence is a weighted
  blend of its prior + incoming propagated evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import networkx as nx

from causal_graph.engine import CausalGraph


@dataclass
class PropagationResult:
    """Result of a confidence-propagation run."""

    posteriors: Dict[str, float] = field(default_factory=dict)
    contributions: Dict[str, Dict[str, float]] = field(default_factory=dict)
    iterations: int = 0
    converged: bool = False


def _noisy_or(values: List[float]) -> float:
    """Combine probabilities with noisy-OR."""
    prod = 1.0
    for v in values:
        prod *= max(0.0, 1.0 - max(0.0, min(1.0, v)))
    return 1.0 - prod


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

    Parameters
    ----------
    iterations
        Maximum number of message-passing iterations.
    prior_weight
        How much the node's own prior contributes to its posterior.
    propagation_weight
        How much the incoming propagated evidence contributes.
    convergence_threshold
        Stop early if the maximum change in posterior across all nodes
        is below this threshold.
    """
    g: nx.DiGraph = graph.graph
    result = PropagationResult()

    if g.number_of_nodes() == 0:
        return result

    # Initialize posteriors with priors
    posteriors: Dict[str, float] = {
        n: float(g.nodes[n].get("confidence", 0.5)) for n in g.nodes()
    }

    for it in range(iterations):
        new_posteriors: Dict[str, float] = {}
        contributions: Dict[str, Dict[str, float]] = {}

        for node in g.nodes():
            prior = float(g.nodes[node].get("confidence", 0.5))

            # ---- Incoming evidence from predecessors (A -> node) ----
            incoming: List[float] = []
            incoming_contrib: Dict[str, float] = {}
            for pred in g.predecessors(node):
                edge = g.edges[pred, node]
                w = float(edge.get("weight", 1.0))
                pred_post = posteriors[pred]
                contribution = pred_post * w
                incoming.append(contribution)
                incoming_contrib[pred] = contribution

            # ---- Incoming evidence from successors (node -> B) ----
            # If node causes B and B is highly confident, node is more confident.
            for succ in g.successors(node):
                edge = g.edges[node, succ]
                w = float(edge.get("weight", 1.0))
                succ_post = posteriors[succ]
                contribution = succ_post * w * 0.5  # damping
                incoming.append(contribution)
                incoming_contrib[f"~{succ}"] = contribution

            propagated = _noisy_or(incoming) if incoming else prior
            posterior = (
                prior_weight * prior + propagation_weight * propagated
            )
            # Renormalize to [0, 1]
            posterior = max(0.0, min(1.0, posterior))

            new_posteriors[node] = posterior
            contributions[node] = incoming_contrib

        # Check convergence
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
    # Sort descending
    return dict(sorted(contribs.items(), key=lambda x: -x[1]))
