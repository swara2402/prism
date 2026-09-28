"""
api.predictions
===============

Endpoints for the Predictive Incident Engine.

* POST /predictions/run    - run prediction now (and persist)
* GET  /predictions        - list persisted predictions
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, cast

from fastapi import APIRouter, Depends, Query
from api.deps import require_api_key
from pydantic import BaseModel

from database.repositories import list_predictions
from models.schemas import PredictionOut
from prediction.engine import predict

router = APIRouter(prefix="/predictions", tags=["predictions"])


class RunPredictionsIn(BaseModel):
    service_filter: Optional[str] = None
    recent_anomalies: Optional[Dict[str, List[float]]] = None
    topology_dependents: Optional[Dict[str, int]] = None


@router.post("/run", response_model=List[PredictionOut])
async def run_predictions(
    body: RunPredictionsIn,
    _api_key: str = Depends(require_api_key),
) -> List[PredictionOut]:
    preds = await predict(
        service_filter=body.service_filter,
        recent_anomalies=body.recent_anomalies,
        topology_dependents=body.topology_dependents,
    )
    return [PredictionOut(
        id="",
        service=p.service,
        predicted_failure_type=p.predicted_failure_type,
        probability=p.probability,
        estimated_time_minutes=p.estimated_time_minutes,
        impact=p.impact,
        rationale=p.rationale,
        created_at=cast(datetime, getattr(p, "created_at", None)),
        updated_at=cast(datetime, getattr(p, "updated_at", getattr(p, "created_at", None))),
    ) for p in preds]


@router.get("", response_model=List[PredictionOut])
async def list_preds(
    limit: int = Query(default=50, ge=1, le=100),
    _api_key: str = Depends(require_api_key),
) -> List[PredictionOut]:
    from database.session import AsyncSessionLocal
    from database import models as dbm
    from sqlalchemy import delete, func, tuple_

    async with AsyncSessionLocal() as session:
        # Import select here to avoid missing import
        from sqlalchemy import select
        # Deduplicate existing predictions: keep only the latest for each (service, type)
        # Find duplicate IDs
        duplicates = await session.execute(
            select(dbm.Prediction.id)
            .where(
                tuple_(dbm.Prediction.service, dbm.Prediction.predicted_failure_type).in_(
                    select(dbm.Prediction.service, dbm.Prediction.predicted_failure_type)
                    .select_from(dbm.Prediction)
                    .group_by(dbm.Prediction.service, dbm.Prediction.predicted_failure_type)
                    .having(func.count() > 1)
                )
            )
            .order_by(dbm.Prediction.updated_at.desc())
            .offset(1)  # Skip the latest one, keep only the newest
        )
        duplicate_ids = [id for (id,) in duplicates.all()]
        if duplicate_ids:
            await session.execute(delete(dbm.Prediction).where(dbm.Prediction.id.in_(duplicate_ids)))
            await session.commit()
        
        # Fetch the deduplicated predictions
        rows = await list_predictions(session, limit=limit)
    return [PredictionOut.model_validate(r) for r in rows]