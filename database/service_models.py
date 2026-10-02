"""Tenant-scoped service configuration, schema mappings and customer connectors."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, Boolean, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


class WorkspaceConfig(Base):
    __tablename__ = "workspace_configs"

    tenant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    llm_provider: Mapped[str] = mapped_column(String(32), default="ollama", nullable=False)
    llm_model: Mapped[str] = mapped_column(String(256), default="llama3.1:8b", nullable=False)
    llm_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    llm_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_mapping: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def _connector_id() -> str:
    return uuid.uuid4().hex


class CustomerConnector(Base):
    """Tenant-bound pull/push connection to a customer's evidence source.

    Secrets are encrypted before persistence. Connector records never expose
    stored credentials through the API.
    """

    __tablename__ = "customer_connectors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_connector_id)
    tenant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="http_json")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="incidents")
    endpoint_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    http_method: Mapped[str] = mapped_column(String(8), nullable=False, default="GET")
    auth_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
