"""
tests.test_knowledge_graph_store
================================

Unit tests for :mod:`knowledge_graph.store` covering the Neo4j-6.x
dict-shaped record handling and the in-memory fallback subgraph logic.
Runs without a live Neo4j server.
"""
from __future__ import annotations

from knowledge_graph.store import (
    KnowledgeGraphStore,
    LABEL_SERVICE,
    LABEL_INCIDENT,
    LABEL_ROOT_CAUSE,
    REL_CAUSED_BY,
    REL_RECENT_CHANGE,
)


def _service(name: str, tier: str = "standard") -> dict:
    return {"name": name, "tier": tier}


class TestShapeNeo4jRecords:
    """Neo4j 6.x returns graph entities as plain dicts from result.data()."""

    def test_shapes_dict_records(self):
        records = [
            {
                "s": _service("payment-svc"),
                "dep": _service("db-primary", "critical"),
                "ch": None,
                "ch_dep": None,
                "i": {"id": "inc1", "title": "boom", "severity": "P1"},
                "rc": {"description": "DB pool exhaustion"},
                "i_dep": None,
                "rc_dep": None,
            }
        ]
        out = KnowledgeGraphStore._shape_neo4j_records(records, ["payment-svc"])

        node_ids = {n["id"] for n in out["nodes"]}
        assert "Service:payment-svc" in node_ids
        assert "Service:db-primary" in node_ids
        assert "Incident:inc1" in node_ids
        assert "RootCause:DB pool exhaustion" in node_ids

        assert ("Service:payment-svc", "DEPENDS_ON", "Service:db-primary") in {
            (e["source"], e["rel"], e["target"]) for e in out["edges"]
        }
        assert {"service": "payment-svc", "root_cause": "DB pool exhaustion"} in out[
            "historical_root_causes"
        ]

    def test_shapes_dependency_changes(self):
        records = [
            {
                "s": _service("payment-svc"),
                "dep": _service("db-primary"),
                "ch": None,
                "ch_dep": {
                    "service": "db-primary",
                    "change_type": "deploy",
                    "timestamp": "2026-08-13T13:45:00Z",
                    "description": "pool resize",
                },
                "i": None,
                "rc": None,
                "i_dep": None,
                "rc_dep": None,
            }
        ]
        out = KnowledgeGraphStore._shape_neo4j_records(records, ["payment-svc"])
        assert out["recent_changes"] == [
            {
                "service": "db-primary",
                "change_type": "deploy",
                "timestamp": "2026-08-13T13:45:00Z",
                "description": "pool resize",
            }
        ]

    def test_deduplicates_fanout_records(self):
        records = [
            {
                "s": _service("payment-svc"),
                "dep": _service("redis-cache"),
                "ch": None,
                "ch_dep": {
                    "service": "redis-cache",
                    "change_type": "config",
                    "timestamp": "2026-08-13T12:10:00Z",
                },
                "i": None,
                "rc": None,
                "i_dep": None,
                "rc_dep": None,
            },
            {
                "s": _service("auth-svc"),
                "dep": _service("redis-cache"),
                "ch": None,
                "ch_dep": {
                    "service": "redis-cache",
                    "change_type": "config",
                    "timestamp": "2026-08-13T12:10:00Z",
                },
                "i": None,
                "rc": None,
                "i_dep": None,
                "rc_dep": None,
            },
        ]
        out = KnowledgeGraphStore._shape_neo4j_records(records, ["payment-svc", "auth-svc"])
        assert len(out["recent_changes"]) == 1

    def test_handles_node_style_records(self):
        """Older driver behaviour (Node objects) must keep working too."""

        class _Node:
            labels = (LABEL_SERVICE,)

            def __init__(self, **props):
                self._props = props

            def __iter__(self):
                return iter(self._props.items())

        records = [{"s": _Node(name="payment-svc"), "dep": None, "ch": None,
                    "ch_dep": None, "i": None, "rc": None, "i_dep": None, "rc_dep": None}]
        out = KnowledgeGraphStore._shape_neo4j_records(records, ["payment-svc"])
        assert any(n["id"] == "Service:payment-svc" for n in out["nodes"])


class TestInMemorySubgraph:
    """The in-memory fallback must surface change / history nodes."""

    def test_subgraph_includes_change_and_history_nodes(self):
        store = KnowledgeGraphStore()
        mem = store._mem
        mem.upsert_node(LABEL_SERVICE, "payment-svc", {"name": "payment-svc"})
        mem.upsert_node(LABEL_SERVICE, "db-primary", {"name": "db-primary"})
        mem.add_edge(LABEL_SERVICE, "payment-svc", LABEL_SERVICE, "db-primary",
                     "DEPENDS_ON")
        # A deploy change on the dependency
        mem.upsert_node("Change", "db-primary:deploy:ts",
                        {"service": "db-primary", "change_type": "deploy",
                         "timestamp": "ts", "description": "pool resize"})
        mem.add_edge("Change", "db-primary:deploy:ts", LABEL_SERVICE, "db-primary",
                     REL_RECENT_CHANGE)
        # A historical root cause linked to an incident on the service
        mem.upsert_node(LABEL_INCIDENT, "inc1", {"id": "inc1"})
        mem.upsert_node(LABEL_ROOT_CAUSE, "pool exhausted", {"description": "pool exhausted"})
        mem.add_edge(LABEL_SERVICE, "payment-svc", LABEL_INCIDENT, "inc1", "AFFECTED_BY")
        mem.add_edge(LABEL_INCIDENT, "inc1", LABEL_ROOT_CAUSE, "pool exhausted", REL_CAUSED_BY)

        sub = mem.subgraph_for_services(["payment-svc"])
        node_ids = {n["id"] for n in sub["nodes"]}
        assert "Change:db-primary:deploy:ts" in node_ids
        assert "RootCause:pool exhausted" in node_ids
