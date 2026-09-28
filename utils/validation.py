# utils/validation.py
"""Utility validation helpers used across PRISM.

All functions are small, pure, and deliberately defensive – they
clamp numeric values, verify finiteness, and normalise weight dictionaries.
These helpers are imported by the new ADG, reliability, and logging code.
"""

from __future__ import annotations

from typing import Dict, Mapping


def clamp_0_1(value: float) -> float:
    """Clamp *value* to the interval ``[0, 1]``.
    """
    return max(0.0, min(1.0, value))


def is_finite(value: float) -> bool:
    """Return ``True`` if *value* is a finite real number.
    """
    import math
    return math.isfinite(value)


def normalize_weights(weights: Mapping[str, float]) -> Dict[str, float]:
    """Normalize a mapping of weights so that they sum to ``1``.

    Zero‑sum inputs return an empty dict.
    """
    total = sum(weights.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in weights.items()}

__all__ = ["clamp_0_1", "is_finite", "normalize_weights"]
