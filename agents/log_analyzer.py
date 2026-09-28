"""Log evidence analysis for WayPoint investigations."""
from __future__ import annotations

from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload
from utils.evidence import CompressedEvidence, compress_logs
from utils.llm import generate_structured

_LOG_SCHEMA = {
    "selected_hypothesis": {"type": str, "default": "", "maxlen": 500},
    "confidence": {"type": float, "min": 0.0, "max": 1.0, "default": 0.0},
    "evidence_ids": {"type": list, "default": []},
}


class LogAnalyzerAgent(BaseAgent):
    """Extract structured log evidence and produce bounded hypotheses.

    The LLM may rank an existing hypothesis, but it cannot manufacture a
    hypothesis, evidence ID, or confirmed root cause.
    """

    name = "log_analyzer"
    supported_incident_types = ["error_rate", "availability", "latency", "data", "security", "*"]
    requires = ["logs"]
    priority = 10
    requires_external = True

    @staticmethod
    def _rule_hypotheses(evidence: CompressedEvidence) -> List[Dict[str, Any]]:
        hypotheses: List[Dict[str, Any]] = []

        def add(name: str, rationale: str, events: List[Any], score: float) -> None:
            ids = list(dict.fromkeys(event.evidence_id for event in events))[:8]
            if ids:
                hypotheses.append(
                    {
                        "hypothesis": name,
                        "rationale": rationale,
                        "evidence_ids": ids,
                        "support_score": min(0.90, max(0.0, score)),
                    }
                )

        critical_events = [e for e in evidence.events if e.line in evidence.critical_events]
        failure_events = [
            e for e in evidence.events
            if e.line in evidence.api_failures
        ]
        deploy_events = [
            e for e in evidence.events
            if any(token in e.line.lower() for token in ("deploy", "deployment", "release"))
        ]
        refused = [e for e in failure_events if "refused" in e.line.lower()]
        timeouts = [e for e in failure_events if "timeout" in e.line.lower()]
        server_errors = [
            e for e in failure_events
            if any(token in e.line.lower() for token in ("500", "503", "5xx"))
        ]
        oom = [
            e for e in critical_events
            if "oom" in e.line.lower() or "out of memory" in e.line.lower()
        ]

        if oom:
            add(
                "Memory exhaustion (OOM) in an affected service.",
                "OOM/error evidence is directly present in the logs.",
                oom,
                0.78,
            )
        if refused and server_errors:
            add(
                "A downstream dependency failure likely contributed to service errors.",
                "Connection refusal precedes HTTP 5xx evidence in the supplied logs.",
                refused + server_errors,
                0.76,
            )
        if timeouts:
            add(
                "An upstream dependency timeout likely contributed to the incident.",
                "Timeout events are directly observed in the supplied logs.",
                timeouts,
                0.68,
            )
        if deploy_events and (critical_events or server_errors):
            add(
                "A recent deployment may have introduced a regression.",
                "A deployment marker is temporally near error evidence.",
                deploy_events + critical_events + server_errors,
                0.62,
            )
        if server_errors:
            add(
                "The application is returning internal server errors.",
                "HTTP 5xx responses are directly observed.",
                server_errors,
                0.60,
            )
        return hypotheses

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        logs: List[str] = context.get("logs", [])
        if not logs:
            return FindingPayload(
                agent_name=self.name,
                finding_type="empty",
                description="No logs provided to LogAnalyzerAgent.",
                confidence=0.1,
                metadata={"epistemic_status": "insufficient_evidence"},
            )

        evidence = compress_logs(logs)
        rule_hypotheses = self._rule_hypotheses(evidence)
        hypotheses = [item["hypothesis"] for item in rule_hypotheses]

        selected = rule_hypotheses[0] if rule_hypotheses else None
        llm_selected: str | None = None
        llm_evidence_ids: List[str] = []
        llm_confidence = 0.0
        if rule_hypotheses:
            try:
                parsed = await generate_structured(
                    "Select the strongest hypothesis from the supplied list. "
                    "Do not invent hypotheses or evidence IDs. Return only a JSON object.\n"
                    f"HYPOTHESES: {rule_hypotheses}",
                    system=(
                        "You are an SRE reviewer. A hypothesis is an inference, not confirmed truth. "
                        "Only select an exact supplied hypothesis and cite only evidence IDs belonging "
                        "to that exact supplied hypothesis. Never claim confirmation."
                    ),
                    schema=_LOG_SCHEMA,
                    evidence=evidence.to_prompt(max_chars=3500),
                )
                if parsed:
                    candidate = str(parsed.get("selected_hypothesis") or "")
                    valid = next(
                        (item for item in rule_hypotheses if item["hypothesis"] == candidate),
                        None,
                    )
                    if valid:
                        allowed_ids = set(valid["evidence_ids"])
                        cited = [
                            str(value)
                            for value in parsed.get("evidence_ids", [])
                            if str(value) in allowed_ids
                        ]
                        # The model must cite at least one piece of evidence
                        # belonging to the selected hypothesis. Otherwise its
                        # selection is advisory only and the deterministic rule
                        # result remains authoritative.
                        if cited:
                            selected = valid
                            llm_selected = candidate
                            llm_evidence_ids = list(dict.fromkeys(cited))[:8]
                            llm_confidence = min(float(parsed.get("confidence", 0.0)), 0.90)
            except Exception:
                # Rule-based evidence remains authoritative when the optional LLM fails.
                pass

        confidence = selected["support_score"] if selected else 0.15
        if llm_selected and llm_confidence > 0.0:
            confidence = min(confidence, llm_confidence, 0.90)

        root_cause_hint = selected["hypothesis"] if selected else None
        supporting_ids = llm_evidence_ids or (selected["evidence_ids"] if selected else [])
        evidence_payload = {
            "events": [
                {
                    "evidence_id": event.evidence_id,
                    "timestamp": event.timestamp,
                    "severity": event.severity,
                    "severity_score": event.severity_score,
                    "services": event.services,
                    "apis": event.apis,
                    "excerpt": event.line,
                }
                for event in evidence.events[:100]
            ],
            "critical_events": evidence.critical_events,
            "anomalies": evidence.anomalies,
            "api_failures": evidence.api_failures,
            "rare_events": evidence.rare_events,
            "services": evidence.services,
            "apis": evidence.apis,
            "pattern_counts": dict(list(evidence.pattern_counts.items())[:20]),
            "temporal_correlations": evidence.temporal_correlations,
            "supporting_evidence_ids": supporting_ids,
            "epistemic_status": "inference" if selected else "insufficient_evidence",
            "llm_selected": llm_selected,
        }

        return FindingPayload(
            agent_name=self.name,
            finding_type="log_analysis",
            description=evidence.summary,
            confidence=confidence,
            evidence=evidence_payload,
            root_cause_hint=root_cause_hint,
            hypotheses=hypotheses[:6],
            metadata={
                "original_lines": evidence.original_line_count,
                "event_count": len(evidence.events),
                "temporal_correlation_count": len(evidence.temporal_correlations),
                "epistemic_status": "inference" if selected else "insufficient_evidence",
            },
        )
