"""Epistemic contract for PRISM investigation results.

PRISM distinguishes four states:

OBSERVED -> EVIDENCE -> INFERENCE -> CONFIRMED

Agent output is never considered confirmation.  A confirmed root cause must
come from an explicit external/human/benchmark confirmation event.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Dict, List, Optional


class ClaimLevel(StrEnum):
    OBSERVED = "observed"
    EVIDENCE = "evidence"
    INFERENCE = "inference"
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class EvidenceRef:
    id: str
    source: str
    summary: str
    level: ClaimLevel = ClaimLevel.EVIDENCE
    reliability: float = 0.5
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HypothesisRecord:
    id: str
    statement: str
    support: List[str] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    support_score: float = 0.0
    status: str = "candidate"

    def add_support(self, evidence_id: str) -> None:
        if evidence_id not in self.support:
            self.support.append(evidence_id)

    def add_contradiction(self, evidence_id: str) -> None:
        if evidence_id not in self.contradictions:
            self.contradictions.append(evidence_id)

    @property
    def is_confirmed(self) -> bool:
        return self.status == ClaimLevel.CONFIRMED.value


def mark_inferred(metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return provenance metadata for an inferred agent result."""
    result = dict(metadata or {})
    provenance = dict(result.get("provenance") or {})
    provenance.setdefault("claim_level", ClaimLevel.INFERENCE.value)
    provenance.setdefault("confirmation_required", True)
    result["provenance"] = provenance
    return result


def require_confirmation(source: Optional[str]) -> str:
    """Validate an authoritative confirmation source."""
    allowed = {
        "engineer_confirmed",
        "incident_postmortem",
        "external_system",
        "benchmark_label",
    }
    if source not in allowed:
        raise ValueError("A PRISM-generated RCA cannot be used as confirmation")
    return source
