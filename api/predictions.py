"""
api.predictions
===============

Endpoints for the Predictive Incident Engine.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Dict, List, Optional, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api.deps import require_api_key, require_tenant
from config.settings import settings
from database.repositories import list_predictions
from models.schemas import PredictionOut
from prediction.engine import predict

router = APIRouter(
    prefix="/predictions",
    tags=["predictions"],
    dependencies=[Depends(require_api_key)],
)


class RunPredictionsIn(BaseModel):
    service_filter: Optional[str] = Field(default=None, max_length=200)
    recent_anomalies: Optional[Dict[str, List[float]]] = None
    topology_dependents: Optional[Dict[str, int]] = None


@router.post("/run", response_model=List[PredictionOut])
async def run_predictions(
    body: RunPredictionsIn,
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> List[PredictionOut]:
    """Run a new prediction pass. The prediction engine owns persistence."""
    preds = await predict(
        service_filter=body.service_filter,
        recent_anomalies=body.recent_anomalies,
        topology_dependents=body.topology_dependents,
        tenant_id=tenant,
    )
    now = datetime.now(timezone.utc)
    return [
        PredictionOut(
            id=f"{p.service}:{p.predicted_failure_type}",
            service=p.service,
            predicted_failure_type=p.predicted_failure_type,
            probability=min(1.0, max(0.0, float(p.probability))) if math.isfinite(float(p.probability)) else 0.0,
            estimated_time_minutes=p.estimated_time_minutes,
            impact=p.impact,
            rationale=p.rationale,
            created_at=cast(datetime, getattr(p, "created_at", None) or now),
            updated_at=cast(datetime, getattr(p, "updated_at", None) or getattr(p, "created_at", None) or now),
        )
        for p in preds
    ]


@router.get("", response_model=List[PredictionOut])
async def list_preds(
    limit: int = Query(default=50, ge=1, le=settings.max_prediction_batch),
    _api_key: str = Depends(require_api_key),
    tenant: str = Depends(require_tenant),
) -> List[PredictionOut]:
    """Read predictions without mutating application state.

    The previous implementation deleted duplicate rows during a GET request.
    Reads should never perform destructive cleanup, so deduplication belongs in
    the write path / a maintenance job instead.
    """
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = await list_predictions(session, limit=limit, tenant_id=tenant)
    return [PredictionOut.model_validate(r) for r in rows]
