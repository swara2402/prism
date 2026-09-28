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

    The LLM can rank evidence-backed hypotheses, but cannot manufacture the
    evidence or turn an inference into confirmed truth.
    """

    name = "log_analyzer"
    supported_incident_types = ["error_rate", "availability", "latency", "data", "security", "*"]
    requires = ["logs"]
    priority = 10
    requires_external = True

    @staticmethod
    def _rule_hypotheses(evidence: CompressedEvidence) -> List[Dict[str, Any]]:
        hypotheses: List[Dict[str, Any]] = []
        event_ids = {e.line: e.evidence_id for e in evidence.events}

        def add(name: str, rationale: str, lines: List[str], score: float) -> None:
            ids = [event_ids[line] for line in lines if line in event_ids]
            if ids:
                hypotheses.append({
                    "hypothesis": name,
                    "rationale": rationale,
                    "evidence_ids": ids[:8],
                    "support_score": min(0.90, max(0.0, score)),
                })

        critical = evidence.critical_events
        failures = evidence.api_failures
        deploys = [e.line for e in evidence.events if any(x in e.line.lower() for x in ("deploy", "deployment", "release"))]
        refused = [line for line in failures if "refused" in line.lower()]
        timeouts = [line for line in failures if "timeout" in line.lower()]
        server_errors = [line for line in failures if any(x in line for x in ("500", "503", "5xx"))]
        oom = [line for line in critical if "oom" in line.lower() or "out of memory" in line.lower()]

        if oom:
            add("Memory exhaustion (OOM) in an affected service.", "OOM/error evidence is present in the logs.", oom, 0.78)
        if refused and server_errors:
            ids = refused + server_errors
            add("A downstream dependency failure likely contributed to service errors.", "Connection refusal precedes HTTP 5xx evidence.", ids, 0.76)
        if timeouts:
            add("An upstream dependency timeout likely contributed to the incident.", "Timeout events are directly observed in the logs.", timeouts, 0.68)
        if deploys and (critical or server_errors):
            add("A recent deployment may have introduced a regression.", "A deployment marker is temporally near error evidence.", deploys + critical + server_errors, 0.62)
        if server_errors:
            add("The application is returning internal server errors.", "HTTP 5xx responses are directly observed.", server_errors, 0.60)
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

        # Model output is advisory. It may select an existing hypothesis and
        # cite existing evidence IDs, but it cannot create a new root cause.
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
                        "Only select an exact supplied hypothesis and cite only supplied evidence IDs."
                    ),
                    schema=_LOG_SCHEMA,
                    evidence=evidence.to_prompt(max_chars=3500),
                )
                if parsed:
                    candidate = str(parsed.get("selected_hypothesis") or "")
                    valid = next((item for item in rule_hypotheses if item["hypothesis"] == candidate), None)
                    supplied_ids = {e.evidence_id for e in evidence.events}
                    cited = [str(x) for x in parsed.get("evidence_ids", []) if str(x) in supplied_ids]
                    if valid and cited:
                        selected = valid
                        llm_selected = candidate
                        llm_evidence_ids = cited[:8]
                        llm_confidence = min(float(parsed.get("confidence", 0.0)), 0.90)
            except Exception:
                # Rule-based evidence remains authoritative when the optional LLM fails.
                pass

        confidence = selected["support_score"] if selected else 0.15
        if llm_selected and llm_confidence:
            confidence = min(confidence, llm_confidence, 0.90)

        root_cause_hint = selected["hypothesis"] if selected else None
        evidence_payload = {
            "events": [
                {
                    "evidence_id": e.evidence_id,
                    "timestamp": e.timestamp,
                    "severity": e.severity,
                    "severity_score": e.severity_score,
                    "services": e.services,
                    "apis": e.apis,
                    "excerpt": e.line,
                }
                for e in evidence.events[:100]
            ],
            "critical_events": evidence.critical_events,
            "anomalies": evidence.anomalies,
            "api_failures": evidence.api_failures,
            "rare_events": evidence.rare_events,
            "services": evidence.services,
            "apis": evidence.apis,
            "pattern_counts": dict(list(evidence.pattern_counts.items())[:20]),
            "temporal_correlations": evidence.temporal_correlations,
            "supporting_evidence_ids": (llm_evidence_ids or (selected["evidence_ids"] if selected else [])),
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
