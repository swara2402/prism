# utils/validation.py
"""Utility validation helpers used across PRISM.

All functions are small, pure, and deliberately defensive – they
clamp numeric values, verify finiteness, and normalise weight dictionaries.
These helpers are imported by the new ADG, reliability, and logging code.
"""

from __future__ import annotations

from typing import Dict, Mapping


def clamp_0_1(value: float) -> float:
    """Clamp a finite numeric value to [0, 1]. Invalid values become 0."""
    import math
    value = float(value)
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))

def is_finite(value: float) -> bool:
    """Return ``True`` if *value* is a finite real number.
    """
    import math
    return math.isfinite(value)


def normalize_weights(weights: Mapping[str, float]) -> Dict[str, float]:
    """Normalize only finite positive weights; invalid/negative inputs are ignored."""
    clean = {}
    for k, v in weights.items():
        try:
            value = float(v)
        except (TypeError, ValueError):
            continue
        if is_finite(value) and value > 0:
            clean[k] = value
    total = sum(clean.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in clean.items()}

__all__ = ["clamp_0_1", "is_finite", "normalize_weights"]
