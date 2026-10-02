"""Service-facing ingestion, workspace configuration and schema mapping APIs."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from api.deps import require_api_key, require_tenant
from config.settings import settings
from database.service_models import WorkspaceConfig
from database.session import AsyncSessionLocal
from models.schemas import IncidentCreate
from services.canonical import infer_mapping, normalize_evidence

router = APIRouter(prefix="/service", tags=["service"])


def _fernet() -> Fernet:
    seed = (settings.llm_credential_secret or settings.signing_secret).encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
    return Fernet(key)


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        raise HTTPException(500, "Stored LLM credential cannot be decrypted")


def _require_admin(request: Request) -> None:
    role = getattr(getattr(request.state, "principal", None), "role", "")
    if role not in {"admin", "owner"}:
        raise HTTPException(403, "Workspace administration permission required")


class LLMConfigIn(BaseModel):
    provider: str = Field(default="ollama", min_length=2, max_length=32)
    model: str = Field(default="llama3.1:8b", min_length=1, max_length=256)
    api_key: str | None = Field(default=None, max_length=4096)
    base_url: str | None = Field(default=None, max_length=512)


class LLMConfigOut(BaseModel):
    provider: str
    model: str
    base_url: str | None
    configured: bool
    api_key_masked: str | None


class SchemaMappingIn(BaseModel):
    mapping: dict[str, str] = Field(default_factory=dict)


class SchemaMappingOut(BaseModel):
    mapping: dict[str, str]


class IngestRequest(BaseModel):
    data: dict[str, Any] = Field(..., min_length=1)
    mapping: dict[str, str] | None = None
    run_investigation: bool = False


class IngestOut(BaseModel):
    incident_id: str | None
    normalized: dict[str, Any]
    mapping: dict[str, str]
    investigation: Any | None = None


class IngestBatchRequest(BaseModel):
    items: list[dict[str, Any]] = Field(..., min_length=1, max_length=100)
    mapping: dict[str, str] | None = None
    run_investigation: bool = False


class IngestBatchItem(BaseModel):
    index: int
    success: bool
    result: IngestOut | None = None
    error: str | None = None


class IngestBatchOut(BaseModel):
    items: list[IngestBatchItem]
    succeeded: int
    failed: int


async def _get_config(tenant_id: str) -> WorkspaceConfig:
    async with AsyncSessionLocal() as session:
        cfg = await session.get(WorkspaceConfig, tenant_id)
        if cfg is None:
            cfg = WorkspaceConfig(tenant_id=tenant_id)
            session.add(cfg)
            await session.commit()
            await session.refresh(cfg)
        return cfg


@router.get("/llm", response_model=LLMConfigOut)
async def get_llm_config(
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> LLMConfigOut:
    _require_admin(request)
    cfg = await _get_config(tenant)
    return LLMConfigOut(
        provider=cfg.llm_provider,
        model=cfg.llm_model,
        base_url=cfg.llm_base_url,
        configured=bool(cfg.llm_api_key_encrypted) or cfg.llm_provider == "ollama",
        api_key_masked="••••••••" if cfg.llm_api_key_encrypted else None,
    )


@router.put("/llm", response_model=LLMConfigOut)
async def set_llm_config(
    body: LLMConfigIn,
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> LLMConfigOut:
    _require_admin(request)
    provider = body.provider.strip().lower()
    allowed = {"ollama", "openai", "openai_compatible"}
    if provider not in allowed:
        raise HTTPException(422, "Unsupported LLM provider. Use ollama, openai, or openai_compatible")

    async with AsyncSessionLocal() as session:
        cfg = await session.get(WorkspaceConfig, tenant)
        if cfg is None:
            cfg = WorkspaceConfig(tenant_id=tenant)
            session.add(cfg)
        cfg.llm_provider = provider
        cfg.llm_model = body.model.strip()
        cfg.llm_base_url = body.base_url.strip() if body.base_url else None
        if body.api_key is not None and body.api_key.strip():
            cfg.llm_api_key_encrypted = _encrypt(body.api_key.strip())
        await session.commit()
        await session.refresh(cfg)

    return LLMConfigOut(
        provider=cfg.llm_provider,
        model=cfg.llm_model,
        base_url=cfg.llm_base_url,
        configured=bool(cfg.llm_api_key_encrypted) or cfg.llm_provider == "ollama",
        api_key_masked="••••••••" if cfg.llm_api_key_encrypted else None,
    )


@router.get("/schema-mapping", response_model=SchemaMappingOut)
async def get_schema_mapping(
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> SchemaMappingOut:
    cfg = await _get_config(tenant)
    return SchemaMappingOut(mapping=cfg.schema_mapping or {})


@router.put("/schema-mapping", response_model=SchemaMappingOut)
async def set_schema_mapping(
    body: SchemaMappingIn,
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> SchemaMappingOut:
    _require_admin(request)
    async with AsyncSessionLocal() as session:
        cfg = await session.get(WorkspaceConfig, tenant)
        if cfg is None:
            cfg = WorkspaceConfig(tenant_id=tenant)
            session.add(cfg)
        cfg.schema_mapping = body.mapping
        await session.commit()
    return SchemaMappingOut(mapping=body.mapping)


@router.post("/ingest", response_model=IngestOut, status_code=201)
async def ingest(
    body: IngestRequest,
    request: Request,
    response: Response,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> IngestOut:
    """Accept customer-shaped data, normalize it, persist it, optionally investigate."""
    cfg = await _get_config(tenant)
    merged_mapping = {**(cfg.schema_mapping or {}), **(body.mapping or {})}
    normalized, inferred = normalize_evidence(body.data, merged_mapping)
    if body.mapping:
        inferred = {**inferred, **body.mapping}

    try:
        incident = IncidentCreate.model_validate(normalized)
    except Exception as exc:
        raise HTTPException(422, f"Customer data could not be normalized: {exc}") from exc

    # Investigation requests go through the durable job queue. The API
    # persists customer data only once and returns immediately with a 202-style
    # job payload embedded in the existing response contract.
    investigation = None
    if body.run_investigation:
        response.status_code = 202
        from utils.job_queue import enqueue_investigation
        from datetime import datetime, timezone

        payload = {
            "incident": incident.model_dump(mode="json"),
            "tenant_id": tenant,
            "request_id": getattr(request.state, "request_id", "ingest"),
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        job = await enqueue_investigation(
            payload,
            idempotency_key=None,
            tenant_id=tenant,
            attempts_max=settings.job_attempts_max,
        )
        investigation = {
            "job_id": job.id,
            "status": job.status,
            "progress": job.progress,
        }
        incident_id = None
    else:
        from database.repositories import create_incident
        async with AsyncSessionLocal() as session:
            inc = await create_incident(
                session,
                tenant_id=tenant,
                title=incident.title,
                description=incident.description,
                severity=incident.severity,
                status="open",
                incident_type=incident.incident_type,
                affected_services=incident.affected_services,
                raw_logs=incident.raw_logs,
                metrics=incident.metrics,
                traces=incident.traces,
                topology=incident.topology,
                context=incident.context,
                started_at=incident.started_at,
            )
            await session.commit()
            incident_id = inc.id

    return IngestOut(
        incident_id=incident_id,
        normalized=normalized,
        mapping=inferred,
        investigation=investigation,
    )


@router.post("/llm/test")
async def test_llm_connection(
    request: Request,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    _require_admin(request)
    cfg = await _get_config(tenant)
    from utils.llm import LLMClient
    client = LLMClient(
        host=cfg.llm_base_url,
        model=cfg.llm_model,
        provider=cfg.llm_provider,
        api_key=_decrypt(cfg.llm_api_key_encrypted),
        timeout=10.0,
    )
    ok = await client.test_connection()
    return {"ok": ok, "provider": cfg.llm_provider, "model": cfg.llm_model}



@router.post("/ingest/batch", response_model=IngestBatchOut, status_code=201)
async def ingest_batch(
    body: IngestBatchRequest,
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> IngestBatchOut:
    """Ingest a bounded batch with per-record validation and persistence results.

    Batch ingestion intentionally persists only. Investigations are submitted
    through the investigation/job API so one slow LLM call cannot hold a batch
    transaction open.
    """
    if body.run_investigation:
        raise HTTPException(
            422,
            "Batch ingestion does not run investigations synchronously; ingest first, then submit investigations through the job API.",
        )

    cfg = await _get_config(tenant)
    merged_mapping = {**(cfg.schema_mapping or {}), **(body.mapping or {})}
    results: list[IngestBatchItem] = []

    from database.repositories import create_incident

    async with AsyncSessionLocal() as session:
        for index, payload in enumerate(body.items):
            try:
                normalized, inferred = normalize_evidence(payload, merged_mapping)
                incident = IncidentCreate.model_validate(normalized)
                inc = await create_incident(
                    session,
                    tenant_id=tenant,
                    title=incident.title,
                    description=incident.description,
                    severity=incident.severity,
                    status="open",
                    incident_type=incident.incident_type,
                    affected_services=incident.affected_services,
                    raw_logs=incident.raw_logs,
                    metrics=incident.metrics,
                    traces=incident.traces,
                    topology=incident.topology,
                    context=incident.context,
                    started_at=incident.started_at,
                )
                results.append(
                    IngestBatchItem(
                        index=index,
                        success=True,
                        result=IngestOut(
                            incident_id=inc.id,
                            normalized=normalized,
                            mapping={**inferred, **(body.mapping or {})},
                            investigation=None,
                        ),
                    )
                )
            except Exception as exc:
                results.append(
                    IngestBatchItem(
                        index=index,
                        success=False,
                        error=f"Customer data could not be ingested: {exc}",
                    )
                )

        # Successful rows are committed even when individual records failed.
        await session.commit()

    succeeded = sum(1 for item in results if item.success)
    return IngestBatchOut(
        items=results,
        succeeded=succeeded,
        failed=len(results) - succeeded,
    )


@router.post("/schema-mapping/infer", response_model=SchemaMappingOut)
async def infer_schema_mapping(
    body: dict[str, Any],
    _auth: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> SchemaMappingOut:
    return SchemaMappingOut(mapping=infer_mapping(body))
