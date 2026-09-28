"""
agents.historical_analyzer
==========================

Looks up similar past incidents from :mod:`memory` and surfaces their
root causes / resolutions.  Acts as the bridge between the agent layer
and the incident-memory store.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload


class HistoricalAnalyzerAgent(BaseAgent):
    """Agent that consults past incident memory."""

    name = "historical_analyzer"
    supported_incident_types = ["*"]
    requires = ["logs"]
    priority = 15

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        # Import here to avoid circular import at module load.
        from memory.store import MemoryStore

        logs: List[str] = context.get("logs", [])
        affected: List[str] = list(context.get("affected_services", []))
        query = " ".join(logs[:50]) + " " + " ".join(affected)

        store = MemoryStore.get()
        hits = await store.search(query, top_k=5)

        if not hits:
            return FindingPayload(
                agent_name=self.name,
                finding_type="historical_search",
                description="No similar past incidents found.",
                confidence=0.2,
                evidence={"hits": []},
                hypotheses=[],
            )

        hypotheses: List[str] = []
        weighted_root_causes: Dict[str, float] = {}
        for h in hits:
            cause = h.root_cause
            weighted_root_causes[cause] = (
                weighted_root_causes.get(cause, 0.0) + h.similarity
            )
            hypotheses.append(
                f"Past incident root cause '{cause}' (similarity={h.similarity:.2f})."
            )

        # Pick the most-voted cause
        best_cause = max(weighted_root_causes, key=weighted_root_causes.get) if weighted_root_causes else None
        confidence = (
            min(1.0, weighted_root_causes.get(best_cause, 0.0))
            if best_cause
            else 0.2
        )

        return FindingPayload(
            agent_name=self.name,
            finding_type="historical_search",
            description=(
                f"Found {len(hits)} similar past incidents. "
                f"Top reused root cause: {best_cause or 'n/a'}."
            ),
            confidence=confidence,
            evidence={
                "hits": [
                    {
                        "incident_id": h.incident_id,
                        "similarity": round(h.similarity, 3),
                        "root_cause": h.root_cause,
                        "resolution": h.resolution,
                        "services": h.services,
                    }
                    for h in hits
                ],
                "weighted_root_causes": {
                    k: round(v, 3) for k, v in weighted_root_causes.items()
                },
            },
            root_cause_hint=best_cause,
            hypotheses=hypotheses,
        )
