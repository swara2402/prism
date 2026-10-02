"""Canonical evidence boundary for customer-specific incident schemas."""
from __future__ import annotations

from typing import Any


ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "incident_title", "name", "alert", "summary"),
    "description": ("description", "details", "message", "incident_description"),
    "severity": ("severity", "priority", "level"),
    "incident_type": ("incident_type", "type", "category"),
    "affected_services": ("affected_services", "services", "service_names", "serviceName"),
    "raw_logs": ("raw_logs", "logs", "log_lines", "events"),
    "metrics": ("metrics", "metric_data", "measurements"),
    "traces": ("traces", "trace_data", "spans"),
    "topology": ("topology", "service_topology", "dependencies"),
    "context": ("context", "metadata", "labels", "tags"),
    "started_at": ("started_at", "start_time", "timestamp", "detected_at"),
}


def _first(payload: dict[str, Any], names: tuple[str, ...]) -> tuple[Any, str | None]:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name], name
    return None, None


def infer_mapping(payload: dict[str, Any]) -> dict[str, str]:
    """Infer safe, deterministic mappings from a customer's sample payload."""
    mapping: dict[str, str] = {}
    lowered = {str(k).lower(): str(k) for k in payload}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            key = lowered.get(alias.lower())
            if key is not None:
                mapping[canonical] = key
                break
    return mapping


def normalize_evidence(
    payload: dict[str, Any], mapping: dict[str, str] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Map arbitrary customer fields into WayPoint's canonical evidence shape."""
    mapping = {**infer_mapping(payload), **(mapping or {})}
    out: dict[str, Any] = {
        "title": "Customer incident",
        "description": None,
        "severity": "P3",
        "incident_type": None,
        "affected_services": [],
        "raw_logs": [],
        "metrics": {},
        "traces": [],
        "topology": {},
        "context": {},
        "started_at": None,
    }

    for canonical, source in mapping.items():
        if canonical not in out or source not in payload:
            continue
        out[canonical] = payload[source]

    # Common customer shapes often wrap all evidence under an incident/evidence
    # object. Flatten one level without guessing beyond known canonical fields.
    if isinstance(payload.get("incident"), dict):
        nested = dict(payload["incident"])
        nested.update({k: v for k, v in payload.items() if k != "incident"})
        return normalize_evidence(nested, mapping)

    if isinstance(out["affected_services"], str):
        out["affected_services"] = [out["affected_services"]]
    if isinstance(out["raw_logs"], str):
        out["raw_logs"] = [out["raw_logs"]]
    if not isinstance(out["metrics"], dict):
        out["metrics"] = {}
    if not isinstance(out["traces"], list):
        out["traces"] = []
    if not isinstance(out["topology"], dict):
        out["topology"] = {}
    if not isinstance(out["context"], dict):
        out["context"] = {}

    return out, mapping
