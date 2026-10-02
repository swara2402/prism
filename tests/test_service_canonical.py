from services.canonical import infer_mapping, normalize_evidence


def test_infers_common_customer_field_names():
    payload = {
        "serviceName": "payments",
        "latencyMs": 1200,
        "logs": ["timeout"],
        "priority": "P1",
    }
    mapping = infer_mapping(payload)
    assert mapping["affected_services"] == "serviceName"
    assert mapping["raw_logs"] == "logs"
    assert mapping["severity"] == "priority"


def test_normalizes_customer_payload():
    payload = {
        "incident_title": "Payment timeout",
        "priority": "P1",
        "serviceName": "payments",
        "logs": "gateway timeout",
        "metadata": {"region": "ap-south-1"},
    }
    normalized, mapping = normalize_evidence(payload)
    assert normalized["title"] == "Payment timeout"
    assert normalized["severity"] == "P1"
    assert normalized["affected_services"] == ["payments"]
    assert normalized["raw_logs"] == ["gateway timeout"]
    assert normalized["context"]["region"] == "ap-south-1"
    assert mapping["title"] == "incident_title"
