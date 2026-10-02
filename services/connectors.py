"""Customer evidence connectors.

The connector boundary is intentionally provider-neutral: customers can expose
logs, metrics, traces or incident exports through a JSON HTTP endpoint, or push
events to a signed webhook. Every record crosses the same canonicalization
boundary before it can become an incident.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken

from config.settings import settings
from database.service_models import CustomerConnector
from services.canonical import normalize_evidence


MAX_CONNECTOR_RESPONSE_BYTES = 2_000_000
ALLOWED_KINDS = {"http_json", "webhook"}
ALLOWED_SOURCE_TYPES = {"logs", "metrics", "traces", "incidents"}


def _fernet() -> Fernet:
    seed = (settings.llm_credential_secret or settings.signing_secret).encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ValueError("Stored connector credential cannot be decrypted") from exc


def validate_endpoint(url: str) -> str:
    """Validate an outbound connector URL and reject obvious SSRF targets."""
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Connector endpoint must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("Connector endpoint must not contain embedded credentials")
    host = parsed.hostname.rstrip(".").lower()
    blocked_names = {"localhost", "localhost.localdomain", "metadata.google.internal"}
    if host in blocked_names or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Connector endpoint host is not allowed")
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise ValueError("Connector endpoint must not target a private or local IP address")
    except ValueError as exc:
        if "must not target" in str(exc):
            raise
        # Hostname: resolve it once and reject private/local answers.
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as resolve_exc:
            raise ValueError("Connector endpoint hostname could not be resolved") from resolve_exc
        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved or addr.is_multicast:
                raise ValueError("Connector endpoint resolves to a private or local address")
    if parsed.scheme == "http" and settings.is_production:
        raise ValueError("Production connectors must use HTTPS")
    return value


def _headers(connector: CustomerConnector) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json", "User-Agent": "WayPoint-Connector/1.0"}
    raw = decrypt_secret(connector.headers_encrypted)
    if raw:
        extra = json.loads(raw)
        if not isinstance(extra, dict):
            raise ValueError("Connector headers must be a JSON object")
        for key, value in extra.items():
            if str(key).lower() in {"authorization", "cookie", "proxy-authorization"}:
                raise ValueError("Sensitive auth headers must use the connector token field")
            headers[str(key)] = str(value)
    token = decrypt_secret(connector.auth_token_encrypted)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _extract_path(payload: Any, path: str | None) -> Any:
    if not path:
        return payload
    current = payload
    for part in [p for p in path.split(".") if p]:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


async def fetch_connector(connector: CustomerConnector) -> tuple[int, Any]:
    if connector.kind != "http_json":
        raise ValueError("This connector is push-only; use its webhook endpoint")
    if not connector.endpoint_url:
        raise ValueError("Connector endpoint is required")
    url = validate_endpoint(connector.endpoint_url)
    method = connector.http_method.upper()
    if method not in {"GET", "POST"}:
        raise ValueError("Connector HTTP method must be GET or POST")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        response = await client.request(method, url, headers=_headers(connector))
        response.raise_for_status()
        if len(response.content) > MAX_CONNECTOR_RESPONSE_BYTES:
            raise ValueError("Connector response is too large")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Connector endpoint did not return JSON") from exc
    return response.status_code, _extract_path(payload, connector.payload_path)


def records_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "incidents", "events", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return [payload]


def normalize_connector_record(
    record: Any, *, source_type: str, mapping: dict[str, str] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    if isinstance(record, dict):
        payload = dict(record)
    elif source_type == "logs":
        payload = {"logs": [str(record)]}
    else:
        payload = {"message": str(record)}

    if source_type == "metrics" and "metrics" not in payload:
        payload = {"metrics": payload, **payload}
    elif source_type == "traces" and "traces" not in payload:
        payload = {"traces": payload.get("spans", [payload]), **payload}

    return normalize_evidence(payload, mapping)


def stable_idempotency_key(connector_id: str, normalized: dict[str, Any]) -> str:
    material = json.dumps(normalized, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(f"{connector_id}:{material}".encode("utf-8")).hexdigest()[:128]


def connector_summary(connector: CustomerConnector) -> dict[str, Any]:
    return {
        "id": connector.id,
        "name": connector.name,
        "kind": connector.kind,
        "source_type": connector.source_type,
        "endpoint_url": connector.endpoint_url,
        "http_method": connector.http_method,
        "payload_path": connector.payload_path,
        "mapping": connector.mapping or {},
        "enabled": connector.enabled,
        "schedule_seconds": connector.schedule_seconds,
        "last_sync_at": connector.last_sync_at,
        "last_status": connector.last_status,
        "last_error": connector.last_error,
        "configured": bool(connector.auth_token_encrypted or connector.headers_encrypted),
        "webhook_path": f"/service/connectors/{connector.id}/webhook" if connector.kind == "webhook" else None,
    }
