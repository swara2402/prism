"""
prediction.engine
=================

Predictive Incident Engine.

Predicts future incidents using:

* Historical incident frequency per service / failure type
* Recent trend slope (failure-rate acceleration)
* Service-degradation signals (recent anomalies)
* Topology exposure (number of dependents)

For each (service, failure_type) pair returns:

* ``probability`` (0..1)
* ``estimated_time_minutes`` (rough ETA)
* ``impact`` (low|medium|high|critical)
* ``rationale``
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from config.logging import get_logger
from database.repositories import list_incidents, save_prediction

logger = get_logger(__name__)


@dataclass
class Prediction:
    service: str
    predicted_failure_type: str
    probability: float
    estimated_time_minutes: Optional[int]
    impact: str  # low|medium|high|critical
    rationale: str
    evidence: Dict[str, Any] = field(default_factory=dict)


def _impact_from_probability(p: float) -> str:
    if p >= 0.75:
        return "critical"
    if p >= 0.5:
        return "high"
    if p >= 0.25:
        return "medium"
    return "low"


def _eta_minutes(p: float, base_interval_minutes: float) -> Optional[int]:
    """
    Estimate time-to-failure.

    Higher probability -> shorter ETA.  ``base_interval_minutes`` is
    the observed average time between incidents for this service.
    """
    if p <= 0.05:
        return None
    # Inverse relationship: p=1 -> very short, p=0.1 -> ~base_interval
    factor = max(0.05, 1.0 - p)
    eta = base_interval_minutes * factor
    return max(5, int(eta))


def _trend_slope(values: Sequence[float]) -> float:
    """Simple linear-regression slope of recent values."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


async def predict(
    *,
    service_filter: Optional[str] = None,
    recent_anomalies: Optional[Dict[str, List[float]]] = None,
    topology_dependents: Optional[Dict[str, int]] = None,
    persist: bool = True,
) -> List[Prediction]:
    """
    Generate predictions for every service with historical incidents.

    Parameters
    ----------
    service_filter
        If set, restrict predictions to this service.
    recent_anomalies
        Optional ``{service: [severity values]}`` from the last N
        minutes.  Used to bump probability for services showing
        active degradation.
    topology_dependents
        Optional ``{service: count}`` of dependent services.  Used to
        scale impact.
    persist
        If True, predictions are written to the ``predictions`` table.
    """
    recent_anomalies = recent_anomalies or {}
    topology_dependents = topology_dependents or {}

    # Load historical incidents
    try:
        from database.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            incidents = await list_incidents(session, limit=5000)
    except Exception as exc:
        logger.warning("prediction_load_incidents_failed error=%r", exc)
        incidents = []

    if not incidents:
        return []

    # Group incidents by (service, failure_type)
    by_pair: Dict[Tuple[str, str], List[datetime]] = defaultdict(list)
    for inc in incidents:
        for svc in (inc.affected_services or []):
            if service_filter and svc != service_filter:
                continue
            ftype = (inc.incident_type or "unknown").lower()
            ts = inc.created_at or datetime.now(timezone.utc)
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except ValueError:
                    ts = datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            by_pair[(svc, ftype)].append(ts)

    now = datetime.now(timezone.utc)
    predictions: List[Prediction] = []
    seen_pairs: Set[Tuple[str, str]] = set()

    for (svc, ftype), timestamps in by_pair.items():
        if (svc, ftype) in seen_pairs:
            continue
        seen_pairs.add((svc, ftype))
        if len(timestamps) < 1:
            continue

        # Frequency-based base probability
        sorted_ts = sorted(timestamps)
        intervals_min: List[float] = []
        for i in range(1, len(sorted_ts)):
            delta = (sorted_ts[i] - sorted_ts[i - 1]).total_seconds() / 60.0
            intervals_min.append(delta)

        avg_interval = statistics.fmean(intervals_min) if intervals_min else (60 * 24 * 30)  # 30d default
        last_incident = sorted_ts[-1]
        time_since_last_min = (now - last_incident).total_seconds() / 60.0

        # Base probability: how close are we to the average recurrence interval?
        ratio = time_since_last_min / max(1.0, avg_interval)
        base_p = min(1.0, max(0.0, ratio))

        # Trend slope on intervals (negative = more frequent = higher risk)
        slope = _trend_slope(intervals_min) if len(intervals_min) >= 2 else 0.0
        trend_boost = max(0.0, min(0.3, -slope / max(1.0, abs(avg_interval)) * 100.0))

        # Recent anomaly boost
        anomaly_values = recent_anomalies.get(svc, [])
        anomaly_boost = 0.0
        if anomaly_values:
            anomaly_boost = min(0.3, statistics.fmean(anomaly_values) * 0.3)

        probability = min(1.0, base_p + trend_boost + anomaly_boost)

        # Impact from topology dependents
        deps = topology_dependents.get(svc, 0)
        if deps >= 10 or probability >= 0.75:
            impact = "critical"
        elif deps >= 5 or probability >= 0.5:
            impact = "high"
        elif deps >= 2 or probability >= 0.25:
            impact = "medium"
        else:
            impact = "low"

        eta = _eta_minutes(probability, avg_interval)

        rationale_parts: List[str] = [
            f"{len(timestamps)} historical {ftype} incident(s) on '{svc}'.",
            f"Average recurrence interval = {avg_interval:.0f} min.",
            f"Time since last = {time_since_last_min:.0f} min.",
            f"Trend slope = {slope:.4f} (negative = increasing frequency).",
        ]
        if anomaly_boost > 0:
            rationale_parts.append(f"Recent anomalies detected (+{anomaly_boost:.2f}).")
        if deps:
            rationale_parts.append(f"Topology: {deps} dependent service(s).")

        pred = Prediction(
            service=svc,
            predicted_failure_type=ftype,
            probability=round(probability, 3),
            estimated_time_minutes=eta,
            impact=impact,
            rationale=" ".join(rationale_parts),
            evidence={
                "incident_count": len(timestamps),
                "avg_interval_minutes": round(avg_interval, 1),
                "time_since_last_minutes": round(time_since_last_min, 1),
                "trend_slope": round(slope, 4),
                "anomaly_boost": round(anomaly_boost, 3),
                "topology_dependents": deps,
            },
        )
        predictions.append(pred)

    predictions.sort(key=lambda p: -p.probability)

    if persist and predictions:
        try:
            from database.session import AsyncSessionLocal
            from sqlalchemy import delete
            from database import models as dbm

            async with AsyncSessionLocal() as session:
                # Clear existing predictions before saving new ones to prevent duplicates
                await session.execute(delete(dbm.Prediction))
                for p in predictions[:50]:
                    await save_prediction(
                        session,
                        service=p.service,
                        predicted_failure_type=p.predicted_failure_type,
                        probability=p.probability,
                        estimated_time_minutes=p.estimated_time_minutes,
                        impact=p.impact,
                        rationale=p.rationale,
                        evidence=p.evidence,
                    )
                await session.commit()
        except Exception as exc:
            logger.warning("prediction_persist_failed error=%r", exc)

    return predictions