"""Abstract base class and execution contract for PRISM agents."""
from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.settings import settings
from investigation.epistemic import mark_inferred


@dataclass
class FindingPayload:
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
        metadata = mark_inferred(self.metadata)
        return {
            "agent_name": self.agent_name,
            "finding_type": self.finding_type,
            "description": self.description,
            "confidence": max(0.0, min(1.0, self.confidence)),
            "evidence": self.evidence,
            "root_cause_hint": self.root_cause_hint,
            "hypotheses": self.hypotheses,
            "latency_s": self.latency_s,
            "metadata": metadata,
        }


class BaseAgent(abc.ABC):
    name: str = "base_agent"
    supported_incident_types: List[str] = ["*"]
    requires: List[str] = []
    priority: int = 100
    requires_external: bool = False

    def __init__(self) -> None:
        self.last_latency = 0.0
        self.last_confidence = 0.0

    def supports(self, incident_type: Optional[str]) -> bool:
        if "*" in self.supported_incident_types or not incident_type:
            return True
        return incident_type.lower() in [t.lower() for t in self.supported_incident_types]

    def can_run(self, context: Dict[str, Any]) -> bool:
        return all(context.get(k) for k in self.requires)

    async def run(self, context: Dict[str, Any]) -> FindingPayload:
        """Run an agent with a hard deadline and explicit degraded status."""
        start = time.perf_counter()
        try:
            payload = await asyncio.wait_for(
                self.investigate(context), timeout=max(0.1, settings.agent_timeout_seconds)
            )
        except asyncio.TimeoutError:
            payload = FindingPayload(
                agent_name=self.name,
                finding_type="timeout",
                description=f"Agent exceeded its {settings.agent_timeout_seconds:.1f}s execution deadline.",
                confidence=0.0,
                evidence={"timeout_seconds": settings.agent_timeout_seconds},
                metadata={"provenance": {"source_type": "system", "codepath": self.name, "status": "timed_out"}},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            payload = FindingPayload(
                agent_name=self.name,
                finding_type="error",
                description="Agent execution failed.",
                confidence=0.0,
                evidence={"error_type": type(exc).__name__},
                metadata={"provenance": {"source_type": "system", "codepath": self.name, "status": "failed"}},
            )
        payload.latency_s = time.perf_counter() - start
        payload.agent_name = self.name
        payload.confidence = max(0.0, min(1.0, payload.confidence))
        self.last_latency = payload.latency_s
        self.last_confidence = payload.confidence
        return payload

    @abc.abstractmethod
    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        raise NotImplementedError

    def adjust_confidence(self, raw_confidence: float, reliability: float) -> float:
        reliability = max(settings.agent_min_reliability, min(1.0, reliability))
        return max(0.0, min(1.0, raw_confidence * (0.5 + 0.5 * reliability)))

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} priority={self.priority}>"
