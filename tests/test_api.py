"""
tests.test_api
==============

End-to-end API smoke tests using FastAPI's TestClient.

Uses SQLite in-memory and disables external integrations.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make sure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENABLE_NEO4J", "false")
os.environ.setdefault("ENABLE_OLLAMA", "false")
os.environ.setdefault("ENABLE_FAISS", "false")


@pytest.fixture
def client():
    """Provide a FastAPI TestClient with an initialized in-memory DB."""
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

    with TestClient(app, headers={"X-API-Key": "test-api-key-that-is-long-enough-32chars"}) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_list_agents(client):
    r = client.get("/agents")
    assert r.status_code == 200
    names = [a["name"] for a in r.json()]
    assert "rule_based_analyzer" in names


def test_memory_stats(client):
    r = client.get("/memory/stats")
    assert r.status_code == 200
    assert "size" in r.json()


def test_kg_subgraph(client):
    r = client.post("/kg/services/subgraph", json={"services": ["x"]})
    assert r.status_code == 200
    assert "nodes" in r.json()


def test_investigate_full_pipeline(client):
    payload = {
        "title": "Payment service errors",
        "severity": "P2",
        "incident_type": "error_rate",
        "affected_services": ["payment-svc"],
        "raw_logs": [
            "2024-01-01T12:00:00Z ERROR payment-svc: OOM killed process 1234",
            "2024-01-01T12:00:01Z ERROR payment-svc: timeout calling /v1/charge",
            "2024-01-01T12:00:02Z WARN retrying",
        ],
        "metrics": {
            "payment_svc_p99_latency_ms": [100, 110, 105, 100, 95, 120, 5000],
            "payment_svc_error_rate": [0.01, 0.01, 0.02, 0.01, 0.5],
        },
        "traces": [
            {"service": "payment-svc", "operation": "charge", "duration_ms": 800, "status": "error"},
        ],
        "topology": {
            "nodes": [{"id": "payment-svc"}, {"id": "auth-svc"}],
            "edges": [{"source": "payment-svc", "target": "auth-svc"}],
        },
    }
    r = client.post("/incidents/investigate", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "incident_id" in body
    assert body["root_cause"]["root_cause"] != ""
    assert "agents_used" in body
    assert len(body["agents_used"]) >= 1


def test_list_incidents_after_investigate(client):
    # First create one
    payload = {
        "title": "test incident",
        "raw_logs": ["ERROR OOM killed process"],
    }
    r = client.post("/incidents/investigate", json=payload)
    assert r.status_code == 200
    incident_id = r.json()["incident_id"]

    # Then list
    r = client.get("/incidents")
    assert r.status_code == 200
    assert any(i["id"] == incident_id for i in r.json())


def test_get_incident_by_id(client):
    payload = {"title": "test incident", "raw_logs": ["ERROR timeout"]}
    r = client.post("/incidents/investigate", json=payload)
    incident_id = r.json()["incident_id"]

    r = client.get(f"/incidents/{incident_id}")
    assert r.status_code == 200
    assert r.json()["id"] == incident_id


def test_resolve_incident(client):
    payload = {"title": "test incident", "raw_logs": ["ERROR timeout"]}
    r = client.post("/incidents/investigate", json=payload)
    incident_id = r.json()["incident_id"]

    r = client.post(
        f"/incidents/{incident_id}/resolve",
        json={"action": "Restarted service", "steps": ["scale up"], "verified": True},
    )
    assert r.status_code == 200
    assert r.json()["action"] == "Restarted service"


def test_investigate_stream(client):
    payload = {
        "title": "Stream test incident",
        "severity": "P2",
        "incident_type": "latency",
        "affected_services": ["payment-svc", "auth-svc"],
        "raw_logs": ["ERROR payment-svc timeout"],
    }
    r = client.post("/incidents/investigate/stream", json=payload)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    text = r.text
    assert "event: pipeline_started" in text
    assert "event: incident_persisted" in text
    assert "event: verdict" in text

