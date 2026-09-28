"""
explainability.engine
=====================

Explainability Engine.

Generates a human-readable explanation of the investigation by
combining:

* Evidence used (which findings, which logs, which metrics)
* Confidence breakdown (per-voter and per-node contributions)
* Graph reasoning (causal chain from root cause to observed symptoms)
* Alternative root causes (runner-ups from the consensus engine)
* Final narrative explanation
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from causal_graph.engine import CausalGraph
from confidence.engine import PropagationResult, explain_confidence
from consensus.engine import ConsensusResult


@dataclass
class Explanation:
    """Structured explanation of an investigation result."""

    incident_id: str
    evidence_used: List[Dict[str, Any]] = field(default_factory=list)
    confidence_breakdown: Dict[str, float] = field(default_factory=dict)
    graph_reasoning: str = ""
    alternative_root_causes: List[Dict[str, Any]] = field(default_factory=list)
    final_explanation: str = ""


def build_explanation(
    *,
    incident_id: str,
    findings: List[Dict[str, Any]],
    consensus: ConsensusResult,
    causal_graph: CausalGraph,
    propagation: PropagationResult,
    root_cause_node_id: Optional[str] = None,
) -> Explanation:
    """Assemble a structured :class:`Explanation` from the investigation outputs."""

    # ----- Evidence used -----
    evidence_used: List[Dict[str, Any]] = []
    for f in findings:
        evidence_used.append(
            {
                "agent": f.get("agent_name"),
                "finding_type": f.get("finding_type"),
                "description": f.get("description"),
                "confidence": round(float(f.get("confidence", 0.0)), 3),
                "key_evidence": _summarize_evidence(f.get("evidence", {})),
            }
        )

    # ----- Confidence breakdown -----
    confidence_breakdown: Dict[str, float] = dict(consensus.voter_breakdown)
    if root_cause_node_id:
        contribs = explain_confidence(causal_graph, propagation, root_cause_node_id)
        for contributor, value in contribs.items():
            confidence_breakdown[f"graph:{contributor}"] = round(value, 3)

    # ----- Graph reasoning -----
    causal_chain: List[Dict[str, Any]] = []
    if root_cause_node_id:
        causal_chain = causal_graph.causal_chain_to(root_cause_node_id)

    graph_reasoning_parts: List[str] = []
    if causal_chain:
        graph_reasoning_parts.append(
            f"Causal chain (root -> observed symptom) has {len(causal_chain)} step(s):"
        )
        for i, step in enumerate(causal_chain, 1):
            graph_reasoning_parts.append(
                f"  {i}. [{step.get('kind')}] {step.get('label')} "
                f"(confidence={step.get('confidence')})"
            )
    if propagation.iterations:
        graph_reasoning_parts.append(
            f"Confidence propagation converged in {propagation.iterations} "
            f"iteration(s) (converged={propagation.converged})."
        )

    # ----- Alternatives -----
    alternatives = [
        {
            "cause": a.cause,
            "confidence": a.confidence,
            "evidence": a.evidence,
            "voters": a.voters,
        }
        for a in consensus.alternatives
    ]

    # ----- Final narrative -----
    final_parts: List[str] = [
        f"Root cause: {consensus.root_cause}.",
        f"Confidence: {consensus.confidence:.1%}.",
        "",
        consensus.explanation,
        "",
        "Evidence summary:",
    ]
    for ev in evidence_used[:8]:
        final_parts.append(
            f"  - [{ev['agent']}] {ev['description']} (conf={ev['confidence']})"
        )
    if alternatives:
        final_parts.append("")
        final_parts.append("Alternative hypotheses considered:")
        for a in alternatives[:3]:
            final_parts.append(f"  - {a['cause']} (conf={a['confidence']})")
    if causal_chain:
        final_parts.append("")
        final_parts.append("Causal chain (from root to symptom):")
        for step in causal_chain:
            final_parts.append(f"  -> {step.get('label')}")

    return Explanation(
        incident_id=incident_id,
        evidence_used=evidence_used,
        confidence_breakdown=confidence_breakdown,
        graph_reasoning="\n".join(graph_reasoning_parts),
        alternative_root_causes=alternatives,
        final_explanation="\n".join(final_parts),
    )


def _summarize_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Pull a small representative subset out of a possibly huge evidence dict."""
    if not isinstance(evidence, dict):
        return {}
    summary: Dict[str, Any] = {}
    for k in (
        "critical_events",
        "anomalies",
        "api_failures",
        "rare_events",
        "slow_spans",
        "error_spans",
        "blast_radius",
        "single_points_of_failure",
        "hits",
        "matched_rules",
        "recommended_actions",
        "contributing_factors",
    ):
        v = evidence.get(k)
        if v is None:
            continue
        if isinstance(v, list):
            summary[k] = v[:3]
        elif isinstance(v, dict):
            summary[k] = dict(list(v.items())[:3])
        else:
            summary[k] = v
    return summary
