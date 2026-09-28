"""Structured, bounded evidence extraction for incident logs."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from utils.text import (
    TIMESTAMP_PATTERN,
    classify_log_severity,
    extract_apis,
    extract_services,
    hash_text,
    normalize_log_line,
)


@dataclass
class LogEvent:
    """A normalized log event with stable provenance."""

    evidence_id: str
    line: str
    normalized: str
    timestamp: Optional[str]
    severity: str
    severity_score: float
    services: List[str] = field(default_factory=list)
    apis: List[str] = field(default_factory=list)


@dataclass
class CompressedEvidence:
    """Bounded evidence used by the reasoning layer and LLM trust boundary."""

    critical_events: List[str] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)
    api_failures: List[str] = field(default_factory=list)
    rare_events: List[str] = field(default_factory=list)
    services: List[str] = field(default_factory=list)
    apis: List[str] = field(default_factory=list)
    pattern_counts: Dict[str, int] = field(default_factory=dict)
    events: List[LogEvent] = field(default_factory=list)
    temporal_correlations: List[Dict[str, object]] = field(default_factory=list)
    summary: str = ""
    original_line_count: int = 0
    compressed_line_count: int = 0

    @property
    def compression_ratio(self) -> float:
        if self.original_line_count == 0:
            return 0.0
        return max(0.0, 1.0 - (len(self.pattern_counts) / self.original_line_count))

    def to_prompt(self, max_chars: int = 4000) -> str:
        """Render evidence as data. The model receives IDs, not authority to invent evidence."""
        parts: List[str] = []
        for label, values in (
            ("CRITICAL EVENTS", self.critical_events),
            ("ANOMALIES", self.anomalies),
            ("API FAILURES", self.api_failures),
            ("RARE EVENTS", self.rare_events),
        ):
            if values:
                parts.append(label + ":\n" + "\n".join(f"- {e}" for e in values[:20]))
        if self.services:
            parts.append("SERVICES: " + ", ".join(self.services[:20]))
        if self.apis:
            parts.append("APIS: " + ", ".join(self.apis[:20]))
        if self.temporal_correlations:
            rows = []
            for c in self.temporal_correlations[:20]:
                rows.append(
                    f"- {c['before_id']} -> {c['after_id']} "
                    f"({c['delta_seconds']:.3f}s): {c['relationship']}"
                )
            parts.append("TEMPORAL CORRELATIONS:\n" + "\n".join(rows))
        if self.pattern_counts:
            top = sorted(self.pattern_counts.items(), key=lambda x: -x[1])[:10]
            parts.append("FREQUENT PATTERNS:\n" + "\n".join(f"- {p} (x{c})" for p, c in top))
        return "\n\n".join(parts)[:max_chars]


def _parse_timestamp(line: str) -> Optional[datetime]:
    match = TIMESTAMP_PATTERN.search(line)
    if not match:
        return None
    value = match.group(0)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _relationship(before: LogEvent, after: LogEvent) -> Optional[str]:
    before_text = before.line.lower()
    after_text = after.line.lower()
    if any(x in before_text for x in ("refused", "connection refused")) and any(x in after_text for x in ("timeout", "503", "500", "5xx")):
        return "dependency_failure_before_service_error"
    if any(x in before_text for x in ("deploy", "deployment", "release")) and after.severity_score >= 0.5:
        return "deployment_before_error"
    if before.severity_score >= 0.7 and after.severity_score >= 0.5:
        return "high_severity_before_error"
    return None


def compress_logs(
    raw_lines: List[str],
    severity_threshold: float = 0.4,
    rare_count: int = 2,
    max_lines_per_bucket: int = 50,
    max_temporal_gap_seconds: float = 60.0,
) -> CompressedEvidence:
    """Parse, normalize, deduplicate and temporally correlate bounded log evidence."""
    ev = CompressedEvidence(original_line_count=len(raw_lines))
    pattern_counter: Counter[str] = Counter()
    parsed_events: List[Tuple[LogEvent, Optional[datetime]]] = []

    for line in raw_lines:
        if not line or not line.strip():
            continue
        original = line.strip()
        normalized = normalize_log_line(original)
        pattern_counter[normalized] += 1
        severity, score = classify_log_severity(original)
        services = extract_services(original)
        apis = extract_apis(original)
        event_id = "log-" + hash_text(normalized + "|" + original)[:12]
        event = LogEvent(
            evidence_id=event_id,
            line=original,
            normalized=normalized,
            timestamp=(TIMESTAMP_PATTERN.search(original).group(0) if TIMESTAMP_PATTERN.search(original) else None),
            severity=severity,
            severity_score=score,
            services=list(dict.fromkeys(services)),
            apis=list(dict.fromkeys(apis)),
        )
        ev.events.append(event)
        parsed_events.append((event, _parse_timestamp(original)))
        ev.apis.extend(apis)
        ev.services.extend(services)
        if score >= 0.8:
            ev.critical_events.append(original)
        elif score >= severity_threshold:
            ev.anomalies.append(original)
        if any(tok in original.lower() for tok in ("timeout", "refused", "5xx", "503", "500")):
            ev.api_failures.append(original)

    rare_patterns = {p for p, count in pattern_counter.items() if count <= rare_count}
    for event in ev.events:
        if event.normalized in rare_patterns:
            ev.rare_events.append(event.line)

    # Correlate only nearby, timestamped events. This is evidence of ordering,
    # not proof of causality.
    timestamped = [(e, ts) for e, ts in parsed_events if ts is not None]
    timestamped.sort(key=lambda pair: pair[1])
    for index, (before, before_ts) in enumerate(timestamped):
        for after, after_ts in timestamped[index + 1:index + 9]:
            delta = (after_ts - before_ts).total_seconds()
            if delta < 0 or delta > max_temporal_gap_seconds:
                break
            relation = _relationship(before, after)
            if relation:
                ev.temporal_correlations.append({
                    "before_id": before.evidence_id,
                    "after_id": after.evidence_id,
                    "delta_seconds": delta,
                    "relationship": relation,
                })

    ev.pattern_counts = dict(pattern_counter)
    ev.services = list(dict.fromkeys(ev.services))
    ev.apis = list(dict.fromkeys(ev.apis))
    ev.critical_events = ev.critical_events[:max_lines_per_bucket]
    ev.anomalies = ev.anomalies[:max_lines_per_bucket]
    ev.api_failures = ev.api_failures[:max_lines_per_bucket]
    ev.rare_events = ev.rare_events[:max_lines_per_bucket]
    ev.temporal_correlations = ev.temporal_correlations[:50]
    ev.compressed_line_count = len(ev.critical_events) + len(ev.anomalies) + len(ev.api_failures) + len(ev.rare_events)
    ev.summary = (
        f"Parsed {ev.original_line_count} lines into {len(ev.events)} events and "
        f"{len(ev.temporal_correlations)} temporal correlations. "
        f"Critical={len(ev.critical_events)}, anomalies={len(ev.anomalies)}, "
        f"API failures={len(ev.api_failures)}, rare={len(ev.rare_events)}."
    )
    return ev
