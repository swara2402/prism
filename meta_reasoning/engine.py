"""
meta_reasoning.engine
=====================

Meta-Reasoning Engine.

After every solved incident this engine evaluates:

* Which agents were useful (contributed to the winning hypothesis)
* Which agents were unnecessary (no signal / low confidence)
* Which reasoning path was optimal (cheapest path that still found
  the correct answer)
* Suggestions to improve future investigations (e.g. "skip
  ``trace_analyzer`` for error-rate incidents — it never contributes")

Persisted into the ``meta_reasoning_records`` table for later analysis.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from config.logging import get_logger
from database.repositories import save_meta_reasoning
from utils.text import jaccard_similarity, tokenize

logger = get_logger(__name__)


@dataclass
class MetaReasoningResult:
    """Output of the meta-reasoning engine."""

    incident_id: str
    useful_agents: List[str] = field(default_factory=list)
    unnecessary_agents: List[str] = field(default_factory=list)
    optimal_path: str = ""
    suggestions: List[str] = field(default_factory=list)
    agent_scores: Dict[str, float] = field(default_factory=dict)


def _agent_score(
    finding: Dict[str, Any],
    final_root_cause: str,
    final_confidence: float,
) -> float:
    """
    Score how useful an agent was.

    A high score requires:

    * The agent's hint is textually similar to the final root cause.
    * The agent had reasonable confidence.
    * The agent did not take an excessive amount of time.
    """
    hint = (finding.get("root_cause_hint") or "").lower()
    conf = float(finding.get("confidence", 0.0))
    latency = float(finding.get("latency_s", 0.0))

    final_tokens = set(tokenize(final_root_cause.lower()))
    hint_tokens = set(tokenize(hint))
    sim = jaccard_similarity(final_tokens, hint_tokens) if hint_tokens else 0.0

    latency_penalty = max(0.0, 1.0 - latency / 10.0)  # >10s => 0 penalty floor
    score = 0.5 * sim + 0.3 * conf + 0.2 * latency_penalty
    return round(score, 3)


def evaluate(
    *,
    incident_id: str,
    findings: List[Dict[str, Any]],
    agents_skipped: List[Dict[str, str]],
    final_root_cause: str,
    final_confidence: float,
    agents_used: List[str],
    persist: bool = True,
) -> MetaReasoningResult:
    """Run the meta-reasoning evaluation for a single incident."""

    useful: List[str] = []
    unnecessary: List[str] = []
    scores: Dict[str, float] = {}

    for f in findings:
        agent = f.get("agent_name", "unknown")
        score = _agent_score(f, final_root_cause, final_confidence)
        scores[agent] = score
        # An agent is "useful" only if BOTH its score is high AND its
        # textual similarity to the final answer is non-trivial.  An
        # agent that confidently gives the wrong answer is penalized.
        hint = (f.get("root_cause_hint") or "").lower()
        hint_tokens = set(tokenize(hint))
        final_tokens = set(tokenize(final_root_cause.lower()))
        sim = jaccard_similarity(final_tokens, hint_tokens) if hint_tokens else 0.0
        if score >= 0.3 and sim >= 0.1:
            useful.append(agent)
        else:
            unnecessary.append(agent)

    # Skipped agents are neither useful nor unnecessary — they weren't run.
    # But we surface them in suggestions.
    skipped_names = [s.get("agent") for s in agents_skipped if s.get("agent")]

    # Optimal path: the subset of useful agents, sorted by priority/latency,
    # that would have been sufficient to reach the same conclusion.
    useful_findings = [f for f in findings if f.get("agent_name") in useful]
    useful_findings.sort(key=lambda f: f.get("latency_s", 0.0))
    optimal_chain = [f["agent_name"] for f in useful_findings]
    optimal_path = " -> ".join(optimal_chain) if optimal_chain else "n/a"

    # Suggestions
    suggestions: List[str] = []
    if unnecessary:
        suggestions.append(
            f"Consider skipping agent(s) {', '.join(unnecessary)} for similar "
            f"incidents — they produced no useful signal."
        )
    if skipped_names:
        suggestions.append(
            f"Skipped agents: {', '.join(skipped_names)}.  Review whether they "
            f"should be enabled for this incident type."
        )
    # Suggest reliability-driven reordering
    sorted_by_score = sorted(scores.items(), key=lambda x: -x[1])
    if len(sorted_by_score) >= 2:
        suggestions.append(
            f"Agent ranking by usefulness: {', '.join(f'{n}({s:.2f})' for n, s in sorted_by_score)}. "
            f"Consider reordering priority accordingly."
        )
    # Low-confidence overall?
    if final_confidence < 0.4:
        suggestions.append(
            "Final confidence was low. Consider adding more evidence sources "
            "or enabling additional agents (e.g. llm_analyzer)."
        )

    result = MetaReasoningResult(
        incident_id=incident_id,
        useful_agents=useful,
        unnecessary_agents=unnecessary,
        optimal_path=optimal_path,
        suggestions=suggestions,
        agent_scores=scores,
    )

    if persist:
        import asyncio

        async def _persist() -> None:
            try:
                from database.session import AsyncSessionLocal

                async with AsyncSessionLocal() as session:
                    await save_meta_reasoning(
                        session,
                        incident_id=incident_id,
                        useful_agents=useful,
                        unnecessary_agents=unnecessary,
                        optimal_path=optimal_path,
                        suggestions=suggestions,
                        agent_scores=scores,
                    )
                    await session.commit()
            except Exception as exc:
                logger.warning("meta_reasoning_persist_failed error=%r", exc)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_persist())
        else:
            if loop.is_running():
                asyncio.ensure_future(_persist())
            else:
                loop.run_until_complete(_persist())

    return result
