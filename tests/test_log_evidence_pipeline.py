from utils.evidence import compress_logs


def test_log_evidence_has_stable_ids_and_temporal_correlation():
    logs = [
        "2026-09-29T10:00:00Z INFO service=payment request started",
        "2026-09-29T10:00:01Z ERROR service=payment connection refused to ledger",
        "2026-09-29T10:00:02Z ERROR service=checkout 503 payment timeout",
    ]

    evidence = compress_logs(logs)

    assert len(evidence.events) == 3
    assert all(event.evidence_id.startswith("log-") for event in evidence.events)
    assert len({event.evidence_id for event in evidence.events}) == 3
    assert evidence.services == ["payment", "checkout"]
    assert evidence.temporal_correlations
    assert any(
        correlation["relationship"] == "dependency_failure_before_service_error"
        for correlation in evidence.temporal_correlations
    )


def test_repeated_lines_keep_distinct_evidence_ids():
    line = "2026-09-29T10:00:00Z ERROR service=api request failed"
    evidence = compress_logs([line, line])

    assert len(evidence.events) == 2
    assert evidence.events[0].evidence_id != evidence.events[1].evidence_id


def test_dependency_correlation_requires_different_services():
    logs = [
        "2026-09-29T10:00:00Z ERROR service=api connection refused",
        "2026-09-29T10:00:01Z ERROR service=api 503 timeout",
    ]

    evidence = compress_logs(logs)

    assert not any(
        correlation["relationship"] == "dependency_failure_before_service_error"
        for correlation in evidence.temporal_correlations
    )


def test_log_evidence_does_not_claim_causality():
    logs = [
        "2026-09-29T10:00:00Z INFO service=api deployment started",
        "2026-09-29T10:00:03Z ERROR service=api 503 upstream timeout",
    ]

    evidence = compress_logs(logs)
    prompt = evidence.to_prompt()

    assert "TEMPORAL CORRELATIONS (ordering evidence, not causality):" in prompt
    assert "deployment_before_error" in prompt
