"""
tests.test_utils
================

Unit tests for utility helpers (text classification, evidence
compression, metrics).
"""
from __future__ import annotations


from utils.evidence import compress_logs
from utils.metrics import ConfusionCounts, MovingAverage, StatsTracker, harmonic_mean
from utils.text import (
    classify_log_severity,
    extract_apis,
    extract_services,
    jaccard_similarity,
    normalize_log_line,
    tokenize,
)


def test_classify_log_severity_info():
    label, score = classify_log_severity("hello world")
    assert label == "info"
    assert score < 0.2


def test_classify_log_severity_fatal():
    label, score = classify_log_severity("FATAL panic: out of memory")
    assert score >= 0.9


def test_extract_apis():
    apis = extract_apis("GET /v1/users POST /v1/orders/123")
    assert "/v1/users" in apis
    assert "/v1/orders/123" in apis


def test_extract_services():
    svcs = extract_services("service=auth_svc, app=payment_service")
    assert "auth_svc" in svcs
    assert "payment_service" in svcs


def test_normalize_log_line():
    line = "2024-01-01T12:00:00.000Z  error  something happened"
    n = normalize_log_line(line)
    assert "<TS>" in n
    assert "  " not in n  # collapsed


def test_jaccard_similarity():
    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard_similarity(set(), set()) == 1.0
    assert jaccard_similarity({"a"}, {"b"}) == 0.0


def test_tokenize():
    toks = tokenize("Hello World hello")
    assert toks.count("hello") == 2


def test_compress_logs_basic():
    logs = [
        "2024-01-01T12:00:00Z ERROR payment-svc: timeout calling /v1/charge",
        "2024-01-01T12:00:01Z ERROR payment-svc: timeout calling /v1/charge",
        "2024-01-01T12:00:02Z INFO healthcheck ok",
        "2024-01-01T12:00:03Z FATAL OOM killer killed process 1234",
    ]
    ev = compress_logs(logs)
    assert ev.original_line_count == 4
    assert len(ev.critical_events) >= 1
    assert any("timeout" in e.lower() for e in ev.api_failures)
    assert ev.compression_ratio > 0


def test_confusion_counts():
    cc = ConfusionCounts()
    cc.record(True, True)
    cc.record(True, False)
    cc.record(False, True)
    cc.record(False, False)
    assert cc.total == 4
    assert cc.accuracy == 0.5
    assert cc.precision == 0.5
    assert cc.recall == 0.5


def test_moving_average():
    ma = MovingAverage(alpha=0.5)
    ma.update(10.0)
    assert ma.value == 10.0
    ma.update(20.0)
    assert 12.0 <= ma.value <= 16.0


def test_stats_tracker():
    st = StatsTracker()
    st.record(0.5, 0.8)
    st.record(0.6, 0.9)
    assert st.samples == 2
    assert 0.4 <= st.latency.value <= 0.7
    assert 0.7 <= st.confidence.value <= 1.0


def test_harmonic_mean():
    assert harmonic_mean([1.0, 1.0, 1.0]) == 1.0
    assert harmonic_mean([0.0, 1.0]) == 0.0
    assert harmonic_mean([]) == 0.0


def test_hypothesis_entropy_ignores_invalid_scores():
    from investigation.metrics import shannon_entropy
    assert abs(shannon_entropy({"a": 1.0, "b": 1.0}) - 1.0) < 1e-6
    assert shannon_entropy({"a": 1.0, "bad": float("nan")}) == 0.0


def test_mdv_rejects_non_finite_inputs():
    from investigation.value_engine import compute_mdv
    action = {
        "discrimination_score": float("nan"),
        "expected_uncertainty_reduction": float("inf"),
        "evidence_value": 1.0,
        "reliability_score": 1.0,
        "execution_cost": 0.0,
    }
    assert 0.0 <= compute_mdv(action) <= 1.0
