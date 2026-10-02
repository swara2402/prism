"""Tenant-scoped service configuration and schema mappings."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, func
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
