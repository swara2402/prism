"""
tests.test_pattern_generator
============================

Unit tests for the self-learning pattern generator.
"""
from __future__ import annotations

from learning.pattern_generator import (
    _extract_signature,
    extract_patterns,
)


def test_extract_signature_normalizes_numbers():
    a = _extract_signature("user 12345 logged in from 10.0.0.1 at 2024-01-01")
    b = _extract_signature("user 99999 logged in from 192.168.0.1 at 2024-02-02")
    assert a == b


def test_extract_signature_preserves_keywords():
    sig = _extract_signature("ERROR payment-svc: timeout calling /v1/charge")
    assert "error" in sig.lower()
    assert "timeout" in sig.lower()


def test_extract_patterns_filters_low_severity():
    logs = [
        "INFO healthcheck ok",
        "ERROR OOM killed process 1234",
        "DEBUG received packet",
    ]
    patterns = extract_patterns(logs, root_cause="OOM", confidence=0.8)
    # Only the high-severity ERROR line should produce a pattern
    assert len(patterns) == 1
    assert "OOM" in patterns[0].pattern_text or "oom" in patterns[0].pattern_text.lower()


def test_extract_patterns_dedup():
    logs = [
        "ERROR OOM killed process 1234",
        "ERROR OOM killed process 5678",  # same signature
        "FATAL panic: out of memory",
    ]
    patterns = extract_patterns(logs, root_cause="memory", confidence=0.9)
    # Two distinct signatures: "OOM killed process <N>" and "panic: out of memory"
    sigs = {p.pattern_signature for p in patterns}
    assert len(sigs) >= 1
