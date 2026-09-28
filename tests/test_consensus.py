"""
tests.test_consensus
====================

Unit tests for the consensus engine.
"""
from __future__ import annotations

import pytest

from consensus.engine import reach_consensus


@pytest.mark.asyncio
async def test_consensus_agreement():
    findings = [
        {
            "agent_name": "rule_based_analyzer",
            "finding_type": "rule_based",
            "description": "matched OOM rule",
            "confidence": 0.85,
            "evidence": {},
            "root_cause_hint": "Memory exhaustion (OOM) killed the service.",
            "hypotheses": [],
        },
        {
            "agent_name": "log_analyzer",
            "finding_type": "log_analysis",
            "description": "found OOM in logs",
            "confidence": 0.7,
            "evidence": {},
            "root_cause_hint": "Memory exhaustion (OOM) detected in logs.",
            "hypotheses": [],
        },
        {
            "agent_name": "trace_analyzer",
            "finding_type": "trace_analysis",
            "description": "trace looks fine",
            "confidence": 0.2,
            "evidence": {},
            "root_cause_hint": None,
            "hypotheses": [],
        },
    ]
    reliability = {
        "rule_based_analyzer": 0.9,
        "log_analyzer": 0.7,
        "trace_analyzer": 0.6,
    }
    result = await reach_consensus(findings, reliability)
    assert result.root_cause != "undetermined"
    assert "memory" in result.root_cause.lower() or "oom" in result.root_cause.lower()
    assert result.confidence > 0.5
    assert "rule_based_analyzer" in result.voter_breakdown


@pytest.mark.asyncio
async def test_consensus_no_quorum():
    findings = [
        {
            "agent_name": "agent_a",
            "finding_type": "x",
            "description": "hint A",
            "confidence": 0.1,
            "evidence": {},
            "root_cause_hint": "Hint A",
            "hypotheses": [],
        },
        {
            "agent_name": "agent_b",
            "finding_type": "x",
            "description": "hint B",
            "confidence": 0.1,
            "evidence": {},
            "root_cause_hint": "Hint B",
            "hypotheses": [],
        },
    ]
    reliability = {"agent_a": 0.2, "agent_b": 0.2}
    result = await reach_consensus(findings, reliability, quorum_threshold=0.5)
    assert result.root_cause == "undetermined"
    assert result.confidence < 0.3


@pytest.mark.asyncio
async def test_consensus_alternatives():
    findings = [
        {
            "agent_name": "agent_a",
            "finding_type": "x",
            "description": "memory issue",
            "confidence": 0.9,
            "evidence": {},
            "root_cause_hint": "Memory exhaustion (OOM).",
            "hypotheses": [],
        },
        {
            "agent_name": "agent_b",
            "finding_type": "x",
            "description": "timeout issue",
            "confidence": 0.7,
            "evidence": {},
            "root_cause_hint": "Downstream dependency timeout.",
            "hypotheses": [],
        },
    ]
    reliability = {"agent_a": 0.8, "agent_b": 0.6}
    result = await reach_consensus(findings, reliability)
    assert len(result.alternatives) >= 1
    assert result.alternatives[0].confidence < result.confidence
