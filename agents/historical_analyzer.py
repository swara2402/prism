"""Historical evidence agent with strict tenant-scoped retrieval."""
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
        if not tenant_id:
            return FindingPayload(
                agent_name=self.name,
                finding_type="degraded",
                description="Historical memory lookup skipped because tenant context is unavailable.",
                confidence=0.0,
                evidence={"reason": "missing_tenant_context"},
                hypotheses=[],
                metadata={"provenance": {"source_type": "system", "codepath": self.name, "status": "degraded"}},
            )

        query = " ".join(logs[:50]) + " " + " ".join(affected)
        hits = await MemoryStore.get().search(query, top_k=5, tenant_id=tenant_id)
        if not hits:
            return FindingPayload(
                agent_name=self.name,
                finding_type="historical_search",
                description="No similar past incidents found in this workspace.",
                confidence=0.0,
                evidence={"hits": [], "tenant_scoped": True},
                hypotheses=[],
            )

        grouped: Dict[str, List[float]] = {}
        for hit in hits:
            grouped.setdefault(hit.root_cause, []).append(hit.similarity)
        best_cause = max(grouped, key=lambda cause: max(grouped[cause]))
        support_score = max(0.0, min(1.0, max(grouped[best_cause])))

        return FindingPayload(
            agent_name=self.name,
            finding_type="historical_search",
            description=(f"Found {len(hits)} tenant-scoped historical match(es). "
                         f"Historical evidence points toward '{best_cause}', but does not confirm causality."),
            confidence=support_score,
            evidence={
                "hits": [{"incident_id": h.incident_id, "similarity": round(h.similarity, 3), "root_cause": h.root_cause, "resolution": h.resolution, "services": h.services} for h in hits],
                "support_score": round(support_score, 3),
                "tenant_scoped": True,
            },
            root_cause_hint=best_cause,
            hypotheses=[f"Past incident root cause '{h.root_cause}' (similarity={h.similarity:.2f})." for h in hits],
            metadata={"provenance": {"source_type": "historical", "codepath": self.name, "fallback_used": False, "claim_type": "contextual_evidence"}},
        )
