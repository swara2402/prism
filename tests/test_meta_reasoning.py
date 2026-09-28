"""
tests.test_meta_reasoning
=========================

Unit tests for the meta-reasoning engine.
"""
from __future__ import annotations

from meta_reasoning.engine import evaluate


def test_meta_reasoning_classifies_agents():
    findings = [
        {
            "agent_name": "good_agent",
            "finding_type": "x",
            "description": "correct hint",
            "confidence": 0.9,
            "latency_s": 0.2,
            "root_cause_hint": "Memory exhaustion (OOM)",
        },
        {
            "agent_name": "bad_agent",
            "finding_type": "x",
            "description": "wrong hint",
            "confidence": 0.8,
            "latency_s": 5.0,
            "root_cause_hint": "Disk full on host abc",
        },
        {
            "agent_name": "abstainer",
            "finding_type": "x",
            "description": "no signal",
            "confidence": 0.1,
            "latency_s": 0.1,
            "root_cause_hint": None,
        },
    ]
    result = evaluate(
        incident_id="inc-1",
        findings=findings,
        agents_skipped=[{"agent": "skipped_one", "reason": "missing evidence"}],
        final_root_cause="Memory exhaustion (OOM)",
        final_confidence=0.85,
        agents_used=["good_agent", "bad_agent", "abstainer"],
        persist=False,
    )
    assert "good_agent" in result.useful_agents
    assert "bad_agent" in result.unnecessary_agents
    assert "abstainer" in result.unnecessary_agents
    assert result.optimal_path.startswith("good_agent")
    assert any("skip" in s.lower() for s in result.suggestions)


def test_meta_reasoning_low_confidence_suggestion():
    findings = [
        {
            "agent_name": "agent_a",
            "finding_type": "x",
            "description": "weak hint",
            "confidence": 0.2,
            "latency_s": 0.1,
            "root_cause_hint": "unknown cause",
        },
    ]
    result = evaluate(
        incident_id="inc-2",
        findings=findings,
        agents_skipped=[],
        final_root_cause="unknown cause",
        final_confidence=0.3,
        agents_used=["agent_a"],
        persist=False,
    )
    assert any("confidence" in s.lower() for s in result.suggestions)
