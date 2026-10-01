"""investigation.value_engine
==============================

Marginal Diagnostic Value (MDV) computation.

MDV is the expected gain in diagnostic progress from executing one more action.
It combines five components:

    1. **discrimination_score**          — how well this action separates the top
                                           competing hypotheses (0–1)
    2. **expected_uncertainty_reduction** — EMA-learned reduction in hypothesis
                                           entropy for this agent/context (0–1)
    3. **evidence_value**                — 1.0 if the action's required evidence
                                           is present, 0.0 otherwise
    4. **reliability_score**             — agent's historical reliability (0–1)
    5. **cost component**                — shaped by cost_mode (see below)

Cost Modes
----------
``cost_efficiency`` (default)
    The cost component is ``1.0 - execution_cost``.  Higher cost → lower MDV.
    The cost weight is *still in the same MDV direction* as all other weights:
    a lower-cost action scores higher.

    MDV = w1·disc + w2·unc + w3·ev + w4·rel + w5·(1 - cost)

``cost_penalty``
    The cost is subtracted *after* the benefit is assembled, making it an
    explicit penalty that can drive MDV below zero before clamping:

    benefit = w1·disc + w2·unc + w3·ev + w4·rel
    MDV = clamp(benefit − w5·cost, 0, 1)

The default mode is ``cost_efficiency`` because it keeps MDV strictly in [0,1]
without surprises and behaves consistently with the rest of the weight math.
"""
from typing import List, Dict, Any, Literal
from .state import InvestigationState
from dataclasses import dataclass


CostMode = Literal["cost_efficiency", "cost_penalty"]
_VALID_COST_MODES = {"cost_efficiency", "cost_penalty"}


@dataclass
class MDVWeights:
    """Configurable weights for MDV components.

    All weights must be non-negative and sum to 1.0. The default values reflect
    the approved priorities:

    * discrimination:       0.30
    * uncertainty_reduction: 0.30
    * evidence_value:       0.20
    * reliability:          0.10
    * cost:                 0.10

    Parameters
    ----------
    cost_mode : ``"cost_efficiency"`` | ``"cost_penalty"``
        Controls how ``execution_cost`` influences MDV. See module docstring.
    """

    discrimination: float = 0.30
    uncertainty_reduction: float = 0.30
    evidence_value: float = 0.20
    reliability: float = 0.10
    cost: float = 0.10
    cost_mode: CostMode = "cost_efficiency"

    def validate(self) -> None:
        total = (
            self.discrimination
            + self.uncertainty_reduction
            + self.evidence_value
            + self.reliability
            + self.cost
        )
        if any(w < 0 for w in [self.discrimination, self.uncertainty_reduction, self.evidence_value, self.reliability, self.cost]):
            raise ValueError("MDVWeights must be non-negative")
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"MDVWeights must sum to 1.0 (got {total})")
        if self.cost_mode not in _VALID_COST_MODES:
            raise ValueError(f"cost_mode must be one of {_VALID_COST_MODES!r}; got {self.cost_mode!r}")


# Default instance – can be overridden by passing a different MDVWeights to ``compute_mdv``.
DEFAULT_MDV_WEIGHTS = MDVWeights()

# Backwards-compatibility constants (deprecated)
W1 = DEFAULT_MDV_WEIGHTS.discrimination
W2 = DEFAULT_MDV_WEIGHTS.uncertainty_reduction
W3 = DEFAULT_MDV_WEIGHTS.evidence_value
W4 = DEFAULT_MDV_WEIGHTS.reliability
W5 = DEFAULT_MDV_WEIGHTS.cost


def compute_mdv(
    action: Dict[str, Any],
    state: InvestigationState | None = None,
    weights: MDVWeights | None = None,
) -> float:
    """Compute Marginal Diagnostic Value (MDV) for a single action.

    All 5 components are normalized to [0, 1].

    Cost Modes
    ----------
    ``cost_efficiency`` (default):
        ``cost_component = 1.0 - execution_cost``
        ``MDV = w1·disc + w2·unc + w3·ev + w4·rel + w5·(1 - cost)``

    ``cost_penalty``:
        ``benefit = w1·disc + w2·unc + w3·ev + w4·rel``
        ``MDV = clamp(benefit − w5·cost, 0, 1)``

    Parameters
    ----------
    action : dict
        Action descriptor with keys: ``discrimination_score``,
        ``expected_uncertainty_reduction``, ``evidence_value``,
        ``reliability_score``, ``execution_cost``.
    state : InvestigationState or None
        Current investigation state (used to derive evidence presence and
        reliability if not directly in the action dict).
    weights : MDVWeights or None
        Weight configuration. Defaults to ``DEFAULT_MDV_WEIGHTS``.
    """
    w = weights or DEFAULT_MDV_WEIGHTS
    w.validate()

    import math

    def _unit(value: Any) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0

    disc = _unit(action.get("discrimination_score", 0.0))
    unc_red = _unit(action.get("expected_uncertainty_reduction", 0.0))

    # Evidence value: check action evidence_value key, state evidence_present, or supported_evidence
    if "evidence_value" in action:
        evidence_val = _unit(action["evidence_value"])
    else:
        supported = action.get("supported_evidence", [])
        if state is not None and hasattr(state, "metadata") and isinstance(state.metadata, dict):
            evidence_map = state.metadata.get("evidence_present", {})
        else:
            evidence_map = action.get("evidence_present", {})

        if not supported:
            # No evidence requirements → treat as fully satisfied
            evidence_val = 1.0
        else:
            # Compute coverage ratio of required evidence present
            present = sum(1 for ev in supported if evidence_map.get(ev, False))
            evidence_val = present / len(supported) if supported else 0.0
    evidence_val = _unit(evidence_val)

    # Reliability score: prefer action dict score, then state metadata, then default 0.5
    agent_name = action.get("agent_name", "")
    if "reliability_score" in action:
        rel_score = _unit(action["reliability_score"])
    elif state is not None and hasattr(state, "metadata") and isinstance(state.metadata, dict):
        rel_map = state.metadata.get("reliability_scores", {})
        rel_score = _unit(rel_map.get(agent_name, 0.5))
    else:
        rel_score = 0.5
    rel_score = _unit(rel_score)

    # Execution cost: always normalized to [0, 1]
    cost = _unit(action.get("execution_cost", 0.0))

    if disc <= 0.01 and unc_red <= 0.01:
        # Degenerate case: no discrimination or uncertainty reduction available.
        # Fall back to cost-only signal to prefer cheaper actions.
        if w.cost_mode == "cost_efficiency":
            return max(0.0, min(1.0, w.cost * (1.0 - cost)))
        else:  # cost_penalty
            return 0.0

    if w.cost_mode == "cost_efficiency":
        # cost_efficiency: cost component contributes positively (1 - cost)
        mdv = (
            w.discrimination * disc
            + w.uncertainty_reduction * unc_red
            + w.evidence_value * evidence_val
            + w.reliability * rel_score
            + w.cost * (1.0 - cost)
        )
    else:
        # cost_penalty: compute benefit first, then subtract cost as explicit penalty
        benefit = (
            w.discrimination * disc
            + w.uncertainty_reduction * unc_red
            + w.evidence_value * evidence_val
            + w.reliability * rel_score
        )
        mdv = benefit - w.cost * cost

    return max(0.0, min(1.0, mdv))


def rank_actions(state: InvestigationState, weights: MDVWeights | None = None) -> List[Dict[str, Any]]:
    """Return ``remaining_actions`` sorted by descending MDV.

    Each action dict is copied and annotated with its computed ``mdv`` value.
    """
    scored: List[Dict[str, Any]] = []
    for a in state.remaining_actions:
        mdv = compute_mdv(a, state, weights)
        a_copy = a.copy()
        a_copy["mdv"] = mdv
        scored.append(a_copy)
    return sorted(scored, key=lambda x: x["mdv"], reverse=True)
