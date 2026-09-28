"""
agents.llm_analyzer
===================

Deep LLM-based root-cause analyzer.  Uses the compressed evidence
produced by :mod:`utils.evidence` to keep the prompt small and to
avoid sending raw logs directly to the model.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload
from utils.evidence import compress_logs
from utils.llm import generate_structured

_LLM_SCHEMA = {
    "root_cause": {"type": str, "required": True, "default": "unknown", "maxlen": 500},
    "confidence": {"type": float, "required": True, "min": 0.0, "max": 1.0, "default": 0.4},
    "hypotheses": {"type": list, "default": []},
    "contributing_factors": {"type": list, "default": []},
    "recommended_actions": {"type": list, "default": []},
}


class LLMAnalyzerAgent(BaseAgent):
    """LLM-driven root-cause analyzer."""

    name = "llm_analyzer"
    supported_incident_types = ["*"]
    requires = ["logs"]
    priority = 60
    requires_external = True

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        logs: List[str] = context.get("logs", [])
        affected: List[str] = list(context.get("affected_services", []))
        metrics: Dict[str, Any] = context.get("metrics", {}) or {}

        evidence = compress_logs(logs)

        prompt_parts: List[str] = [
            "You are a senior SRE. Investigate the following incident evidence",
            "and return a JSON object with keys: root_cause, confidence (0..1),",
            "hypotheses (list of strings), contributing_factors (list of strings),",
            "and recommended_actions (list of strings).",
            "",
            f"Affected services: {', '.join(affected) or 'unknown'}.",
        ]
        if metrics:
            prompt_parts.append(f"\nMetric summaries: {json.dumps(metrics, default=str)[:500]}")

        prompt = "\n".join(prompt_parts)

        try:
            data = await generate_structured(
                prompt,
                system=(
                    "You are a strict JSON-only assistant. Do not output anything "
                    "other than a single JSON object."
                ),
                schema=_LLM_SCHEMA,
                evidence=evidence.to_prompt(max_chars=3000),
            )
        except Exception as exc:
            return FindingPayload(
                agent_name=self.name,
                finding_type="llm_analysis",
                description=f"LLM analysis failed: {exc!r}",
                confidence=0.1,
                evidence={},
                hypotheses=[],
            )

        if data is None:
            return FindingPayload(
                agent_name=self.name,
                finding_type="llm_analysis",
                description="LLM structured output was rejected (schema violation); "
                            "falling back to rule-based defaults.",
                confidence=0.1,
                evidence={},
                hypotheses=[],
                metadata={"fallback_used": True},
            )

        root_cause = data.get("root_cause", "unknown")
        confidence = float(data.get("confidence", 0.4))
        hypotheses = list(data.get("hypotheses", []) or [])
        contributing = list(data.get("contributing_factors", []) or [])
        actions = list(data.get("recommended_actions", []) or [])

        return FindingPayload(
            agent_name=self.name,
            finding_type="llm_analysis",
            description=f"LLM root cause: {root_cause}",
            confidence=confidence,
            evidence={
                "compressed_evidence_summary": evidence.summary,
                "contributing_factors": contributing,
                "recommended_actions": actions,
                "raw_response": data,
            },
            root_cause_hint=root_cause,
            hypotheses=hypotheses,
        )
