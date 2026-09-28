"""
agents.rule_based_analyzer
==========================

Pure rule-based agent.  Always available — no external dependencies.
Used as the deterministic baseline voter in the consensus engine.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload
from utils.text import classify_log_severity, extract_apis, extract_services


# Simple rule book: (predicate on logs, root_cause_hint, confidence, weight)
RULES = [
    (lambda logs: any("oom" in line.lower() or "out of memory" in line.lower() for line in logs),
     "Memory exhaustion (OOM) killed the service.", 0.85, ["oom"]),
    (lambda logs: any("timeout" in line.lower() or "timed out" in line.lower() for line in logs),
     "Downstream dependency timeout.", 0.7, []),
    (lambda logs: any("connection refused" in line.lower() for line in logs),
     "Downstream service unavailable (connection refused).", 0.75, []),
    (lambda logs: any("deployment" in line.lower() or "deploy" in line.lower() for line in logs),
     "Recent deployment introduced a regression.", 0.6, []),
    (lambda logs: any("disk full" in line.lower() or "no space left" in line.lower() for line in logs),
     "Disk exhaustion on the host.", 0.9, []),
    (lambda logs: any("certificate" in line.lower() and "expired" in line.lower() for line in logs),
     "TLS certificate expired.", 0.95, []),
    (lambda logs: any("503" in line for line in logs),
     "Upstream returned 503 (service unavailable).", 0.65, []),
    (lambda logs: any("429" in line for line in logs),
     "Rate-limit (429) on upstream API.", 0.6, []),
    (lambda logs: any("dns" in line.lower() and ("fail" in line.lower() or "error" in line.lower()) for line in logs),
     "DNS resolution failure.", 0.8, []),
    (lambda logs: any("deadlock" in line.lower() for line in logs),
     "Database deadlock.", 0.8, []),
]


class RuleBasedAnalyzerAgent(BaseAgent):
    """Deterministic rule-based agent.  Used as consensus baseline."""

    name = "rule_based_analyzer"
    supported_incident_types = ["*"]
    requires = ["logs"]
    priority = 5
    requires_external = False

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        logs: List[str] = context.get("logs", [])
        if not logs:
            return FindingPayload(
                agent_name=self.name,
                finding_type="rule_based",
                description="No logs to apply rules to.",
                confidence=0.1,
                evidence={"matched_rules": []},
            )

        matched: List[Dict[str, Any]] = []
        for idx, (predicate, hint, conf, tags) in enumerate(RULES):
            try:
                if predicate(logs):
                    matched.append(
                        {"rule_id": idx, "hint": hint, "confidence": conf, "tags": tags}
                    )
            except Exception:
                continue

        # Aggregate: prioritize OOM if present, otherwise take the highest-confidence rule
        if matched:
            oom_rule = next((r for r in matched if "oom" in r["tags"]), None)
            if oom_rule:
                best = oom_rule
            else:
                best = max(matched, key=lambda r: r["confidence"])

            boost = min(0.15, 0.03 * (len(matched) - 1))
            final_conf = min(1.0, best["confidence"] + boost)
            root_cause_hint = best["hint"]
            hypotheses = [m["hint"] for m in matched]
        else:
            final_conf = 0.2
            root_cause_hint = None
            hypotheses = []

        # Severity fallback
        max_sev = max((classify_log_severity(line)[1] for line in logs), default=0.0)
        if not matched and max_sev >= 0.5:
            hypotheses.append("Generic error detected; rules could not isolate cause.")
            final_conf = max(final_conf, 0.3)

        return FindingPayload(
            agent_name=self.name,
            finding_type="rule_based",
            description=(
                f"Matched {len(matched)} rule(s). "
                f"Top hint: {root_cause_hint or 'none'}."
            ),
            confidence=final_conf,
            evidence={
                "matched_rules": matched,
                "max_severity": max_sev,
                "services_mentioned": extract_services(" ".join(logs)),
                "apis_mentioned": extract_apis(" ".join(logs)),
            },
            root_cause_hint=root_cause_hint,
            hypotheses=hypotheses,
        )