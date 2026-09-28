"""
agents.base
===========

Abstract base class for every investigation agent.

Each agent:

* declares the incident *types* it can handle (used by the orchestrator
  for adaptive activation);
* exposes an async :meth:`investigate` method returning a
  :class:`FindingPayload`;
* records its own latency / confidence for later reliability scoring.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import settings


@dataclass
class FindingPayload:
    """Structured output produced by an agent."""

    agent_name: str
    finding_type: str
    description: str
    confidence: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)
    root_cause_hint: Optional[str] = None
    hypotheses: List[str] = field(default_factory=list)
    latency_s: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "finding_type": self.finding_type,
            "description": self.description,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "root_cause_hint": self.root_cause_hint,
            "hypotheses": self.hypotheses,
            "latency_s": self.latency_s,
            "metadata": self.metadata,
        }


class BaseAgent(abc.ABC):
    """Abstract base class for all investigation agents."""

    #: Display name (must be unique across the registry).
    name: str = "base_agent"

    #: Incident types this agent can handle.  ``["*"]`` means all.
    supported_incident_types: List[str] = ["*"]

    #: Categories of evidence this agent requires (used for activation).
    requires: List[str] = []  # e.g. ["logs", "metrics", "traces", "topology", "history", "kg"]

    #: Order hint (lower runs earlier).
    priority: int = 100

    #: Whether the agent depends on external services (LLM, DB, Neo4j).
    requires_external: bool = False

    def __init__(self) -> None:
        self.last_latency: float = 0.0
        self.last_confidence: float = 0.0

    # ----- Public API -----

    def supports(self, incident_type: Optional[str]) -> bool:
        """Return True if this agent should be activated for ``incident_type``."""
        if "*" in self.supported_incident_types:
            return True
        if not incident_type:
            return True
        return incident_type.lower() in [t.lower() for t in self.supported_incident_types]

    def can_run(self, context: Dict[str, Any]) -> bool:
        """Return True if all required evidence keys are present in ``context``."""
        return all(context.get(k) for k in self.requires)

    async def run(self, context: Dict[str, Any]) -> FindingPayload:
        """Run the agent; record latency & confidence."""
        start = time.perf_counter()
        try:
            payload = await self.investigate(context)
        except Exception as exc:
            payload = FindingPayload(
                agent_name=self.name,
                finding_type="error",
                description=f"Agent failed: {exc!r}",
                confidence=0.0,
                evidence={"exception": repr(exc)},
            )
        payload.latency_s = time.perf_counter() - start
        payload.agent_name = self.name
        self.last_latency = payload.latency_s
        self.last_confidence = payload.confidence
        return payload

    @abc.abstractmethod
    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        """Implement the agent's investigation logic."""
        raise NotImplementedError

    # ----- Reliability-aware confidence -----

    def adjust_confidence(self, raw_confidence: float, reliability: float) -> float:
        """Blend raw confidence with the agent's reliability score."""
        reliability = max(settings.agent_min_reliability, min(1.0, reliability))
        return raw_confidence * (0.5 + 0.5 * reliability)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} priority={self.priority}>"
