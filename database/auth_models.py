"""Identity models for users, tenants and tenant-bound machine credentials."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.session import Base


def _id() -> str:
    return uuid.uuid4().hex


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    service_accounts: Mapped[list["ServiceAccount"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    """A workspace member.

    Email is unique *per tenant*, not globally. A global unique constraint let
    any workspace admin permanently claim an address, denying every other
    workspace the ability to onboard that person, and made a 409 response leak
    that the address is registered somewhere.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="_tenant_email_uc"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="viewer", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    tenant: Mapped[Tenant] = relationship(back_populates="users")


class ServiceAccount(Base):
    """Tenant-bound machine identity. Plaintext tokens are never persisted."""
    __tablename__ = "service_accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="engineer", nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    tenant: Mapped[Tenant] = relationship(back_populates="service_accounts")
