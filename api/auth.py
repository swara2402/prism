"""Human login/session and workspace membership endpoints."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from auth.security import COOKIE_NAME, authenticate_login, create_access_token, hash_password, principal_from_request
from config.settings import settings
from database.auth_models import ServiceAccount, Tenant, User
from database.session import AsyncSessionLocal
from utils.rate_limit import build_rate_limiter

router = APIRouter(prefix="/auth", tags=["auth"])
_login_limiter = build_rate_limiter("5/minute", settings.redis_url)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class MemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = "viewer"
    password: str = Field(min_length=12, max_length=256)


class RoleUpdate(BaseModel):
    role: str


class ServiceAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    role: str = "engineer"
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict:
    if "@" not in body.email:
        raise HTTPException(422, "Enter a valid email address")
    ip = request.client.host if request.client else "unknown"
    allowed, retry = await _login_limiter.check(f"login:{ip}")
    if not allowed:
        raise HTTPException(429, "Too many sign-in attempts. Try again shortly.", headers={"Retry-After": str(max(1, int(retry + 0.999)))})
    principal = await authenticate_login(body.email, body.password)
    if not principal:
        raise HTTPException(401, "Email or password is incorrect")
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(User, Tenant).join(Tenant, Tenant.id == User.tenant_id).where(User.id == principal.user_id))).first()
    user, tenant = row
    token = create_access_token(user, tenant)
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=settings.is_production, samesite="strict", max_age=8 * 3600, path="/")
    return {"user": {"id": principal.user_id, "email": principal.email, "role": principal.role}, "tenant": {"id": principal.tenant_id, "name": principal.tenant_name}}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"logged_out": True}


@router.get("/me")
async def me(request: Request) -> dict:
    p = await principal_from_request(request)
    return {"user": {"id": p.user_id, "email": p.email, "role": p.role}, "tenant": {"id": p.tenant_id, "name": p.tenant_name}, "permissions": {"can_investigate": p.can("engineer"), "can_admin": p.can("admin"), "can_manage_workspace": p.can("owner")}}


@router.get("/members")
async def members(request: Request) -> list[dict]:
    p = await principal_from_request(request)
    if not p.can("admin"):
        raise HTTPException(403, "Admin permission required")
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(User).where(User.tenant_id == p.tenant_id).order_by(User.email))).scalars().all()
    return [{"id": u.id, "email": u.email, "role": u.role, "active": u.is_active} for u in rows]


@router.post("/members")
async def add_member(body: MemberCreate, request: Request) -> dict:
    p = await principal_from_request(request)
    if not p.can("admin"):
        raise HTTPException(403, "Admin permission required")
    if body.role not in {"viewer", "engineer", "admin"}:
        raise HTTPException(400, "Role must be viewer, engineer, or admin")
    async with AsyncSessionLocal() as session:
        exists = (await session.execute(select(User).where(User.email == body.email.strip().lower()))).scalar_one_or_none()
        if exists:
            raise HTTPException(409, "A user with that email already exists")
        user = User(email=body.email.strip().lower(), password_hash=hash_password(body.password), tenant_id=p.tenant_id, role=body.role, is_active=True)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return {"id": user.id, "email": user.email, "role": user.role}


@router.patch("/members/{user_id}/role")
async def change_role(user_id: str, body: RoleUpdate, request: Request) -> dict:
    p = await principal_from_request(request)
    if not p.can("owner"):
        raise HTTPException(403, "Workspace owner permission required")
    if body.role not in {"viewer", "engineer", "admin", "owner"}:
        raise HTTPException(400, "Invalid role")
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.id == user_id, User.tenant_id == p.tenant_id))).scalar_one_or_none()
        if not user:
            raise HTTPException(404, "Member not found")
        if user.id == p.user_id and body.role != "owner":
            raise HTTPException(400, "Owner cannot remove their own owner role")
        user.role = body.role
        await session.commit()
    return {"id": user.id, "email": user.email, "role": user.role}


@router.post("/service-accounts")
async def create_service_account(body: ServiceAccountCreate, request: Request) -> dict:
    """Create a tenant-bound machine credential. The token is returned once."""
    p = await principal_from_request(request)
    if not p.can("admin"):
        raise HTTPException(403, "Admin permission required")
    if body.role not in {"viewer", "engineer", "admin"}:
        raise HTTPException(400, "Invalid service-account role")
    token = "prism_sa_" + secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    account = ServiceAccount(
        tenant_id=p.tenant_id,
        name=body.name.strip(),
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        role=body.role,
        scopes=["incident.read", "incident.investigate"] if body.role != "admin" else ["*"] ,
        expires_at=expires_at,
        is_active=True,
    )
    async with AsyncSessionLocal() as session:
        session.add(account)
        await session.commit()
        await session.refresh(account)
    return {"id": account.id, "name": account.name, "tenant_id": account.tenant_id, "role": account.role, "expires_at": account.expires_at, "token": token, "warning": "Store this token securely. PRISM will not show it again."}
