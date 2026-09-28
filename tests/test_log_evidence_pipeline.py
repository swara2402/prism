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
    assert evidence.services == ["payment", "checkout"]
    assert evidence.temporal_correlations
    assert any(
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

    assert "TEMPORAL CORRELATIONS:" in prompt
    assert "deployment_before_error" in prompt
