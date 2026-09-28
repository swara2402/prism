"""
agents.trace_analyzer
=====================

Inspects distributed-trace spans (list of dicts with ``service``,
``operation``, ``duration_ms``, ``status``, ``parent_span_id``) to
localize slow / failed spans and to identify the critical path.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload


class TraceAnalyzerAgent(BaseAgent):
    """Agent that analyzes distributed traces."""

    name = "trace_analyzer"
    supported_incident_types = ["latency", "error_rate", "availability", "*"]
    requires = ["traces"]
    priority = 30

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        traces: List[Dict[str, Any]] = context.get("traces", []) or []
        if not traces:
            return FindingPayload(
                agent_name=self.name,
                finding_type="empty",
                description="No traces provided.",
                confidence=0.1,
            )

        slow_spans: List[Dict[str, Any]] = []
        error_spans: List[Dict[str, Any]] = []
        service_durations: Dict[str, List[float]] = {}

        for span in traces:
            svc = span.get("service") or span.get("service_name") or "unknown"
            op = span.get("operation") or span.get("operation_name") or "unknown"
            duration = float(span.get("duration_ms", 0) or 0)
            status = str(span.get("status", "ok")).lower()

            service_durations.setdefault(svc, []).append(duration)

            if duration > 500:
                slow_spans.append(
                    {"service": svc, "operation": op, "duration_ms": duration}
                )
            if status in ("error", "failed", "5xx", "timeout"):
                error_spans.append(
                    {"service": svc, "operation": op, "status": status, "duration_ms": duration}
                )

        # Identify slowest service by average duration
        svc_avg = {
            svc: sum(d) / len(d) for svc, d in service_durations.items() if d
        }
        slowest_svc = (
            max(svc_avg.items(), key=lambda x: x[1]) if svc_avg else None
        )

        hypotheses: List[str] = []
        if slowest_svc:
            hypotheses.append(
                f"Slowest service: '{slowest_svc[0]}' (avg={slowest_svc[1]:.0f}ms)."
            )
        if error_spans:
            services_with_errors = {s["service"] for s in error_spans}
            hypotheses.append(
                f"Error spans on services: {', '.join(services_with_errors)}."
            )
        if any("timeout" in str(s.get("status", "")).lower() for s in error_spans):
            hypotheses.append("Trace shows timeout(s) on downstream calls.")
        if any(s.get("duration_ms", 0) > 2000 for s in slow_spans):
            hypotheses.append("Very long span (>2s) detected — possible upstream stall.")

        max_severity = 0.0
        if slow_spans:
            max_severity = max(s["duration_ms"] for s in slow_spans) / 5000.0
        if error_spans:
            max_severity = max(max_severity, 0.7)

        confidence = min(1.0, 0.3 + max_severity * 0.5)
        root_cause_hint = hypotheses[0] if hypotheses else None

        return FindingPayload(
            agent_name=self.name,
            finding_type="trace_analysis",
            description=(
                f"{len(slow_spans)} slow spans, {len(error_spans)} error spans "
                f"across {len(service_durations)} services."
            ),
            confidence=confidence,
            evidence={
                "slow_spans": slow_spans[:20],
                "error_spans": error_spans[:20],
                "service_avg_durations_ms": {k: round(v, 1) for k, v in svc_avg.items()},
                "slowest_service": slowest_svc[0] if slowest_svc else None,
            },
            root_cause_hint=root_cause_hint,
            hypotheses=hypotheses,
        )
