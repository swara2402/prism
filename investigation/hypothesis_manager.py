"""Hypothesis management utilities for PRISM.

Provides functions to extract the top competing hypotheses from the
current hypothesis distribution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Tuple

if TYPE_CHECKING:
    from investigation.state import InvestigationState


def top_competing_hypotheses(
    hypotheses: Dict[str, float], top_n: int = 2
) -> List[str]:
    """Return the identifiers of the top‑N hypotheses sorted by probability.

    Parameters
    ----------
    hypotheses: Dict[str, float]
        Mapping of hypothesis name → probability (or score).
    top_n: int, optional
        Number of top hypotheses to return. Default is 2.
    """
    if not hypotheses:
        return []
    sorted_items = sorted(hypotheses.items(), key=lambda kv: kv[1], reverse=True)
    return [h for h, _ in sorted_items[:top_n]]


def get_competing_hypotheses(state: "InvestigationState") -> List[Tuple[str, float]]:
    """Return the two highest‑probability hypotheses from the investigation state.

    Extracts ``state.hypotheses`` (a ``Dict[str, float]``) and returns a list of
    ``(hypothesis_name, probability)`` tuples sorted by descending probability.
    If fewer than two hypotheses exist, the list will contain as many as are available.
    """
    hypotheses = state.hypotheses
    if not hypotheses:
        return []
    sorted_items = sorted(hypotheses.items(), key=lambda kv: kv[1], reverse=True)
    return sorted_items[:2]

def competition_intensity(p1: float, p2: float) -> float:
    """Compute intensity of competition between two hypothesis probabilities.

    The intensity is high when the probabilities are close (i.e., the system is unsure).
    Defined as ``1 - |p1 - p2|`` yielding a value in ``[0, 1]``.
    """
    return 1.0 - abs(p1 - p2)
