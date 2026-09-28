"""
models.schemas
==============

Pydantic schemas used as the public contract for the API and as the
internal data-transfer objects between agents / engines.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from config.settings import settings


# ---------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------

class IncidentCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=512)
    description: Optional[str] = Field(default=None, max_length=10_000)
    severity: Literal["P0", "P1", "P2", "P3", "P4"] = "P3"
    incident_type: Optional[str] = Field(
        default=None,
        max_length=64,
        description="E.g. 'latency', 'error_rate', 'availability', 'data', 'security'",
    )
    affected_services: List[str] = Field(default_factory=list)
    raw_logs: List[str] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    traces: List[Dict[str, Any]] = Field(default_factory=list)
    topology: Dict[str, Any] = Field(default_factory=dict)
    context: Dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None

    @field_validator("affected_services")
    @classmethod
    def _limit_services(cls, v: List[str]) -> List[str]:
        max_n = settings.max_affected_services
        if len(v) > max_n:
            raise ValueError(f"affected_services cannot exceed {max_n} items")
        return [s[:256] for s in v]

    @field_validator("raw_logs")
    @classmethod
    def _limit_logs(cls, v: List[str]) -> List[str]:
        max_lines = settings.max_log_lines
        max_chars = settings.max_log_line_chars
        if len(v) > max_lines:
            raise ValueError(f"raw_logs cannot exceed {max_lines} lines")
        return [line[:max_chars] for line in v]

    @field_validator("traces")
    @classmethod
    def _limit_traces(cls, v: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        max_n = settings.max_traces
        if len(v) > max_n:
            raise ValueError(f"traces cannot exceed {max_n} items")
        return v


class IncidentOut(BaseModel):
    id: str
    title: str
    description: Optional[str]
    severity: str
    status: str
    incident_type: Optional[str]
    affected_services: List[str]
    created_at: datetime
    resolved_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------
# Findings
# ---------------------------------------------------------------

class FindingOut(BaseModel):
    id: str
    incident_id: str
    agent_name: str
    finding_type: str
    description: str
    confidence: float
    evidence: Dict[str, Any]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------
# Root Cause
# ---------------------------------------------------------------

class AlternativeHypothesis(BaseModel):
    cause: str
    confidence: float
    evidence: List[str] = Field(default_factory=list)


class RootCauseOut(BaseModel):
    incident_id: str
    root_cause: str
    confidence: float
    alternatives: List[AlternativeHypothesis]
    explanation: str
    causal_chain: List[Dict[str, Any]]
    contributing_factors: List[str]


# ---------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------

GroundTruthSource = Literal[
    "engineer_confirmed",
    "incident_postmortem",
    "external_system",
    "benchmark_label",
]


class ResolutionOut(BaseModel):
    incident_id: str
    action: str
    steps: List[str]
    verified: bool
    confirmed_root_cause: Optional[str] = None
    ground_truth_source: Optional[GroundTruthSource] = None
    ground_truth_confidence: Optional[float] = None
    confirmed_by: Optional[str] = None


class ResolutionCreate(BaseModel):
    action: str = Field(..., min_length=1, max_length=2048)
    steps: List[str] = Field(default_factory=list, max_length=100)
    verified: bool = False
    confirmed_root_cause: Optional[str] = Field(
        default=None,
        max_length=4096,
        description=(
            "Engineer-confirmed root cause. PRISM never learns from its "
            "own unverified consensus; supplying this value is what unlocks "
            "pattern/memory/knowledge-graph/agent-reliability updates."
        ),
    )
    ground_truth_source: Optional[GroundTruthSource] = Field(
        default=None,
        description=(
            "Provenance of the confirmed root cause. Required for agent "
            "reliability grading so agents are compared against independent "
            "truth rather than PRISM's consensus."
        ),
    )
    ground_truth_confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="Optional confidence in the confirmed root cause.",
    )
    confirmed_by: Optional[str] = Field(
        default=None, max_length=128,
        description="Who/what confirmed the root cause (engineer, tool, runbook).",
    )

    @model_validator(mode="after")
    def _require_source_when_confirmed(self) -> "ResolutionCreate":
        if self.confirmed_root_cause and self.confirmed_by is None and self.ground_truth_source is None:
            raise ValueError(
                "confirmed_root_cause requires ground_truth_source and/or confirmed_by"
            )
        return self


# ---------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------

class PatternOut(BaseModel):
    id: str
    pattern_signature: str
    pattern_text: str
    root_cause_hint: str
    confidence: float
    approved: bool
    occurrence_count: int

    model_config = {"from_attributes": True}


class PatternApprove(BaseModel):
    pattern_id: str
    approver: str = Field(..., min_length=1, max_length=128)


# ---------------------------------------------------------------
# Memory
# ---------------------------------------------------------------

class MemoryHit(BaseModel):
    incident_id: str
    similarity: float
    root_cause: str
    resolution: Optional[str]
    services: List[str]


class MemorySearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)


# ---------------------------------------------------------------
# Agent Reliability
# ---------------------------------------------------------------

class AgentReliabilityOut(BaseModel):
    agent_name: str
    accuracy: float
    precision: float
    recall: float
    latency_avg: float
    confidence_avg: float
    false_positives: int
    false_negatives: int
    invocations: int
    reliability_score: float

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------

class PredictionOut(BaseModel):
    id: str
    service: str
    predicted_failure_type: str
    probability: float
    estimated_time_minutes: Optional[int]
    impact: str
    rationale: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PredictionRunRequest(BaseModel):
    services: List[str] = Field(default_factory=list)

    @field_validator("services")
    @classmethod
    def _limit_batch(cls, v: List[str]) -> List[str]:
        max_n = settings.max_prediction_batch
        if len(v) > max_n:
            raise ValueError(f"services batch cannot exceed {max_n}")
        return v


# ---------------------------------------------------------------
# Explainability / Meta-Reasoning
# ---------------------------------------------------------------

class ExplanationOut(BaseModel):
    incident_id: str
    evidence_used: List[Dict[str, Any]]
    confidence_breakdown: Dict[str, Any]
    graph_reasoning: str
    alternative_root_causes: List[AlternativeHypothesis]
    final_explanation: str


class MetaReasoningOut(BaseModel):
    incident_id: str
    useful_agents: List[str]
    unnecessary_agents: List[str]
    optimal_path: str
    suggestions: List[str]
    agent_scores: Dict[str, float]


# ---------------------------------------------------------------
# Investigation result
# ---------------------------------------------------------------

class AgentStatus(BaseModel):
    """Per-agent execution outcome for failure semantics & provenance."""

    agent_name: str
    status: Literal["ok", "failed", "skipped"] = "ok"
    execution_ms: float = 0.0
    finding_type: Optional[str] = None


class JobOut(BaseModel):
    """Status of a background investigation job (Phase 14/16)."""

    id: str
    tenant_id: Optional[str] = None
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    progress: float = 0.0
    attempts: int = 0
    attempts_max: int = 3
    result_incident_id: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    incident: Optional[IncidentOut] = None


class InvestigationResult(BaseModel):
    incident_id: str
    root_cause: RootCauseOut
    resolution: Optional[ResolutionOut] = None
    explanation: ExplanationOut
    meta_reasoning: MetaReasoningOut
    agents_used: List[str]
    duration_seconds: float
    # Failure semantics: how many agents degraded vs succeeded (P1#21).
    agent_statuses: List[AgentStatus] = Field(default_factory=list)
    status: Literal[
        "completed",
        "completed_with_degraded_agents",
        "failed",
    ] = "completed"
    # Reproducibility metadata (P2#22): exact versions / model / config used.
    runtime: Dict[str, Any] = Field(default_factory=dict)