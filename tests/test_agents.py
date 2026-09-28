"""
tests.test_agents
=================

Unit tests for individual agents.
"""
from __future__ import annotations

import pytest

from agents.log_analyzer import LogAnalyzerAgent
from agents.metric_analyzer import MetricAnalyzerAgent
from agents.rule_based_analyzer import RuleBasedAnalyzerAgent
from agents.trace_analyzer import TraceAnalyzerAgent
from agents.registry import list_agent_names, instantiate_all


@pytest.mark.asyncio
async def test_rule_based_agent_oom():
    agent = RuleBasedAnalyzerAgent()
    ctx = {"logs": ["2024-01-01 FATAL OOM killed process 1234 in payment-svc"]}
    payload = await agent.run(ctx)
    assert payload.confidence > 0.5
    assert payload.root_cause_hint is not None
    assert "memory" in payload.root_cause_hint.lower() or "oom" in payload.root_cause_hint.lower()


@pytest.mark.asyncio
async def test_rule_based_agent_no_signal():
    agent = RuleBasedAnalyzerAgent()
    ctx = {"logs": ["hello world"]}
    payload = await agent.run(ctx)
    assert payload.confidence <= 0.4


@pytest.mark.asyncio
async def test_log_analyzer_agent_timeout():
    agent = LogAnalyzerAgent()
    ctx = {
        "logs": [
            "2024-01-01 ERROR payment-svc: timeout calling /v1/charge",
            "2024-01-01 ERROR payment-svc: connection refused from auth-svc",
            "2024-01-01 WARN retrying",
        ],
        "affected_services": ["payment-svc"],
    }
    payload = await agent.run(ctx)
    assert payload.finding_type == "log_analysis"
    assert payload.evidence.get("compression_ratio") is not None
    # Should surface either timeout or refused in API failures
    assert len(payload.evidence.get("api_failures", [])) >= 1


@pytest.mark.asyncio
async def test_metric_analyzer_agent_anomaly():
    agent = MetricAnalyzerAgent()
    ctx = {
        "metrics": {
            "payment_svc_p99_latency_ms": [100, 110, 105, 100, 95, 120, 5000],
        }
    }
    payload = await agent.run(ctx)
    assert payload.finding_type == "metric_analysis"
    assert len(payload.evidence["anomalies"]) >= 1


@pytest.mark.asyncio
async def test_metric_analyzer_ignores_scalar_metrics():
    agent = MetricAnalyzerAgent()
    payload = await agent.run({"metrics": {"latency_ms": 1200}})

    assert payload.finding_type == "metric_analysis"
    assert payload.evidence["metrics_inspected"] == ["latency_ms"]


@pytest.mark.asyncio
async def test_trace_analyzer_agent():
    agent = TraceAnalyzerAgent()
    ctx = {
        "traces": [
            {"service": "payment-svc", "operation": "charge", "duration_ms": 800, "status": "ok"},
            {"service": "auth-svc", "operation": "verify", "duration_ms": 50, "status": "error"},
        ]
    }
    payload = await agent.run(ctx)
    assert payload.finding_type == "trace_analysis"
    assert payload.evidence["slowest_service"] == "payment-svc"
    assert len(payload.evidence["error_spans"]) == 1


def test_registry_lists_all_agents():
    names = list_agent_names()
    assert "rule_based_analyzer" in names
    assert "log_analyzer" in names
    assert "metric_analyzer" in names
    assert "trace_analyzer" in names
    assert "topology_analyzer" in names
    assert "historical_analyzer" in names
    assert "knowledge_graph_analyzer" in names
    assert "llm_analyzer" in names


def test_registry_instantiate_all():
    instances = instantiate_all()
    for name, agent in instances.items():
        assert agent.name == name
