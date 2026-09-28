# investigation/diagnostic_gain.py
"""Actual Diagnostic Gain (ADG) calculation utilities.

The ADG measures the reduction in uncertainty between two
`InvestigationState` snapshots. It aggregates several metric
differences (e.g., utility, discrimination, evidence, coverage)
using configurable weights.

Missing metrics are excluded and the remaining weights are
renormalised so the total weight sums to 1. The final ADG is
clamped to the range [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import math

# ---------------------------------------------------------------------------
# Settings – default weights (can be overridden via Settings)
# ---------------------------------------------------------------------------
DEFAULT_DIAGNOSTIC_GAIN_WEIGHTS = {
    "uncertainty": 0.35,
    "disagreement": 0.25,
    "coverage": 0.25,
    "causal_consistency": 0.15,
}

@dataclass(frozen=True)
class DiagnosticGainWeights:
    """Weight configuration for ADG components using actual InvestigationState fields.

    All weights should be non‑negative. The calculation renormalises active (non‑missing) weights.
    """

    uncertainty: float = DEFAULT_DIAGNOSTIC_GAIN_WEIGHTS["uncertainty"]
    disagreement: float = DEFAULT_DIAGNOSTIC_GAIN_WEIGHTS["disagreement"]
    coverage: float = DEFAULT_DIAGNOSTIC_GAIN_WEIGHTS["coverage"]
    causal_consistency: float = DEFAULT_DIAGNOSTIC_GAIN_WEIGHTS["causal_consistency"]

    # Backwards compatibility properties
    @property
    def utility(self) -> float:
        return self.uncertainty

    @property
    def discrimination(self) -> float:
        return self.disagreement

    @property
    def evidence(self) -> float:
        return self.coverage

    def as_dict(self) -> dict[str, float]:
        return {
            "uncertainty": self.uncertainty,
            "disagreement": self.disagreement,
            "coverage": self.coverage,
            "causal_consistency": self.causal_consistency,
        }

    def active_weights(self, available: dict[str, Optional[float]]) -> dict[str, float]:
        """Return a dict of weights for metrics that are not ``None``.

        The weights are renormalised so that they sum to 1.
        """
        raw = {
            k: getattr(self, k) for k in self.as_dict().keys() if available.get(k) is not None
        }
        total = sum(raw.values())
        if total == 0:
            return {}
        return {k: v / total for k, v in raw.items()}


@dataclass(frozen=True)
class DiagnosticGainResult:
    """Result of an ADG computation.

    ``total_gain`` is the weighted sum of metric differences, clamped to
    the interval ``[0, 1]``. Individual component gains are also stored.
    """

    total_gain: float
    component_gains: dict[str, float]

    @property
    def is_positive(self) -> bool:
        return self.total_gain > 0


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def _clamp_0_1(value: float) -> float:
    return max(0.0, min(1.0, value))


def _get_val(obj: Any, *keys: str) -> Optional[float]:
    """Retrieve attribute or dict key float value if finite, else None."""
    if obj is None:
        return None
    for k in keys:
        v = getattr(obj, k, None)
        if v is None and isinstance(obj, dict):
            v = obj.get(k)
        if v is not None:
            try:
                fv = float(v)
                if math.isfinite(fv):
                    return fv
            except (ValueError, TypeError):
                pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_adg(
    prev_state: "object",
    cur_state: "object",
    weights: Optional[DiagnosticGainWeights] = None,
) -> DiagnosticGainResult:
    """Compute Actual Diagnostic Gain (ADG) between two InvestigationState snapshots.

    Metrics and Directional Differences:
    * Uncertainty (hypothesis_uncertainty): old - new  (reduction is positive gain)
    * Disagreement (agent_disagreement):    old - new  (reduction is positive gain)
    * Evidence (evidence_coverage):        new - old  (increase is positive gain)
    * Causal Consistency (causal_consistency): new - old (increase is positive gain)

    Missing / unavailable metrics (None) are excluded and active weights are renormalized.
    """
    if weights is None:
        weights = DiagnosticGainWeights()

    raw_gains: dict[str, Optional[float]] = {}

    # 1. Uncertainty: old - new
    u_old = _get_val(prev_state, "hypothesis_uncertainty", "uncertainty", "entropy")
    u_new = _get_val(cur_state, "hypothesis_uncertainty", "uncertainty", "entropy")
    if u_old is not None and u_new is not None:
        raw_gains["uncertainty"] = max(0.0, u_old - u_new)
    else:
        raw_gains["uncertainty"] = None

    # 2. Disagreement: old - new
    d_old = _get_val(prev_state, "agent_disagreement", "disagreement", "hypothesis_disagreement", "discrimination")
    d_new = _get_val(cur_state, "agent_disagreement", "disagreement", "hypothesis_disagreement", "discrimination")
    if d_old is not None and d_new is not None:
        raw_gains["disagreement"] = max(0.0, d_old - d_new)
    else:
        raw_gains["disagreement"] = None

    # 3. Evidence Coverage: new - old
    c_old = _get_val(prev_state, "evidence_coverage", "coverage", "evidence")
    c_new = _get_val(cur_state, "evidence_coverage", "coverage", "evidence")
    if c_old is not None and c_new is not None:
        raw_gains["coverage"] = max(0.0, c_new - c_old)
    else:
        raw_gains["coverage"] = None

    # 4. Causal Consistency: new - old
    # NOTE: None means the metric is unmeasurable (no graph / no findings), NOT zero.
    # When one or both states lack graph data, we skip causal_consistency from ADG
    # rather than treating absence as 0.0 which would penalise perfectly valid steps.
    cc_old = _get_val(prev_state, "causal_consistency")
    cc_new = _get_val(cur_state, "causal_consistency")
    if cc_old is not None and cc_new is not None:
        raw_gains["causal_consistency"] = max(0.0, cc_new - cc_old)
    else:
        raw_gains["causal_consistency"] = None  # excluded from ADG; weights are renormalised

    # Filter available gains
    available_gains = {k: v for k, v in raw_gains.items() if v is not None}
    active_w = weights.active_weights(available_gains)

    if not active_w:
        return DiagnosticGainResult(total_gain=0.0, component_gains={})

    weighted_sum = sum(available_gains[name] * active_w[name] for name in active_w)
    total = _clamp_0_1(weighted_sum)
    return DiagnosticGainResult(total_gain=total, component_gains=available_gains)


__all__ = ["DiagnosticGainWeights", "DiagnosticGainResult", "compute_adg"]

