"""
tests.test_investigation_tree
=============================

Unit tests for the dynamic investigation tree builder.
"""
from __future__ import annotations

from investigation.tree import InvestigationTreeBuilder


def test_tree_builder_full_evidence():
    ctx = {
        "logs": ["ERROR something"],
        "metrics": {"latency": [1, 2, 3]},
        "traces": [{"service": "x", "operation": "y", "duration_ms": 10, "status": "ok"}],
        "topology": {"nodes": [], "edges": []},
        "affected_services": ["x"],
    }
    builder = InvestigationTreeBuilder()
    tree = builder.build("inc-1", "error_rate", ctx)

    assert tree.root_step == "root"
    assert "root" in tree.steps
    assert "decide" in tree.steps
    # Should have steps for each evidence type
    step_names = " ".join(s.name.lower() for s in tree.steps.values())
    assert "logs" in step_names
    assert "metrics" in step_names
    assert "traces" in step_names
    assert "topology" in step_names
    assert "memory" in step_names  # because affected_services present
    assert "knowledge graph" in step_names


def test_tree_builder_minimal_evidence():
    ctx = {"logs": ["ERROR something"]}
    builder = InvestigationTreeBuilder()
    tree = builder.build("inc-2", None, ctx)

    # Only log-based steps should be present (plus root and decide)
    step_names = " ".join(s.name.lower() for s in tree.steps.values())
    assert "logs" in step_names
    assert "metrics" not in step_names
    assert "traces" not in step_names


def test_tree_topological_order():
    ctx = {"logs": ["x"], "metrics": {"a": [1, 2]}}
    builder = InvestigationTreeBuilder()
    tree = builder.build("inc-3", None, ctx)
    order = tree.topological_order()
    assert order[0] == "root"
    assert order[-1] == "decide"
