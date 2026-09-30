"""Tenant-scoped pattern API."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.deps import require_api_key
from auth.security import principal_from_request
from database.repositories import list_approved_patterns, list_pending_patterns
from learning.pattern_generator import approve_pattern_by_id, match_approved_patterns
from models.schemas import PatternApprove, PatternOut

router = APIRouter(prefix="/patterns", tags=["patterns"], dependencies=[Depends(require_api_key)])


class MatchRequest(BaseModel):
    logs: List[str]


class MatchHit(BaseModel):
    pattern_id: str
    root_cause_hint: str
    confidence: float
    matched_signature: str
    provenance: str = "approved_historical_pattern"
    epistemic_status: str = "evidence"


async def _principal(request: Request):
    return await principal_from_request(request)


async def _tenant_pattern_rows(request: Request, approved: bool) -> list:
    """Return the authenticated tenant's patterns.

    Previously this loaded *every* tenant's patterns and filtered the rows in
    Python by re-querying the source incidents -- an unbounded read plus an
    application-side join. The repository is tenant-scoped now, so the query
    itself carries the predicate.
    """
    p = await _principal(request)
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return await (
            list_approved_patterns(session, tenant_id=p.tenant_id)
            if approved
            else list_pending_patterns(session, tenant_id=p.tenant_id)
        )


@router.get("/pending", response_model=List[PatternOut])
async def pending_patterns(request: Request) -> List[PatternOut]:
    rows = await _tenant_pattern_rows(request, approved=False)
    return [PatternOut.model_validate(r) for r in rows]


@router.get("/approved", response_model=List[PatternOut])
async def approved_patterns(request: Request) -> List[PatternOut]:
    rows = await _tenant_pattern_rows(request, approved=True)
    return [PatternOut.model_validate(r) for r in rows]


@router.post("/{pattern_id}/approve", response_model=dict)
async def approve(pattern_id: str, body: PatternApprove, request: Request) -> dict:
    if body.pattern_id != pattern_id:
        raise HTTPException(400, "pattern_id mismatch")
    p = await _principal(request)
    if not p.can("admin"):
        raise HTTPException(403, "Admin permission required")
    # The authenticated identity is the audit actor. Never trust the caller to
    # choose who approved a pattern through request JSON.
    ok = await approve_pattern_by_id(pattern_id, p.email, tenant_id=p.tenant_id)
    if not ok:
        raise HTTPException(404, "Pattern not found")
    return {"approved": True, "pattern_id": pattern_id, "approved_by": p.email}


@router.post("/match", response_model=List[MatchHit])
async def match_patterns(body: MatchRequest, request: Request) -> List[MatchHit]:
    p = await _principal(request)
    hits = await match_approved_patterns(body.logs, tenant_id=p.tenant_id)
    return [MatchHit(**h) for h in hits]
