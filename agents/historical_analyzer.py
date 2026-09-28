"""Historical evidence agent.

Historical incidents are contextual evidence only.  Similarity is never
converted into a probability and historical matches cannot by themselves
confirm a root cause.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload


class HistoricalAnalyzerAgent(BaseAgent):
    name = "historical_analyzer"
    supported_incident_types = ["*"]
    requires = ["logs"]
    priority = 15

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        from memory.store import MemoryStore

        logs: List[str] = context.get("logs", [])
        affected: List[str] = list(context.get("affected_services", []))
        tenant_id = context.get("tenant_id")
        query = " ".join(logs[:50]) + " " + " ".join(affected)
        hits = await MemoryStore.get().search(query, top_k=5, tenant_id=tenant_id)

        if not hits:
            return FindingPayload(
                agent_name=self.name,
                finding_type="historical_search",
                description="No similar past incidents found.",
                confidence=0.0,
                evidence={"hits": []},
                hypotheses=[],
                metadata={"provenance": {"source_type": "historical", "codepath": self.name}},
            )

        grouped: Dict[str, List[float]] = {}
        for hit in hits:
            grouped.setdefault(hit.root_cause, []).append(hit.similarity)

        best_cause = max(grouped, key=lambda cause: max(grouped[cause]))
        best_similarity = max(grouped[best_cause])
        # This is a bounded evidence-strength score, not a probability.
        support_score = max(0.0, min(1.0, best_similarity))
        hypotheses = [
            f"Past incident root cause '{hit.root_cause}' (similarity={hit.similarity:.2f})."
            for hit in hits
        ]

        return FindingPayload(
            agent_name=self.name,
            finding_type="historical_search",
            description=(
                f"Found {len(hits)} tenant-scoped historical match(es). "
                f"Historical evidence points toward '{best_cause}', but does not confirm causality."
            ),
            confidence=support_score,
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
                "support_score": round(support_score, 3),
                "tenant_scoped": True,
            },
            root_cause_hint=best_cause,
            hypotheses=hypotheses,
            metadata={
                "provenance": {
                    "source_type": "historical",
                    "codepath": self.name,
                    "fallback_used": False,
                    "claim_type": "contextual_evidence",
                }
            },
        )
