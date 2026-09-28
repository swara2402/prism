"""Human login/session endpoints for the PRISM console."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from auth.security import COOKIE_NAME, authenticate_login, create_access_token, principal_from_request
from config.settings import settings
from database.auth_models import User, Tenant
from database.session import AsyncSessionLocal
from sqlalchemy import select

router=APIRouter(prefix="/auth",tags=["auth"])
class LoginRequest(BaseModel):
    email: str=Field(min_length=3,max_length=320)
    password: str=Field(min_length=1,max_length=256)

@router.post("/login")
async def login(body:LoginRequest,response:Response)->dict:
    if "@" not in body.email: raise HTTPException(422,"Enter a valid email address")
    principal=await authenticate_login(body.email,body.password)
    if not principal: raise HTTPException(status_code=401,detail="Email or password is incorrect")
    async with AsyncSessionLocal() as session:
        row=(await session.execute(select(User,Tenant).join(Tenant,Tenant.id==User.tenant_id).where(User.id==principal.user_id))).first()
    user,tenant=row; token=create_access_token(user,tenant)
    response.set_cookie(COOKIE_NAME,token,httponly=True,secure=settings.is_production,samesite="strict",max_age=8*3600,path="/")
    return {"user":{"id":principal.user_id,"email":principal.email,"role":principal.role},"tenant":{"id":principal.tenant_id,"name":principal.tenant_name}}

@router.post("/logout")
async def logout(response:Response)->dict:
    response.delete_cookie(COOKIE_NAME,path="/"); return {"logged_out":True}

@router.get("/me")
async def me(request:Request)->dict:
    principal=await principal_from_request(request)
    return {"user":{"id":principal.user_id,"email":principal.email,"role":principal.role},"tenant":{"id":principal.tenant_id,"name":principal.tenant_name},"permissions":{"can_investigate":principal.can("engineer"),"can_admin":principal.can("admin"),"can_manage_workspace":principal.can("owner")}}
