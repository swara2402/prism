"""
agents.log_analyzer
===================

Analyzes raw log lines to detect anomalies, error spikes, OOM,
timeouts, deployment markers, and rare-event bursts.

Uses both rule-based heuristics (always available) and an optional
LLM call (when Ollama is reachable) for higher-level summarization.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload
from utils.evidence import compress_logs
from utils.llm import generate_structured
from utils.text import (
    classify_log_severity,
)

_LOG_SCHEMA = {
    "root_cause": {"type": str, "default": "", "maxlen": 500},
    "confidence": {"type": float, "min": 0.0, "max": 1.0, "default": 0.0},
    "hypotheses": {"type": list, "default": []},
}


class LogAnalyzerAgent(BaseAgent):
    """Agent that mines raw logs for error patterns."""

    name = "log_analyzer"
    supported_incident_types = [
        "error_rate",
        "availability",
        "latency",
        "data",
        "security",
        "*",
    ]
    requires = ["logs"]
    priority = 10
    requires_external = True

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        logs: List[str] = context.get("logs", [])
        if not logs:
            return FindingPayload(
                agent_name=self.name,
                finding_type="empty",
                description="No logs provided to LogAnalyzerAgent.",
                confidence=0.1,
            )

        evidence = compress_logs(logs)

        severity_scores: List[float] = []
        for line in logs:
            _, score = classify_log_severity(line)
            severity_scores.append(score)
        avg_severity = sum(severity_scores) / max(1, len(severity_scores))
        max_severity = max(severity_scores) if severity_scores else 0.0

        # Build hypotheses from rule-based signals
        hypotheses: List[str] = []
        if any("oom" in e.lower() or "memory" in e.lower() for e in evidence.critical_events):
            hypotheses.append("Memory exhaustion (OOM) in one of the affected services.")
        if any("timeout" in e.lower() for e in evidence.api_failures):
            hypotheses.append("Upstream dependency timeout causing cascading failures.")
        if any("deployment" in e.lower() or "deploy" in e.lower() for e in evidence.anomalies):
            hypotheses.append("Recent deployment introduced a regression.")
        if any("refused" in e.lower() for e in evidence.api_failures):
            hypotheses.append("Downstream service unavailable (connection refused).")
        if any("5xx" in e or "500" in e or "503" in e for e in evidence.critical_events):
            hypotheses.append("Internal server errors indicating application-level fault.")

        # Try LLM enhancement — via the structured-output trust boundary so
        # log content (a potential prompt-injection vector) is delimited as
        # data and the parsed result is schema-validated + clamped.
        llm_root_cause: str | None = None
        llm_confidence: float | None = None
        try:
            prompt = (
                "You are an SRE investigating a production incident.\n"
                "Given the following compressed evidence, identify the most likely root cause.\n"
                "Return JSON with keys: root_cause, confidence, hypotheses.\n"
            )
            parsed = await generate_structured(
                prompt,
                system="You are a strict JSON-only assistant.",
                schema=_LOG_SCHEMA,
                evidence=evidence.to_prompt(max_chars=3000),
            )
            if parsed:
                llm_root_cause = parsed.get("root_cause") or None
                llm_confidence = float(parsed.get("confidence", 0.0))
                extra_h = parsed.get("hypotheses", [])
                if isinstance(extra_h, list):
                    hypotheses.extend(extra_h)
        except Exception:
            pass

        # Dedup hypotheses preserving order
        seen = set()
        deduped = []
        for h in hypotheses:
            key = h.lower()
            if key not in seen:
                seen.add(key)
                deduped.append(h)
        hypotheses = deduped[:6]

        confidence = llm_confidence if llm_confidence is not None else min(1.0, 0.4 + max_severity * 0.5)
        root_cause_hint = llm_root_cause or (hypotheses[0] if hypotheses else None)

        return FindingPayload(
            agent_name=self.name,
            finding_type="log_analysis",
            description=evidence.summary,
            confidence=confidence,
            evidence={
                "critical_events": evidence.critical_events,
                "anomalies": evidence.anomalies,
                "api_failures": evidence.api_failures,
                "rare_events": evidence.rare_events,
                "services": evidence.services,
                "apis": evidence.apis,
                "pattern_counts": dict(list(evidence.pattern_counts.items())[:20]),
                "compression_ratio": evidence.compression_ratio,
                "max_severity": max_severity,
                "avg_severity": avg_severity,
                "llm_root_cause": llm_root_cause,
            },
            root_cause_hint=root_cause_hint,
            hypotheses=hypotheses,
            metadata={"original_lines": evidence.original_line_count},
        )
