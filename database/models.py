"""
database.models
===============

SQLAlchemy ORM models for:

* Incidents & their evidence
* Root cause findings
* Resolutions
* Pattern library (self-learning)
* Agent reliability statistics
* Lessons learned

Embeddings are stored as JSON-encoded vectors (PostgreSQL ``jsonb``)
so the system can run without the ``pgvector`` extension.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from database.session import Base


# Dialect-aware JSON type: uses JSONB on PostgreSQL, JSON on SQLite/other.
class JSONBOrJSON(TypeDecorator):
    """Platform-independent JSON type.

    Uses PostgreSQL's :class:`JSONB` when available, otherwise falls
    back to the generic :class:`JSON` type (e.g. for SQLite in tests).
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


def _uuid_str() -> str:
    return uuid.uuid4().hex


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Incident(Base, TimestampMixin):
    """A production incident under investigation or already resolved."""

    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(String(32), default="P3")
    status: Mapped[str] = mapped_column(String(32), default="open")  # open|investigating|resolved
    incident_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    affected_services: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    raw_logs: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSONBOrJSON, default=dict, nullable=False)
    traces: Mapped[List[dict]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    topology: Mapped[dict] = mapped_column(JSONBOrJSON, default=dict, nullable=False)
    context: Mapped[dict] = mapped_column(JSONBOrJSON, default=dict, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Client-supplied idempotency key: same key + same incident => no duplicate
    # work.  Unique so concurrent requests can never claim the same key onto
    # two different incident rows (the duplicate request is replayed, see
    # api.investigation._run_investigation).
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True, unique=True
    )
    # Optional multi-tenant scoping (Phase 4).  Null == default tenant, so
    # existing deployments are unaffected.  Read paths filter on this column
    # when the ``X-Tenant-Id`` header is supplied.
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    findings: Mapped[List["Finding"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )
    root_cause: Mapped[Optional["RootCause"]] = relationship(
        back_populates="incident", uselist=False, cascade="all, delete-orphan"
    )
    resolution: Mapped[Optional["Resolution"]] = relationship(
        back_populates="incident", uselist=False, cascade="all, delete-orphan"
    )
    lessons: Mapped[List["LessonLearned"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )


class Finding(Base, TimestampMixin):
    """An intermediate finding produced by an agent during investigation."""

    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    incident_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    finding_type: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[dict] = mapped_column(JSONBOrJSON, default=dict, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONBOrJSON, default=dict, nullable=False)

    incident: Mapped["Incident"] = relationship(back_populates="findings")


class RootCause(Base, TimestampMixin):
    """Final root cause hypothesis chosen by the consensus engine."""

    __tablename__ = "root_causes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    incident_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("incidents.id", ondelete="CASCADE"), unique=True, index=True
    )
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    alternatives: Mapped[List[dict]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, default="")
    causal_chain: Mapped[List[dict]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    contributing_factors: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list, nullable=False)

    incident: Mapped["Incident"] = relationship(back_populates="root_cause")


class Resolution(Base, TimestampMixin):
    """Resolution / remediation applied to an incident."""

    __tablename__ = "resolutions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    incident_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("incidents.id", ondelete="CASCADE"), unique=True, index=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONBOrJSON, default=dict, nullable=False)

    incident: Mapped["Incident"] = relationship(back_populates="resolution")


class LessonLearned(Base, TimestampMixin):
    """A lesson learned from a resolved incident."""

    __tablename__ = "lessons_learned"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    incident_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    lesson: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(128), default="general")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)

    incident: Mapped["Incident"] = relationship(back_populates="lessons")


class Pattern(Base, TimestampMixin):
    """
    Self-learning pattern library.

    A *pattern* is a normalized log signature that has been associated with
    a particular root cause in the past.  Approved patterns are reused by
    the :mod:`learning.pattern_generator` to short-circuit future
    investigations.
    """

    __tablename__ = "patterns"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    pattern_signature: Mapped[str] = mapped_column(String(256), index=True)
    pattern_text: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause_hint: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    incident_ids: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONBOrJSON, default=dict, nullable=False)


class IncidentMemory(Base, TimestampMixin):
    """
    Incident memory record with embedding vector (JSONB list of floats).

    Used for semantic similarity retrieval via FAISS or in-memory cosine.
    """

    __tablename__ = "incident_memory"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    incident_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    text_repr: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[List[float]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    lessons: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list, nullable=False)
    services: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list, nullable=False)


class AgentReliability(Base, TimestampMixin):
    """
    Reliability statistics per agent.  Updated continuously by the
    :mod:`meta_reasoning` and :mod:`learning.continuous_learning` engines.
    """

    __tablename__ = "agent_reliability"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(128), index=True)
    accuracy: Mapped[float] = mapped_column(Float, default=0.5)
    precision: Mapped[float] = mapped_column(Float, default=0.5)
    recall: Mapped[float] = mapped_column(Float, default=0.5)
    latency_avg: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_avg: Mapped[float] = mapped_column(Float, default=0.5)
    false_positives: Mapped[int] = mapped_column(Integer, default=0)
    false_negatives: Mapped[int] = mapped_column(Integer, default=0)
    true_positives: Mapped[int] = mapped_column(Integer, default=0)
    true_negatives: Mapped[int] = mapped_column(Integer, default=0)
    invocations: Mapped[int] = mapped_column(Integer, default=0)
    reliability_score: Mapped[float] = mapped_column(Float, default=0.5)
    last_invocation: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Prediction(Base, TimestampMixin):
    """A predictive incident forecast produced by :mod:`prediction`."""

    __tablename__ = "predictions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    service: Mapped[str] = mapped_column(String(128), index=True)
    predicted_failure_type: Mapped[str] = mapped_column(String(256))
    __table_args__ = (
        UniqueConstraint("service", "predicted_failure_type", name="_service_predicted_failure_type_uc"),
    )
    probability: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_time_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    impact: Mapped[str] = mapped_column(String(32), default="medium")
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict] = mapped_column(JSONBOrJSON, default=dict, nullable=False)


class InvestigationJob(Base, TimestampMixin):
    """
    A durable investigation job for the background worker (Phase 14/16).

    ``POST /incidents/investigate/async`` enqueues a row here and returns
    ``202`` immediately; the worker claims it, runs the pipeline, and writes
    the resulting incident id.  The unique ``idempotency_key`` collapses
    duplicate enqueues onto a single job, and ``attempts`` / ``attempts_max``
    plus ``error`` provide retry + dead-letter semantics.
    """

    __tablename__ = "investigation_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # Same idempotency-key semantics as incidents: same key + same payload
    # must never create two jobs.
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True, unique=True
    )
    status: Mapped[str] = mapped_column(  # queued|running|completed|failed|cancelled
        String(16), default="queued", nullable=False, index=True
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts_max: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONBOrJSON, default=dict, nullable=False)
    result_incident_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("incidents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Exponential retry backoff: a failed try requeues the job with
    # next_attempt_at set in the future so workers skip it until then.
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class MetaReasoningRecord(Base, TimestampMixin):
    """An evaluation produced by the meta-reasoning engine."""

    __tablename__ = "meta_reasoning_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_str)
    incident_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("incidents.id", ondelete="CASCADE"), index=True
    )
    useful_agents: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list)
    unnecessary_agents: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list)
    optimal_path: Mapped[str] = mapped_column(Text, default="")
    suggestions: Mapped[List[str]] = mapped_column(JSONBOrJSON, default=list)
    agent_scores: Mapped[dict] = mapped_column(JSONBOrJSON, default=dict)