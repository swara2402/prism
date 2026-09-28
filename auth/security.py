"""Secure local authentication, JWT sessions, RBAC and tenant context."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import HTTPException, Request, status
from sqlalchemy import select

from config.settings import settings
from database.auth_models import Tenant, User
from database.session import AsyncSessionLocal

ALGORITHM = "HS256"
COOKIE_NAME = "prism_session"
ROLE_ORDER = {"viewer": 10, "engineer": 20, "admin": 30, "owner": 40}

@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    tenant_id: str
    role: str
    tenant_name: str
    def can(self, role: str) -> bool:
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER.get(role, 999)

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256": return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False

def create_access_token(user: User, tenant: Tenant) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user.id, "email": user.email, "tenant_id": user.tenant_id, "role": user.role,
               "tenant_name": tenant.name, "iat": now,
               "exp": now + timedelta(hours=settings.auth_session_hours), "jti": secrets.token_hex(16)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)

def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])

async def authenticate_login(email: str, password: str) -> Optional[Principal]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User, Tenant).join(Tenant, Tenant.id == User.tenant_id).where(
            User.email == email.strip().lower(), User.is_active.is_(True), Tenant.is_active.is_(True)))
        row = result.first()
        if not row: return None
        user, tenant = row
        if not verify_password(password, user.password_hash): return None
        return Principal(user.id, user.email, user.tenant_id, user.role, tenant.name)

async def principal_from_request(request: Request) -> Principal:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "): token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Please sign in to PRISM", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Your PRISM session has expired. Please sign in again.", headers={"WWW-Authenticate": "Bearer"})
    user_id, tenant_id, role = claims.get("sub"), claims.get("tenant_id"), claims.get("role")
    if not user_id or not tenant_id or role not in ROLE_ORDER:
        raise HTTPException(status_code=401, detail="Invalid PRISM session")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User, Tenant).join(Tenant, Tenant.id == User.tenant_id).where(
            User.id == user_id, User.tenant_id == tenant_id, User.is_active.is_(True), Tenant.is_active.is_(True)))
        row = result.first()
    if not row: raise HTTPException(status_code=401, detail="Account is no longer active")
    user, tenant = row
    if user.role != role: raise HTTPException(status_code=401, detail="Session permissions changed. Please sign in again.")
    return Principal(user.id, user.email, user.tenant_id, user.role, tenant.name)

def enforce_route_permissions(request: Request, principal: Principal) -> None:
    path, method = request.url.path.rstrip("/"), request.method.upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"} and not principal.can("engineer"):
        raise HTTPException(status_code=403, detail="Engineer permission required for this action")
    if any(path.startswith(p) for p in ("/patterns/", "/kg/", "/agents/", "/predictions/")) and method != "GET" and not principal.can("admin"):
        raise HTTPException(status_code=403, detail="Admin permission required for this action")
    if path.endswith("/approve") and not principal.can("admin"):
        raise HTTPException(status_code=403, detail="Admin permission required to approve patterns")

async def bootstrap_owner() -> None:
    if not settings.bootstrap_email or not settings.bootstrap_password: return
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is not None: return
        tenant = Tenant(name=settings.bootstrap_tenant_name or "My PRISM Workspace")
        session.add(tenant); await session.flush()
        session.add(User(email=settings.bootstrap_email.strip().lower(), password_hash=hash_password(settings.bootstrap_password),
                         tenant_id=tenant.id, role="owner", is_active=True))
        await session.commit()
