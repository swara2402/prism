"""
tests.test_learning
===================

Regression tests for the authoritative-learning guarantee:

* PRISM never learns from its own unverified consensus.
* ``POST /incidents/{id}/resolve`` is the only learning trigger, and it
  only learns when a *confirmed* (ground-truth) root cause is supplied.
* Agent reliability is graded against ground truth, never against PRISM's
  consensus, and requires an explicit ``ground_truth_source``.
* ``LEARNING_MODE=frozen`` makes the whole pipeline a no-op so evaluation
  runs stay independent and reproducible.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from database import models as dbm
from learning.continuous_learning import LearningInput, learn_from_incident

_AUTH = {"X-API-Key": "test-api-key-that-is-long-enough-32chars"}


def _make_input(**overrides):
    base = dict(
        incident_id="inc-learning-1",
        root_cause=None,
        confidence=0.9,
        affected_services=["web-svc"],
        raw_logs=[
            "2024-01-01T12:00:00Z ERROR web-svc: timeout calling /v1/charge",
            "2024-01-01T12:00:01Z ERROR web-svc: retrying connect",
        ],
        resolution="scaled up replicas",
        agents_used=["rule_based_analyzer", "log_analyzer"],
        findings=[
            {
                "agent_name": "rule_based_analyzer",
                "root_cause_hint": "Possible timeout / latency-related root cause.",
                "confidence": 0.8,
            },
            {
                "agent_name": "log_analyzer",
                "root_cause_hint": "network_failure",
                "confidence": 0.7,
            },
        ],
        consensus={},
        lessons=["lesson: add timeout alerts"],
        ground_truth_root_cause="timeout",
        ground_truth_source="engineer_confirmed",
        confirmed_by="sre-alice",
        confirmed_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return LearningInput(**base)


# ---------------------------------------------------------------
# Gating: no learning without confirmation / in frozen mode
# ---------------------------------------------------------------

def test_learn_refused_without_confirmed_root_cause():
    """PRISM's unverified consensus must never trigger learning."""
    inp = _make_input(
        root_cause=None,
        ground_truth_root_cause=None,
        ground_truth_source=None,
        confirmed_by=None,
    )
    loop = asyncio.new_event_loop()
    try:
        summary = loop.run_until_complete(learn_from_incident(inp))
    finally:
        loop.close()

    assert summary["learned"] is False
    assert summary["reason"] == "unconfirmed_incident"


def test_learn_is_noop_in_frozen_mode(monkeypatch):
    """LEARNING_MODE=frozen keeps evaluation runs independent.

    test_api.py's client fixture purges config/database/main/api modules
    (but not ``learning``), which swaps the cached settings singleton
    under an already-imported continuous_learning. Reimport both together
    so the monkeypatched mode is the one the learner actually reads.
    """
    for mod in list(sys.modules.keys()):
        if mod.startswith(("config", "learning")):
            del sys.modules[mod]

    from config.settings import settings
    from learning.continuous_learning import learn_from_incident

    monkeypatch.setattr(settings, "learning_mode", "frozen")
    inp = _make_input()
    loop = asyncio.new_event_loop()
    try:
        summary = loop.run_until_complete(learn_from_incident(inp))
    finally:
        loop.close()

    assert summary["learned"] is False
    assert summary["reason"] == "learning_mode_frozen"


@pytest.mark.asyncio
async def test_learn_confirmed_updates_memory_patterns_and_reliability(db_session):
    """A confirmed resolution with ground truth triggers full learning."""
    inp = _make_input(incident_id="inc-confirmed-1")
    summary = await learn_from_incident(inp)

    assert summary["learned"] is True
    assert summary["memory_updated"] is True
    assert summary["knowledge_graph_updated"] is True
    assert summary["agent_reliability_updated"] is True

    patterns = (
        await db_session.execute(select(func.count()).select_from(dbm.Pattern))
    ).scalar_one()
    assert patterns >= 1

    memory_count = (
        await db_session.execute(
            select(func.count()).select_from(dbm.IncidentMemory)
        )
    ).scalar_one()
    assert memory_count == 1

    lessons = (
        await db_session.execute(
            select(func.count()).select_from(dbm.LessonLearned)
        )
    ).scalar_one()
    assert lessons >= 1

    agents = (await db_session.execute(select(dbm.AgentReliability))).scalars().all()
    assert len(agents) >= 1
    graded = sum(
        (a.true_positives or 0)
        + (a.false_positives or 0)
        + (a.false_negatives or 0)
        + (a.true_negatives or 0)
        for a in agents
    )
    assert graded >= 1


@pytest.mark.asyncio
async def test_reliability_stays_flat_without_ground_truth_source(db_session):
    """Without ground_truth_source, learning may still update patterns/memory
    but must NOT grade agents (no circular grading against consensus)."""
    inp = _make_input(
        incident_id="inc-confirmed-2",
        ground_truth_source=None,
        confirmed_by="sre-alice",  # source is what matters for grading
    )
    summary = await learn_from_incident(inp)

    assert summary["learned"] is True
    assert summary["agent_reliability_updated"] is False

    agents = (await db_session.execute(select(dbm.AgentReliability))).scalars().all()
    for a in agents:
        assert (a.true_positives or 0) == 0
        assert (a.false_positives or 0) == 0


@pytest.mark.asyncio
async def test_reliability_source_must_be_an_allowed_provenance(db_session):
    from learning.continuous_learning import ALLOWED_GROUND_TRUTH_SOURCES

    inp = _make_input(
        incident_id="inc-confirmed-3",
        ground_truth_source="llm_guess",  # not independent ground truth
    )
    summary = await learn_from_incident(inp)
    assert summary["learned"] is True
    assert summary["agent_reliability_updated"] is False
    assert "llm_guess" not in ALLOWED_GROUND_TRUTH_SOURCES


# ---------------------------------------------------------------
# API-level behaviour
# ---------------------------------------------------------------

@pytest.fixture
def client():
    """API client with lifespan entered so tables exist.

    Shadows the conftest ``client`` fixture: conftest's version returns
    ``TestClient(app)`` without entering lifespan, so the in-memory DB
    never gets its tables and investigating 401s. test_api.py does the
    same (purge modules, create tables, enter lifespan, send the API key).
    """
    # Force reimport of settings + app so env vars are picked up
    for mod in list(sys.modules.keys()):
        if mod.startswith(("config", "database", "main", "api")):
            del sys.modules[mod]

    from fastapi.testclient import TestClient
    from database.session import Base, engine
    from main import app

    # Create tables synchronously via the engine
    import asyncio

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())

    with TestClient(app, headers=_AUTH) as c:
        yield c


def _query_counts_for_incident(incident_id: str):
    # Import fresh so we hit the engine/database created by the client
    # fixture (which purges and reinstates modules).
    from database import models as _dbm
    from database.session import AsyncSessionLocal
    from sqlalchemy import select as _select, func as _func

    async def _run():
        async with AsyncSessionLocal() as s:
            findings = (
                await s.execute(
                    _select(_dbm.Finding).where(_dbm.Finding.incident_id == incident_id)
                )
            ).scalars().all()
            with_hint = sum(
                1 for f in findings if (f.metadata_ or {}).get("root_cause_hint")
            )
            mem = (
                await s.execute(
                    _select(_func.count())
                    .select_from(_dbm.IncidentMemory)
                    .where(_dbm.IncidentMemory.incident_id == incident_id)
                )
            ).scalar_one()
            pats = (
                await s.execute(_select(_func.count()).select_from(_dbm.Pattern))
            ).scalar_one()
            agents = (await s.execute(_select(_dbm.AgentReliability))).scalars().all()
            graded = sum(
                (a.true_positives or 0)
                + (a.false_positives or 0)
                + (a.false_negatives or 0)
                + (a.true_negatives or 0)
                for a in agents
            )
            return len(findings), with_hint, mem, pats, graded

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


def test_api_investigate_does_not_learn(client):
    """
    Investigating an incident must not write patterns, solved-incident
    memory, or grade agents — the consensus is unverified at that point.
    """
    payload = {
        "title": "contamination guard",
        "raw_logs": ["2024-01-01T12:00:00Z ERROR web-svc: timeout calling /v1/charge"],
    }
    r = client.post("/incidents/investigate", json=payload, headers=_AUTH)
    assert r.status_code == 200, r.text
    incident_id = r.json()["incident_id"]

    n_findings, with_hint, mem, pats, graded = _query_counts_for_incident(incident_id)
    assert n_findings >= 1
    assert with_hint >= 1  # provenance persisted for later grading
    assert mem == 0        # no "solved incident" memory for an unconfirmed run
    assert pats == 0       # no trusted patterns from unconfirmed diagnostics
    assert graded == 0     # agents were NOT graded against PRISM's consensus


def test_api_resolve_without_confirmation_does_not_learn(client):
    payload = {
        "title": "resolve without ground truth",
        "raw_logs": ["2024-01-01T12:00:00Z ERROR web-svc: OOM killed process"],
    }
    r = client.post("/incidents/investigate", json=payload, headers=_AUTH)
    assert r.status_code == 200, r.text
    incident_id = r.json()["incident_id"]

    r = client.post(
        f"/incidents/{incident_id}/resolve",
        json={"action": "restarted service", "verified": True},
        headers=_AUTH,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmed_root_cause"] is None

    _, _, mem, pats, graded = _query_counts_for_incident(incident_id)
    assert mem == 0
    assert pats == 0
    assert graded == 0


def test_api_resolve_with_confirmed_root_cause_learns(client):
    payload = {
        "title": "resolved OOM incident",
        "severity": "P2",
        "affected_services": ["web-svc"],
        "raw_logs": ["2024-01-01T12:00:00Z ERROR web-svc: OOM killed process 1234"],
    }
    r = client.post("/incidents/investigate", json=payload, headers=_AUTH)
    assert r.status_code == 200, r.text
    incident_id = r.json()["incident_id"]

    r = client.post(
        f"/incidents/{incident_id}/resolve",
        json={
            "action": "increased memory limits",
            "steps": ["bump container memory"],
            "verified": True,
            "confirmed_root_cause": "resource exhaustion (memory)",
            "ground_truth_source": "engineer_confirmed",
            "ground_truth_confidence": 1.0,
            "confirmed_by": "sre-bob",
        },
        headers=_AUTH,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["confirmed_root_cause"] == "resource exhaustion (memory)"
    assert body["ground_truth_source"] == "engineer_confirmed"
    assert body["confirmed_by"] == "sre-bob"

    n_findings, with_hint, mem, pats, graded = _query_counts_for_incident(incident_id)
    assert n_findings >= 1
    assert with_hint >= 1
    assert mem == 1               # confirmed incident stored in memory
    assert pats >= 1              # confirmed patterns created
    assert graded >= 1            # agents graded against confirmed ground truth


def test_api_resolve_confirmed_without_source_rejects(client):
    """Confirmed root causes must carry provenance (source and/or byline)."""
    payload = {
        "title": "bad resolve payload",
        "raw_logs": ["ERROR timeout"],
    }
    r = client.post("/incidents/investigate", json=payload, headers=_AUTH)
    assert r.status_code == 200, r.text
    incident_id = r.json()["incident_id"]

    r = client.post(
        f"/incidents/{incident_id}/resolve",
        json={
            "action": "restarted",
            "confirmed_root_cause": "timeout",
        },
        headers=_AUTH,
    )
    assert r.status_code == 422