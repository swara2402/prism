"""
consensus.engine
================

Consensus Engine.

Combines multiple independent voters into a single root-cause decision:

1. **Rule-based reasoning**      — from `rule_based_analyzer`
2. **LLM reasoning**             — from `llm_analyzer`
3. **Historical Incident Memory** — from `historical_analyzer`
4. **Knowledge Graph**           — from `knowledge_graph_analyzer`
5. **Agent reliability**         — from the `agent_reliability` table

Each voter's contribution is weighted by:

* the voter's own confidence in its hint
* the voter's historical reliability score
* a small bonus when multiple voters converge on the same hint

Output:

* :class:`ConsensusResult` with:

    - ``root_cause`` (string)
    - ``confidence`` (float in [0,1])
    - ``alternatives`` (list of (cause, confidence, evidence) tuples)
    - ``explanation`` (string)
    - ``voter_breakdown`` (dict)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.text import jaccard_similarity, tokenize
from investigation.reliability_store import get_reliability


@dataclass
class VoterOpinion:
    """One voter's opinion on the root cause."""

    voter: str
    root_cause_hint: Optional[str]
    confidence: float
    reliability: float
    evidence: Dict[str, Any] = field(default_factory=dict)
    hypotheses: List[str] = field(default_factory=list)
    # Structural provenance (P1#18/19): two voters reaching the same answer via
    # the *same* fallback codepath are correlated, not independent evidence.
    source_type: str = "rule"
    codepath: str = ""
    fallback_used: bool = False


@dataclass
class AlternativeHypothesis:
    cause: str
    confidence: float
    evidence: List[str]
    voters: List[str]


@dataclass
class ConsensusResult:
    root_cause: str
    confidence: float
    alternatives: List[AlternativeHypothesis]
    explanation: str
    voter_breakdown: Dict[str, Dict[str, Any]]


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _cluster_hints(
    opinions: Sequence[VoterOpinion],
    similarity_threshold: float = 0.4,
) -> List[Tuple[str, List[VoterOpinion]]]:
    """
    Group voter opinions whose root-cause hints are textually similar.

    Returns a list of (representative_hint, [opinions]) clusters.
    """
    clusters: List[Tuple[str, List[VoterOpinion], set[str]]] = []

    for op in opinions:
        if not op.root_cause_hint:
            continue
        op_tokens = set(tokenize(_normalize(op.root_cause_hint)))
        matched = False
        for i, (rep, members, rep_tokens) in enumerate(clusters):
            sim = jaccard_similarity(op_tokens, rep_tokens)
            if sim >= similarity_threshold:
                members.append(op)
                # Update representative tokens (union)
                rep_tokens.update(op_tokens)
                matched = True
                break
        if not matched:
            clusters.append((op.root_cause_hint, [op], set(op_tokens)))

    return [(rep, members) for rep, members, _ in clusters]


# ---------------------------------------------------------------
# Engine
# ---------------------------------------------------------------

async def reach_consensus(
    findings: List[Dict[str, Any]],
    reliability_scores: Optional[Dict[str, float]] = None,
    *,
    incident_id: Optional[str] = None,
    incident_type: Optional[str] = None,
    graph_candidates: Optional[Dict[str, float]] = None,
    quorum_threshold: float = 0.0,
) -> ConsensusResult:

    """
    Combine agent findings into a single root-cause decision.

    Parameters
    ----------
    findings
        List of finding dicts (as produced by
        :meth:`FindingPayload.to_dict`).
    reliability_scores
        Mapping ``agent_name -> reliability``.
    incident_id
        Optional incident ID string.
    incident_type
        Optional incident context/category string for context-aware reliability lookup.
    quorum_threshold
        Minimum total weight required to declare a winner.  If no
        cluster reaches this threshold, the engine returns a
        low-confidence "undetermined" verdict.
    """
    context_key = incident_type or incident_id or "default"

    # Build voter opinions
    opinions: List[VoterOpinion] = []
    for f in findings:
        agent = f.get("agent_name", "unknown")
        meta = f.get("metadata", {}) or {}
        provenance = meta.get("provenance", {}) or {}
        opinions.append(
            VoterOpinion(
                voter=agent,
                root_cause_hint=f.get("root_cause_hint"),
                confidence=float(f.get("confidence", 0.0)),
                reliability=float(
                    (reliability_scores or {}).get(
                        agent, get_reliability(context_key, agent)
                    )
                ),
                evidence=f.get("evidence", {}) or {},
                hypotheses=list(f.get("hypotheses", []) or []),
                source_type=provenance.get("source_type", "rule"),
                codepath=provenance.get("codepath", "") or agent,
                fallback_used=bool(provenance.get("fallback_used", False)),
            )
        )


    # Cluster hints
    clusters = _cluster_hints(opinions)

    # Score each cluster
    cluster_scores: List[Tuple[str, List[VoterOpinion], float]] = []
    for rep, members in clusters:
        # Correlated voters: members that reached the same answer through the
        # same fallback codepath are ONE piece of evidence.  Split their
        # combined weight across the group so N copies do not vote N times.
        fallback_groups: Dict[str, int] = {}
        for m in members:
            if m.fallback_used and m.codepath:
                fallback_groups[m.codepath] = fallback_groups.get(m.codepath, 0) + 1

        total_weight = 0.0
        for m in members:
            branch = m.confidence * (0.4 + 0.6 * m.reliability)
            group = fallback_groups.get(m.codepath, 1)
            total_weight += branch / group if (m.fallback_used and group > 1) else branch
        # Quorum bonus: more INDEPENDENT mechanisms => higher confidence.
        # Correlated voters that share the same fallback codepath count as
        # one piece of evidence (P1#18 structural weakness fix).
        independent_codepaths = {m.codepath for m in members}
        quorum_bonus = min(0.2, 0.05 * (len(independent_codepaths) - 1))
        # Graph confidence bonus
        graph_confidence = 0.0
        if graph_candidates:
            graph_confidence = graph_candidates.get(rep, 0.0)
        graph_bonus = 0.15 * graph_confidence
        score = min(1.0, total_weight + quorum_bonus + graph_bonus)
        cluster_scores.append((rep, members, score))

    # Sort by score desc
    cluster_scores.sort(key=lambda x: -x[2])

    if not cluster_scores or cluster_scores[0][2] < quorum_threshold:
        return ConsensusResult(
            root_cause="undetermined",
            confidence=0.1,
            alternatives=[],
            explanation=(
                "Consensus engine could not reach a quorum. "
                "Insufficient or conflicting evidence from voters."
            ),
            voter_breakdown={
                op.voter: {
                    "hint": op.root_cause_hint,
                    "confidence": op.confidence,
                    "reliability": op.reliability,
                    "source_type": op.source_type,
                    "codepath": op.codepath,
                    "fallback_used": op.fallback_used,
                }
                for op in opinions
            },
        )

    winner_rep, winner_members, winner_score = cluster_scores[0]
    alternatives: List[AlternativeHypothesis] = []
    for rep, members, score in cluster_scores[1:]:
        alternatives.append(
            AlternativeHypothesis(
                cause=rep,
                confidence=round(score, 3),
                evidence=[
                    f"{m.voter} (conf={m.confidence:.2f}, rel={m.reliability:.2f})"
                    for m in members
                ],
                voters=[m.voter for m in members],
            )
        )

    voter_breakdown: Dict[str, Dict[str, Any]] = {}
    for op in opinions:
        in_winner = op in winner_members
        voter_breakdown[op.voter] = {
            "hint": op.root_cause_hint,
            "confidence": round(op.confidence, 3),
            "reliability": round(op.reliability, 3),
            "voted_for_winner": in_winner,
            "source_type": op.source_type,
            "codepath": op.codepath,
            "fallback_used": op.fallback_used,
        }

    explanation_lines: List[str] = [
        f"Consensus root cause: '{winner_rep}'.",
        f"Supported by {len(winner_members)} voter(s): "
        + ", ".join(m.voter for m in winner_members)
        + ".",
        f"Final confidence = {winner_score:.3f} (combined voter confidence * reliability + quorum bonus).",
    ]
    if alternatives:
        explanation_lines.append(
            "Alternative hypotheses: "
            + "; ".join(f"{a.cause} ({a.confidence:.2f})" for a in alternatives[:3])
            + "."
        )

    return ConsensusResult(
        root_cause=winner_rep,
        confidence=round(winner_score, 3),
        alternatives=alternatives,
        explanation=" ".join(explanation_lines),
        voter_breakdown=voter_breakdown,
    )
