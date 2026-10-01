"""
agents.metric_analyzer
======================

Detects anomalies in time-series metrics.  Works on a dictionary of
``{metric_name: [values...]}`` and applies simple but robust statistics
(z-score, percentage change) without requiring scikit-learn.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List

from agents.base import BaseAgent, FindingPayload


class MetricAnalyzerAgent(BaseAgent):
    """Agent that analyzes metric time-series for anomalies."""

    name = "metric_analyzer"
    supported_incident_types = ["latency", "error_rate", "availability", "data", "*"]
    requires = ["metrics"]
    priority = 20

    async def investigate(self, context: Dict[str, Any]) -> FindingPayload:
        metrics: Dict[str, List[float]] = context.get("metrics", {}) or {}

        anomalies: List[Dict[str, Any]] = []
        hypotheses: List[str] = []
        max_score = 0.0

        for metric_name, values in metrics.items():
            # Metrics may also contain a current scalar reading. It cannot
            # establish a time-series anomaly, but it must not fail the agent.
            if not isinstance(values, (list, tuple)) or len(values) < 3:
                continue
            try:
                nums = [float(v) for v in values]
            except (TypeError, ValueError):
                continue

            mean = statistics.fmean(nums)
            stdev = statistics.pstdev(nums) or 1e-9
            last = nums[-1]
            z = abs(last - mean) / stdev
            # Compare the latest observation with the preceding window,
            # not with a baseline that includes the current point.
            baseline = nums[:-1]
            prev_avg = statistics.fmean(baseline) if baseline else mean
            if abs(prev_avg) < 1e-9:
                change_pct = 0.0 if abs(last) < 1e-9 else float("inf")
            else:
                change_pct = ((last - prev_avg) / abs(prev_avg)) * 100

            severity = min(1.0, z / 4.0)  # z>=4 => 1.0

            if z >= 2.0 or abs(change_pct) >= 50:
                anomalies.append(
                    {
                        "metric": metric_name,
                        "last_value": last,
                        "mean": mean,
                        "stdev": stdev,
                        "z_score": round(z, 3),
                        "change_pct": round(change_pct, 2),
                        "severity": round(severity, 3),
                    }
                )
                max_score = max(max_score, severity)

                if "latency" in metric_name.lower() or "p99" in metric_name.lower():
                    hypotheses.append(f"Latency spike on '{metric_name}' (+{change_pct:.0f}%).")
                elif "error" in metric_name.lower() or "5xx" in metric_name.lower():
                    hypotheses.append(f"Error-rate spike on '{metric_name}' (z={z:.1f}).")
                elif "cpu" in metric_name.lower():
                    hypotheses.append(f"CPU saturation on '{metric_name}'.")
                elif "memory" in metric_name.lower() or "heap" in metric_name.lower():
                    hypotheses.append(f"Memory pressure on '{metric_name}'.")
                elif "queue" in metric_name.lower() or "lag" in metric_name.lower():
                    hypotheses.append(f"Queue backlog building on '{metric_name}'.")
                elif "saturation" in metric_name.lower() or "util" in metric_name.lower():
                    hypotheses.append(f"Resource saturation on '{metric_name}'.")

        confidence = min(1.0, 0.3 + max_score * 0.6)
        root_cause_hint = hypotheses[0] if hypotheses else None

        return FindingPayload(
            agent_name=self.name,
            finding_type="metric_analysis",
            description=(
                f"Detected {len(anomalies)} metric anomalies "
                f"(max severity={max_score:.2f})."
            ),
            confidence=confidence,
            evidence={
                "anomalies": anomalies,
                "metrics_inspected": list(metrics.keys()),
            },
            root_cause_hint=root_cause_hint,
            hypotheses=hypotheses,
        )
