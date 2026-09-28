"""
utils.evidence
==============

Evidence Compression Engine.

Instead of sending all raw logs to the LLM, this module:

1. Extracts anomalies / errors / critical events
2. Removes duplicates via normalization + hashing
3. Compresses repetitive lines into (pattern, count) tuples
4. Caps the final payload size
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

from utils.text import (
    classify_log_severity,
    extract_apis,
    extract_services,
    normalize_log_line,
)


@dataclass
class CompressedEvidence:
    """Output of the evidence compression engine."""

    critical_events: List[str] = field(default_factory=list)
    anomalies: List[str] = field(default_factory=list)
    api_failures: List[str] = field(default_factory=list)
    rare_events: List[str] = field(default_factory=list)
    services: List[str] = field(default_factory=list)
    apis: List[str] = field(default_factory=list)
    pattern_counts: Dict[str, int] = field(default_factory=dict)
    summary: str = ""
    original_line_count: int = 0
    compressed_line_count: int = 0

    @property
    def compression_ratio(self) -> float:
        """Fraction of original lines that were deduplicated (via normalized patterns)."""
        if self.original_line_count == 0:
            return 0.0
        # Unique normalized signatures actually stored in ``pattern_counts``.
        # Two raw lines that differ only by timestamp / numbers collapse
        # to the same signature, so this measures true dedup compression.
        unique_patterns = len(self.pattern_counts)
        return max(0.0, 1.0 - (unique_patterns / self.original_line_count))

    def to_prompt(self, max_chars: int = 4000) -> str:
        """Render the compressed evidence as a compact LLM-ready string."""
        parts: List[str] = []
        if self.critical_events:
            parts.append("CRITICAL EVENTS:\n" + "\n".join(f"- {e}" for e in self.critical_events[:20]))
        if self.anomalies:
            parts.append("ANOMALIES:\n" + "\n".join(f"- {e}" for e in self.anomalies[:20]))
        if self.api_failures:
            parts.append("API FAILURES:\n" + "\n".join(f"- {e}" for e in self.api_failures[:20]))
        if self.rare_events:
            parts.append("RARE EVENTS:\n" + "\n".join(f"- {e}" for e in self.rare_events[:20]))
        if self.services:
            parts.append("SERVICES: " + ", ".join(self.services[:20]))
        if self.apis:
            parts.append("APIS: " + ", ".join(self.apis[:20]))
        if self.pattern_counts:
            top = sorted(self.pattern_counts.items(), key=lambda x: -x[1])[:10]
            parts.append("FREQUENT PATTERNS:\n" + "\n".join(f"- {p} (x{c})" for p, c in top))
        out = "\n\n".join(parts)
        return out[:max_chars]


def compress_logs(
    raw_lines: List[str],
    severity_threshold: float = 0.4,
    rare_count: int = 2,
    max_lines_per_bucket: int = 50,
) -> CompressedEvidence:
    """
    Compress a list of raw log lines into structured evidence.

    Parameters
    ----------
    raw_lines
        Input log lines.
    severity_threshold
        Lines with severity >= threshold become ``anomalies``.
    rare_count
        Patterns occurring <= ``rare_count`` times are flagged as rare.
    max_lines_per_bucket
        Maximum number of lines kept in each bucket of the result.
    """
    ev = CompressedEvidence(original_line_count=len(raw_lines))

    normalized: List[str] = []
    pattern_counter: Counter[str] = Counter()

    for line in raw_lines:
        if not line or not line.strip():
            continue
        n = normalize_log_line(line)
        normalized.append((line.strip(), n))
        pattern_counter[n] += 1

        apis = extract_apis(line)
        svcs = extract_services(line)
        ev.apis.extend(apis)
        ev.services.extend(svcs)

        label, score = classify_log_severity(line)
        if score >= 0.8:
            ev.critical_events.append(line.strip())
        elif score >= severity_threshold:
            ev.anomalies.append(line.strip())
        if any(tok in line.lower() for tok in ("timeout", "refused", "5xx", "503", "500")):
            ev.api_failures.append(line.strip())

    rare_patterns = {p for p, c in pattern_counter.items() if c <= rare_count}
    for orig, norm in normalized:
        if norm in rare_patterns:
            ev.rare_events.append(orig)

    ev.pattern_counts = dict(pattern_counter)
    ev.services = list(dict.fromkeys(ev.services))  # dedup preserve order
    ev.apis = list(dict.fromkeys(ev.apis))

    # Truncate buckets
    ev.critical_events = ev.critical_events[:max_lines_per_bucket]
    ev.anomalies = ev.anomalies[:max_lines_per_bucket]
    ev.api_failures = ev.api_failures[:max_lines_per_bucket]
    ev.rare_events = ev.rare_events[:max_lines_per_bucket]

    ev.compressed_line_count = (
        len(ev.critical_events)
        + len(ev.anomalies)
        + len(ev.api_failures)
        + len(ev.rare_events)
    )

    ev.summary = (
        f"Compressed {ev.original_line_count} lines into "
        f"{ev.compressed_line_count} evidence items "
        f"(ratio={ev.compression_ratio:.2%}). "
        f"Critical={len(ev.critical_events)}, "
        f"Anomalies={len(ev.anomalies)}, "
        f"API failures={len(ev.api_failures)}, "
        f"Rare={len(ev.rare_events)}."
    )
    return ev
