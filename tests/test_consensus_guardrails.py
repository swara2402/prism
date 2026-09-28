import pytest

from consensus.engine import reach_consensus


@pytest.mark.asyncio
async def test_single_voter_cannot_confirm_root_cause():
    result = await reach_consensus([
        {"agent_name": "rule_based", "root_cause_hint": "database timeout", "confidence": 1.0, "metadata": {}}
    ])
    assert result.root_cause == "undetermined"
    assert "required" in result.explanation


@pytest.mark.asyncio
async def test_two_independent_voters_can_reach_consensus():
    result = await reach_consensus([
        {"agent_name": "rule_based", "root_cause_hint": "database timeout", "confidence": 0.9, "metadata": {}},
        {"agent_name": "metric_analyzer", "root_cause_hint": "database timeout", "confidence": 0.9, "metadata": {}},
    ])
    assert result.root_cause == "database timeout"
    assert 0.0 <= result.confidence <= 1.0
    assert "not a calibrated probability" in result.explanation


@pytest.mark.asyncio
async def test_duplicate_same_voter_does_not_count_as_quorum():
    result = await reach_consensus([
        {"agent_name": "rule_based", "root_cause_hint": "database timeout", "confidence": 1.0, "metadata": {}},
        {"agent_name": "rule_based", "root_cause_hint": "database timeout", "confidence": 1.0, "metadata": {}},
    ])
    assert result.root_cause == "undetermined"
