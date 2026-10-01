"""Consensus engine with explicit quorum and support-score semantics.

The value historically exposed as ``confidence`` is a *support score*, not a
calibrated probability.  Consensus requires the configured minimum number of
independent voters and the configured support threshold before an RCA can be
declared.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.settings import settings
from investigation.reliability_store import get_reliability
from utils.text import jaccard_similarity, tokenize


@dataclass
class VoterOpinion:
    voter: str
    root_cause_hint: Optional[str]
    confidence: float
    reliability: float
    evidence: Dict[str, Any] = field(default_factory=dict)
    hypotheses: List[str] = field(default_factory=list)
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


def _finite_unit(value: Any) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, min(1.0, value))


def _normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s)


def _cluster_hints(
    opinions: Sequence[VoterOpinion], similarity_threshold: float = 0.3
) -> List[Tuple[str, List[VoterOpinion]]]:
    clusters: List[Tuple[str, List[VoterOpinion], set[str]]] = []
    for op in opinions:
        if not op.root_cause_hint:
            continue
        tokens = set(tokenize(_normalize(op.root_cause_hint)))
        matched = False
        for _, (rep, members, rep_tokens) in enumerate(clusters):
            if jaccard_similarity(tokens, rep_tokens) >= similarity_threshold:
                members.append(op)
                rep_tokens.update(tokens)
                matched = True
                break
        if not matched:
            clusters.append((op.root_cause_hint, [op], tokens))
    return [(rep, members) for rep, members, _ in clusters]


def _independent_members(members: Sequence[VoterOpinion]) -> List[VoterOpinion]:
    """Collapse duplicate findings from the same voter/codepath."""
    selected: Dict[tuple[str, str], VoterOpinion] = {}
    for member in members:
        key = (member.voter, member.codepath or member.voter)
        current = selected.get(key)
        if current is None or member.confidence > current.confidence:
            selected[key] = member
    return list(selected.values())


async def reach_consensus(
    findings: List[Dict[str, Any]],
    reliability_scores: Optional[Dict[str, float]] = None,
    *,
    incident_id: Optional[str] = None,
    incident_type: Optional[str] = None,
    graph_candidates: Optional[Dict[str, float]] = None,
    quorum_threshold: Optional[float] = None,
    min_voters: Optional[int] = None,
) -> ConsensusResult:
    """Return an RCA only when quorum and support thresholds are satisfied."""
    context_key = incident_type or incident_id or "default"
    threshold = settings.consensus_confidence_threshold if quorum_threshold is None else float(quorum_threshold)
    required_voters = settings.consensus_min_voters if min_voters is None else int(min_voters)
    required_voters = max(1, required_voters)

    opinions: List[VoterOpinion] = []
    for f in findings:
        meta = f.get("metadata", {}) or {}
        provenance = meta.get("provenance", {}) or {}
        opinions.append(
            VoterOpinion(
                voter=str(f.get("agent_name", "unknown")),
                root_cause_hint=f.get("root_cause_hint"),
                confidence=_finite_unit(f.get("confidence", 0.0)),
                reliability=_finite_unit((reliability_scores or {}).get(
                    str(f.get("agent_name", "unknown")),
                    get_reliability(context_key, str(f.get("agent_name", "unknown"))),
                )),
                evidence=f.get("evidence", {}) or {},
                hypotheses=list(f.get("hypotheses", []) or []),
                source_type=provenance.get("source_type", "rule"),
                codepath=provenance.get("codepath", "") or str(f.get("agent_name", "unknown")),
                fallback_used=bool(provenance.get("fallback_used", False)),
            )
        )

    clusters = _cluster_hints(opinions)
    cluster_scores: List[Tuple[str, List[VoterOpinion], float]] = []
    for rep, members in clusters:
        independent = _independent_members(members)
        fallback_groups: Dict[str, int] = {}
        for member in independent:
            if member.fallback_used and member.codepath:
                fallback_groups[member.codepath] = fallback_groups.get(member.codepath, 0) + 1

        total = 0.0
        for member in independent:
            branch = member.confidence * (0.4 + 0.6 * member.reliability)
            group = fallback_groups.get(member.codepath, 1)
            total += branch / group if group > 1 else branch

        codepaths = {m.codepath for m in independent}
        quorum_bonus = min(0.2, 0.05 * max(0, len(codepaths) - 1))
        # Graph labels are display text and may differ only by case/spacing from
        # an agent hint. Normalize the lookup so graph evidence actually
        # participates in consensus when the semantics match.
        graph_scores = graph_candidates or {}
        graph_score = float(graph_scores.get(rep, 0.0))
        if graph_score == 0.0:
            normalized_rep = _normalize(rep)
            graph_score = max(
                (_finite_unit(score) for label, score in graph_scores.items()
                 if _normalize(str(label)) == normalized_rep),
                default=0.0,
            )
        graph_bonus = 0.15 * max(0.0, min(1.0, graph_score))
        score = min(1.0, total + quorum_bonus + graph_bonus)
        cluster_scores.append((rep, independent, score))

    cluster_scores.sort(key=lambda item: -item[2])
    winner = cluster_scores[0] if cluster_scores else None

    def breakdown() -> Dict[str, Dict[str, Any]]:
        return {
            op.voter: {
                "hint": op.root_cause_hint,
                "support_score": round(op.confidence, 3),
                "reliability": round(op.reliability, 3),
                "source_type": op.source_type,
                "codepath": op.codepath,
                "fallback_used": op.fallback_used,
            }
            for op in opinions
        }

    if winner is None:
        return ConsensusResult("undetermined", 0.0, [], "No candidate hypothesis was produced.", breakdown())

    winner_rep, winner_members, winner_score = winner
    if len(winner_members) < required_voters or winner_score < threshold:
        alternatives = [
            AlternativeHypothesis(
                cause=rep,
                confidence=round(score, 3),
                evidence=[f"{m.voter} (support={m.confidence:.2f}, rel={m.reliability:.2f})" for m in members],
                voters=[m.voter for m in members],
            )
            for rep, members, score in cluster_scores[1:]
        ]
        return ConsensusResult(
            root_cause="undetermined",
            confidence=round(winner_score, 3),
            alternatives=alternatives,
            explanation=(
                f"No consensus: required {required_voters} independent voter(s) "
                f"and support score >= {threshold:.2f}; received {len(winner_members)} "
                f"voter(s) with support score {winner_score:.3f}."
            ),
            voter_breakdown=breakdown(),
        )

    alternatives = [
        AlternativeHypothesis(
            cause=rep,
            confidence=round(score, 3),
            evidence=[f"{m.voter} (support={m.confidence:.2f}, rel={m.reliability:.2f})" for m in members],
            voters=[m.voter for m in members],
        )
        for rep, members, score in cluster_scores[1:]
    ]

    voter_breakdown = breakdown()
    for op in opinions:
        voter_breakdown[op.voter]["voted_for_winner"] = op in winner_members

    explanation = (
        f"Consensus candidate '{winner_rep}' reached quorum with {len(winner_members)} "
        f"independent voter(s). Support score={winner_score:.3f}. "
        "This score is heuristic support, not a calibrated probability."
    )
    return ConsensusResult(
        root_cause=winner_rep,
        confidence=round(winner_score, 3),
        alternatives=alternatives,
        explanation=explanation,
        voter_breakdown=voter_breakdown,
    )
