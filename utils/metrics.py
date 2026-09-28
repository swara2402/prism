"""
utils.metrics
=============

Statistical helpers for agent reliability (accuracy / precision / recall, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class ConfusionCounts:
    """Rolling confusion-matrix counters."""

    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0

    def record(self, predicted_positive: bool, actual_positive: bool) -> None:
        if predicted_positive and actual_positive:
            self.true_positives += 1
        elif predicted_positive and not actual_positive:
            self.false_positives += 1
        elif not predicted_positive and not actual_positive:
            self.true_negatives += 1
        else:
            self.false_negatives += 1

    @property
    def total(self) -> int:
        return self.true_positives + self.false_positives + self.true_negatives + self.false_negatives

    @property
    def accuracy(self) -> float:
        t = self.total
        return (self.true_positives + self.true_negatives) / t if t else 0.0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class MovingAverage:
    """Exponentially-weighted moving average."""

    value: float = 0.0
    alpha: float = 0.1
    initialized: bool = False

    def update(self, sample: float) -> float:
        if not self.initialized:
            self.value = sample
            self.initialized = True
        else:
            self.value = self.alpha * sample + (1 - self.alpha) * self.value
        return self.value


@dataclass
class StatsTracker:
    """Tracks latency / confidence moving averages for an agent."""

    latency: MovingAverage = field(default_factory=lambda: MovingAverage(alpha=0.2))
    confidence: MovingAverage = field(default_factory=lambda: MovingAverage(alpha=0.2))
    samples: int = 0

    def record(self, latency_s: float, confidence: float) -> None:
        self.latency.update(latency_s)
        self.confidence.update(confidence)
        self.samples += 1


def harmonic_mean(values: List[float]) -> float:
    """Compute harmonic mean of non-negative values (clamped to [0,1])."""
    if not values:
        return 0.0
    total = 0.0
    for v in values:
        if v <= 0:
            return 0.0
        total += 1.0 / v
    return len(values) / total
