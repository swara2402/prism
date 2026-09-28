"""
tests.test_security
===================

Security-focused tests: auth, validation limits, concurrency, CORS headers.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client():
    # Ensure test env
    os.environ["APP_ENV"] = "test"
    os.environ["API_KEY"] = "test-api-key-that-is-long-enough-32chars"
    from config.settings import get_settings
    get_settings.cache_clear()
    from main import app
    with TestClient(app) as client:
        yield client


def test_missing_api_key_returns_401(app_client):
    r = app_client.post(
        "/incidents/investigate",
        json={"title": "Test incident latency spike"},
    )
    assert r.status_code == 401
    assert "X-API-Key" in r.json().get("detail", "") or "Missing" in r.json().get("detail", "")


def test_wrong_api_key_returns_401(app_client):
    r = app_client.post(
        "/incidents/investigate",
        json={"title": "Test incident latency spike"},
        headers={"X-API-Key": "wrong-key-wrong-key-wrong-key-wrong"},
    )
    assert r.status_code == 401


def test_correct_api_key_accepted(app_client):
    # May fail deeper in pipeline without full deps, but must not be 401
    r = app_client.post(
        "/incidents/investigate",
        json={
            "title": "Test incident latency spike",
            "description": "Synthetic test",
            "severity": "P3",
            "affected_services": ["svc-a"],
            "raw_logs": ["error: timeout"],
        },
        headers={"X-API-Key": "test-api-key-that-is-long-enough-32chars"},
    )
    assert r.status_code != 401


def test_oversized_logs_rejected(app_client):
    huge_logs = [f"line {i} " + ("x" * 100) for i in range(600)]
    r = app_client.post(
        "/incidents/investigate",
        json={
            "title": "Oversized logs test",
            "raw_logs": huge_logs,
        },
        headers={"X-API-Key": "test-api-key-that-is-long-enough-32chars"},
    )
    assert r.status_code == 422


def test_too_many_services_rejected(app_client):
    r = app_client.post(
        "/incidents/investigate",
        json={
            "title": "Too many services",
            "affected_services": [f"svc-{i}" for i in range(60)],
        },
        headers={"X-API-Key": "test-api-key-that-is-long-enough-32chars"},
    )
    assert r.status_code == 422


def test_too_many_traces_rejected(app_client):
    r = app_client.post(
        "/incidents/investigate",
        json={
            "title": "Too many traces",
            "traces": [{"id": i} for i in range(250)],
        },
        headers={"X-API-Key": "test-api-key-that-is-long-enough-32chars"},
    )
    assert r.status_code == 422


def test_health_is_unauthenticated(app_client):
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_security_headers_present(app_client):
    r = app_client.get("/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "no-referrer"
    assert "no-store" in (r.headers.get("Cache-Control") or "")
    assert r.headers.get("X-Request-ID")


def test_memory_search_requires_auth(app_client):
    r = app_client.post("/memory/search", json={"query": "latency"})
    assert r.status_code == 401


def test_memory_stats_requires_auth(app_client):
    r = app_client.get("/memory/stats")
    assert r.status_code == 401
