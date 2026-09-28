"""
knowledge_graph.store
=====================

Enterprise Knowledge Graph backed by Neo4j.

Maintains relationships among:

* Services
* APIs (endpoints)
* Users (teams owning services)
* Incidents
* Root causes
* Resolutions
* Patterns

Falls back to an in-memory NetworkX graph when Neo4j is unreachable
so the system can still operate in CI / offline mode.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from config.logging import get_logger
from config.settings import settings

logger = get_logger(__name__)


# ---------------------------------------------------------------
# Schema (Cypher labels & relationship types)
# ---------------------------------------------------------------

LABEL_SERVICE = "Service"
LABEL_API = "API"
LABEL_USER = "User"
LABEL_INCIDENT = "Incident"
LABEL_ROOT_CAUSE = "RootCause"
LABEL_RESOLUTION = "Resolution"
LABEL_PATTERN = "Pattern"

REL_DEPENDS_ON = "DEPENDS_ON"
REL_OWNS = "OWNS"
REL_EXPOSES = "EXPOSES"
REL_AFFECTED_BY = "AFFECTED_BY"
REL_CAUSED_BY = "CAUSED_BY"
REL_RESOLVED_BY = "RESOLVED_BY"
REL_MATCHES_PATTERN = "MATCHES_PATTERN"
REL_RECENT_CHANGE = "RECENT_CHANGE"


# ---------------------------------------------------------------
# In-memory fallback graph
# ---------------------------------------------------------------

@dataclass
class _InMemoryKG:
    """NetworkX-based fallback KG used when Neo4j is unavailable."""

    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    edges: List[Dict[str, Any]] = field(default_factory=list)

    def upsert_node(self, label: str, key: str, props: Dict[str, Any]) -> None:
        node_id = f"{label}:{key}"
        existing = self.nodes.get(node_id, {})
        existing.update({"label": label, "key": key, "props": props})
        self.nodes[node_id] = existing

    def add_edge(
        self,
        src_label: str,
        src_key: str,
        dst_label: str,
        dst_key: str,
        rel: str,
        props: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.edges.append(
            {
                "source": f"{src_label}:{src_key}",
                "target": f"{dst_label}:{dst_key}",
                "rel": rel,
                "props": props or {},
            }
        )

    def subgraph_for_services(self, services: Sequence[str]) -> Dict[str, Any]:
        """Build a subgraph dict containing the affected services + 1-hop neighbors."""
        target_ids = {f"{LABEL_SERVICE}:{s}" for s in services}
        nodes_out: List[Dict[str, Any]] = []
        for nid, data in self.nodes.items():
            if nid in target_ids:
                nodes_out.append({"id": nid, **data})
        # Add 1-hop neighbors
        keep = set(target_ids)
        for e in self.edges:
            if e["source"] in target_ids:
                keep.add(e["target"])
            if e["target"] in target_ids:
                keep.add(e["source"])
        # Pull in Change / RootCause nodes attached to any kept node so recent
        # changes and historical root causes survive in the fallback view.
        for e in self.edges:
            if e["rel"] in (REL_RECENT_CHANGE, REL_CAUSED_BY) and (
                e["source"] in keep or e["target"] in keep
            ):
                keep.add(e["source"])
                keep.add(e["target"])
        for nid, data in self.nodes.items():
            if nid in keep and not any(n["id"] == nid for n in nodes_out):
                nodes_out.append({"id": nid, **data})

        edges_out = [e for e in self.edges if e["source"] in keep and e["target"] in keep]
        return {"nodes": nodes_out, "edges": edges_out}


# ---------------------------------------------------------------
# Store
# ---------------------------------------------------------------

class KnowledgeGraphStore:
    """
    Async wrapper around Neo4j with in-memory fallback.

    All write methods are idempotent (MERGE semantics).
    """

    _instance: Optional["KnowledgeGraphStore"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._driver = None
        self._mem = _InMemoryKG()
        self._connect()

    def _connect(self) -> None:
        if not settings.enable_neo4j:
            return
        try:
            from neo4j import AsyncGraphDatabase

            self._driver = AsyncGraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
            logger.info("neo4j_driver_created uri=%s", settings.neo4j_uri)
        except Exception as exc:
            logger.warning("neo4j_driver_create_failed error=%r", exc)
            self._driver = None

    @classmethod
    def get(cls) -> "KnowledgeGraphStore":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Drop the singleton (and its in-memory fallback graph).

        Used for strict test / evaluation isolation so scenarios do not
        inherit nodes/edges from earlier runs.
        """
        with cls._lock:
            cls._instance = None

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    async def _run_neo4j(self, query: str, **params: Any) -> None:
        """Run a Cypher write against Neo4j.  No-op when the driver is absent."""
        if self._driver is None:
            return
        try:
            async with self._driver.session() as s:
                await s.run(query, **params)
        except Exception as exc:
            logger.warning("neo4j_write_failed error=%r query=%s", exc, query[:120])

    # ---------- Node upserts ----------

    async def upsert_service(
        self,
        name: str,
        team: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> None:
        props = {"name": name}
        if team:
            props["team"] = team
        if tier:
            props["tier"] = tier
        self._mem.upsert_node(LABEL_SERVICE, name, props)
        await self._run_neo4j(
            f"MERGE (n:{LABEL_SERVICE} {{name: $name}}) SET n += $props",
            name=name, props=props,
        )
        if team:
            self._mem.upsert_node(LABEL_USER, team, {"name": team})
            self._mem.add_edge(LABEL_USER, team, LABEL_SERVICE, name, REL_OWNS)
            await self._run_neo4j(
                f"MERGE (u:{LABEL_USER} {{name: $team}}) "
                f"MERGE (n:{LABEL_SERVICE} {{name: $name}}) "
                f"MERGE (u)-[:{REL_OWNS}]->(n)",
                team=team, name=name,
            )

    async def upsert_api(
        self, path: str, method: str = "GET", service: Optional[str] = None
    ) -> None:
        key = f"{method} {path}"
        props = {"path": path, "method": method}
        self._mem.upsert_node(LABEL_API, key, props)
        await self._run_neo4j(
            f"MERGE (a:{LABEL_API} {{path: $path, method: $method}})",
            path=path, method=method,
        )
        if service:
            self._mem.add_edge(LABEL_SERVICE, service, LABEL_API, key, REL_EXPOSES)
            await self._run_neo4j(
                f"MERGE (svc:{LABEL_SERVICE} {{name: $service}}) "
                f"MERGE (a:{LABEL_API} {{path: $path, method: $method}}) "
                f"MERGE (svc)-[:{REL_EXPOSES}]->(a)",
                service=service, path=path, method=method,
            )

    async def upsert_incident(
        self, incident_id: str, title: str, severity: str = "P3"
    ) -> None:
        props = {"id": incident_id, "title": title, "severity": severity}
        self._mem.upsert_node(LABEL_INCIDENT, incident_id, props)
        await self._run_neo4j(
            f"MERGE (n:{LABEL_INCIDENT} {{id: $id}}) SET n += $props",
            id=incident_id, props=props,
        )

    async def link_incident_service(
        self, incident_id: str, service: str
    ) -> None:
        self._mem.add_edge(
            LABEL_SERVICE, service, LABEL_INCIDENT, incident_id, REL_AFFECTED_BY
        )
        await self._run_neo4j(
            f"MATCH (svc:{LABEL_SERVICE} {{name: $service}}) "
            f"MATCH (inc:{LABEL_INCIDENT} {{id: $incident_id}}) "
            f"MERGE (inc)-[:{REL_AFFECTED_BY}]->(svc)",
            service=service, incident_id=incident_id,
        )

    async def link_incident_root_cause(
        self, incident_id: str, root_cause: str
    ) -> None:
        rc_key = root_cause[:64]
        self._mem.upsert_node(LABEL_ROOT_CAUSE, rc_key, {"description": root_cause})
        self._mem.add_edge(
            LABEL_INCIDENT, incident_id, LABEL_ROOT_CAUSE, rc_key, REL_CAUSED_BY
        )
        await self._run_neo4j(
            f"MATCH (inc:{LABEL_INCIDENT} {{id: $incident_id}}) "
            f"MERGE (rc:{LABEL_ROOT_CAUSE} {{description: $root_cause}}) "
            f"MERGE (inc)-[:{REL_CAUSED_BY}]->(rc)",
            incident_id=incident_id, root_cause=root_cause,
        )

    async def link_incident_resolution(
        self, incident_id: str, resolution: str
    ) -> None:
        res_key = resolution[:64]
        self._mem.upsert_node(LABEL_RESOLUTION, res_key, {"action": resolution})
        self._mem.add_edge(
            LABEL_INCIDENT, incident_id, LABEL_RESOLUTION, res_key, REL_RESOLVED_BY
        )
        await self._run_neo4j(
            f"MATCH (inc:{LABEL_INCIDENT} {{id: $incident_id}}) "
            f"MERGE (res:{LABEL_RESOLUTION} {{action: $resolution}}) "
            f"MERGE (inc)-[:{REL_RESOLVED_BY}]->(res)",
            incident_id=incident_id, resolution=resolution,
        )

    async def link_service_dependency(
        self, src_service: str, dst_service: str, weight: float = 1.0
    ) -> None:
        self._mem.add_edge(
            LABEL_SERVICE, src_service, LABEL_SERVICE, dst_service,
            REL_DEPENDS_ON, {"weight": weight},
        )
        await self._run_neo4j(
            f"MERGE (a:{LABEL_SERVICE} {{name: $src}}) "
            f"MERGE (b:{LABEL_SERVICE} {{name: $dst}}) "
            f"MERGE (a)-[:{REL_DEPENDS_ON} {{weight: $weight}}]->(b)",
            src=src_service, dst=dst_service, weight=weight,
        )

    async def record_recent_change(
        self,
        service: str,
        change_type: str,
        timestamp: Optional[str] = None,
        description: str = "",
    ) -> None:
        ts = timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        change_id = f"{service}:{change_type}:{ts}"
        props = {
            "service": service,
            "change_type": change_type,
            "timestamp": ts,
            "description": description,
        }
        self._mem.upsert_node("Change", change_id, props)
        self._mem.add_edge(
            "Change", change_id, LABEL_SERVICE, service, REL_RECENT_CHANGE
        )
        await self._run_neo4j(
            f"MERGE (svc:{LABEL_SERVICE} {{name: $service}}) "
            f"MERGE (ch:Change {{service: $service, change_type: $change_type, timestamp: $ts}}) "
            f"SET ch += $props "
            f"MERGE (ch)-[:{REL_RECENT_CHANGE}]->(svc)",
            service=service, change_type=change_type, ts=ts, props=props,
        )

    # ---------- Queries ----------

    async def query_service_subgraph(
        self, services: Sequence[str]
    ) -> Dict[str, Any]:
        """
        Return a subgraph around ``services`` with:

        * Their dependencies
        * Their recent changes
        * Their historical root causes
        """
        if self._driver is not None:
            try:
                async with self._driver.session() as s:
                    result = await s.run(
                        """
                        UNWIND $services AS svc
                        MATCH (s:Service {name: svc})
                        OPTIONAL MATCH (s)-[:DEPENDS_ON]->(dep:Service)
                        OPTIONAL MATCH (ch:Change)-[:RECENT_CHANGE]->(s)
                        OPTIONAL MATCH (ch_dep:Change)-[:RECENT_CHANGE]->(dep)
                        OPTIONAL MATCH (i:Incident)-[:AFFECTED_BY]->(s)
                        OPTIONAL MATCH (i)-[:CAUSED_BY]->(rc:RootCause)
                        OPTIONAL MATCH (i_dep:Incident)-[:AFFECTED_BY]->(dep)
                        OPTIONAL MATCH (i_dep)-[:CAUSED_BY]->(rc_dep:RootCause)
                        RETURN s, dep, ch, ch_dep, i, rc, i_dep, rc_dep
                        """,
                        services=list(services),
                    )
                    records = await result.data()
                    return self._shape_neo4j_records(records, services)
            except Exception as exc:
                logger.warning("neo4j_query_failed error=%r", exc)

        # Fallback: in-memory graph
        sub = self._mem.subgraph_for_services(services)
        recent_changes = [
            {
                "service": e["props"].get("service"),
                "change_type": e["props"].get("change_type"),
                "timestamp": e["props"].get("timestamp"),
                "description": e["props"].get("description"),
            }
            for e in sub["edges"]
            if e["rel"] == REL_RECENT_CHANGE
        ]
        historical_root_causes: List[Dict[str, Any]] = []
        for e in sub["edges"]:
            if e["rel"] == REL_CAUSED_BY:
                node = self._mem.nodes.get(e["target"])
                if node:
                    historical_root_causes.append(
                        {
                            "service": e["source"].split(":", 1)[1],
                            "root_cause": node["props"].get("description"),
                        }
                    )
        return {
            "nodes": sub["nodes"],
            "edges": sub["edges"],
            "recent_changes": recent_changes,
            "historical_root_causes": historical_root_causes,
        }

    @staticmethod
    def _shape_neo4j_records(
        records: List[Dict[str, Any]], services: Sequence[str]
    ) -> Dict[str, Any]:
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        recent_changes: List[Dict[str, Any]] = []
        historical_root_causes: List[Dict[str, Any]] = []

        # neo4j 6.x returns graph entities as plain dicts from result.data();
        # older drivers returned Node objects. Map each RETURN alias to its
        # label so the shaping code handles both forms.
        label_by_var = {
            "s": LABEL_SERVICE,
            "dep": LABEL_SERVICE,
            "ch": "Change",
            "ch_dep": "Change",
            "i": LABEL_INCIDENT,
            "i_dep": LABEL_INCIDENT,
            "rc": LABEL_ROOT_CAUSE,
            "rc_dep": LABEL_ROOT_CAUSE,
        }

        def _props(node: Any) -> Dict[str, Any]:
            return dict(node) if not isinstance(node, dict) else node

        def _key(node: Any) -> str:
            props = _props(node)
            for candidate in ("name", "id", "description", "action"):
                if props.get(candidate):
                    return str(props[candidate])
            service, ctype = props.get("service"), props.get("change_type")
            if service:
                return f"{service}:{ctype}"
            return str(id(node))

        for r in records:
            for key, label in label_by_var.items():
                node = r.get(key)
                if node is None:
                    continue
                node_id = f"{label}:{_key(node)}"
                if node_id not in nodes:
                    nodes[node_id] = {"id": node_id, "props": _props(node)}
            s = r.get("s")
            dep = r.get("dep")
            if s and dep:
                edges.append(
                    {
                        "source": f"{LABEL_SERVICE}:{_key(s)}",
                        "target": f"{LABEL_SERVICE}:{_key(dep)}",
                        "rel": REL_DEPENDS_ON,
                    }
                )
            ch = r.get("ch")
            ch_dep = r.get("ch_dep")
            s_name = _props(s).get("name") if s else None
            dep_name = _props(dep).get("name") if dep else None
            if ch:
                recent_changes.append(
                    {
                        "service": s_name,
                        "change_type": _props(ch).get("change_type"),
                        "timestamp": _props(ch).get("timestamp"),
                        "description": _props(ch).get("description"),
                    }
                )
            if ch_dep:
                recent_changes.append(
                    {
                        "service": dep_name,
                        "change_type": _props(ch_dep).get("change_type"),
                        "timestamp": _props(ch_dep).get("timestamp"),
                        "description": _props(ch_dep).get("description"),
                    }
                )
            i = r.get("i")
            rc = r.get("rc")
            i_dep = r.get("i_dep")
            rc_dep = r.get("rc_dep")
            if i and rc:
                historical_root_causes.append(
                    {
                        "service": s_name,
                        "root_cause": _props(rc).get("description"),
                    }
                )
            if i_dep and rc_dep:
                historical_root_causes.append(
                    {
                        "service": dep_name,
                        "root_cause": _props(rc_dep).get("description"),
                    }
                )

        # De-duplicate — an UNWIND fan-out can return the same change / root
        # cause once per queried service path.
        seen_changes: set = set()
        deduped_changes: List[Dict[str, Any]] = []
        for item in recent_changes:
            key = (
                item.get("service"),
                item.get("change_type"),
                item.get("timestamp"),
            )
            if key in seen_changes:
                continue
            seen_changes.add(key)
            deduped_changes.append(item)

        seen_causes: set = set()
        deduped_causes: List[Dict[str, Any]] = []
        for item in historical_root_causes:
            key = (item.get("service"), item.get("root_cause"))
            if key in seen_causes:
                continue
            seen_causes.add(key)
            deduped_causes.append(item)

        return {
            "nodes": list(nodes.values()),
            "edges": edges,
            "recent_changes": deduped_changes,
            "historical_root_causes": deduped_causes,
        }

    async def update_after_investigation(
        self,
        incident_id: str,
        affected_services: Sequence[str],
        root_cause: str,
        resolution: Optional[str],
    ) -> None:
        """Update the KG after an investigation completes."""
        # The incident node must exist before linking services / causes to it
        # (the Neo4j links MATCH on the incident id).
        await self.upsert_incident(incident_id, title=f"Incident {incident_id}")
        for svc in affected_services:
            await self.upsert_service(svc)
            await self.link_incident_service(incident_id, svc)
        await self.link_incident_root_cause(incident_id, root_cause)
        if resolution:
            await self.link_incident_resolution(incident_id, resolution)
