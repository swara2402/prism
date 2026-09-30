"""
tests.test_security
===================

Security-focused tests: auth, validation limits, CORS/security headers.

``anon_client`` runs the real authentication path with no credential, so the
rejection tests assert production behaviour rather than a test-mode bypass.
``sa_client`` authenticates with a genuine tenant-bound service-account token,
so the accepted-path tests exercise the credential lookup, hash comparison and
tenant binding that production uses.
"""
from __future__ import annotations

import pytest


def test_missing_api_key_returns_401(anon_client):
    r = anon_client.post(
        "/incidents/investigate",
        json={"title": "Test incident latency spike"},
    )
    assert r.status_code == 401
    # The message must name the header credential path, not tell an API caller
    # to "sign in" -- they cannot.
    assert "X-API-Key" in r.json().get("detail", "")


def test_wrong_api_key_returns_401(anon_client):
    r = anon_client.post(
        "/incidents/investigate",
        json={"title": "Test incident latency spike"},
        headers={"X-API-Key": "wrong-key-wrong-key-wrong-key-wrong"},
    )
    assert r.status_code == 401


def test_no_credential_never_becomes_a_tenant(anon_client):
    """An unauthenticated caller must not be handed a tenant context.

    The header the tenant used to be selected from is ignored entirely: the
    tenant is bound to the verified credential, so a spoofed value changes
    nothing and cannot be used to read another tenant's data.
    """
    r = anon_client.get(
        "/incidents",
        headers={"X-Tenant-Id": "some-other-tenant"},
    )
    assert r.status_code == 401


def test_correct_api_key_accepted(sa_client):
    # May fail deeper in the pipeline without full deps, but must not be 401.
    r = sa_client.post(
        "/incidents/investigate",
        json={
            "title": "Test incident latency spike",
            "description": "Synthetic test",
            "severity": "P3",
            "affected_services": ["svc-a"],
            "raw_logs": ["error: timeout"],
        },
    )
    assert r.status_code != 401


def test_oversized_logs_rejected(sa_client):
    huge_logs = [f"line {i} " + ("x" * 100) for i in range(600)]
    r = sa_client.post(
        "/incidents/investigate",
        json={
            "title": "Oversized logs test",
            "raw_logs": huge_logs,
        },
    )
    assert r.status_code == 422


def test_too_many_services_rejected(sa_client):
    r = sa_client.post(
        "/incidents/investigate",
        json={
            "title": "Too many services",
            "affected_services": [f"svc-{i}" for i in range(60)],
        },
    )
    assert r.status_code == 422


def test_too_many_traces_rejected(sa_client):
    r = sa_client.post(
        "/incidents/investigate",
        json={
            "title": "Too many traces",
            "traces": [{"id": i} for i in range(250)],
        },
    )
    assert r.status_code == 422


def test_health_is_unauthenticated(anon_client):
    r = anon_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_security_headers_present(anon_client):
    r = anon_client.get("/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "no-referrer"
    assert "no-store" in (r.headers.get("Cache-Control") or "")
    assert r.headers.get("X-Request-ID")


def test_memory_search_requires_auth(anon_client):
    r = anon_client.post("/memory/search", json={"query": "latency"})
    assert r.status_code == 401


def test_memory_stats_requires_auth(anon_client):
    r = anon_client.get("/memory/stats")
    assert r.status_code == 401
