from __future__ import annotations


def test_customer_connector_lifecycle(client):
    created = client.post(
        "/service/connectors",
        json={
            "name": "Customer incident feed",
            "kind": "webhook",
            "source_type": "incidents",
            "auth_token": "webhook-secret",
            "mapping": {
                "title": "summary",
                "severity": "priority",
                "affected_services": "service",
                "raw_logs": "logs",
            },
        },
    )
    assert created.status_code == 201, created.text
    connector = created.json()
    assert connector["kind"] == "webhook"
    assert connector["configured"] is True
    assert connector["webhook_path"].endswith(f"/{connector['id']}/webhook")

    payload = {
        "summary": "Payments database timeout",
        "priority": "P1",
        "service": "payments-api",
        "logs": ["connection pool exhausted"],
    }
    first = client.post(
        connector["webhook_path"],
        headers={"X-WayPoint-Secret": "webhook-secret"},
        json={"payload": payload},
    )
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 1

    second = client.post(
        connector["webhook_path"],
        headers={"X-WayPoint-Secret": "webhook-secret"},
        json={"payload": payload},
    )
    assert second.status_code == 200, second.text
    assert second.json()["duplicates"] == 1

    bad = client.post(
        connector["webhook_path"],
        headers={"X-WayPoint-Secret": "wrong"},
        json={"payload": payload},
    )
    assert bad.status_code == 401


def test_onboarding_status_reflects_mapping_and_connector(client):
    before = client.get("/service/onboarding")
    assert before.status_code == 200
    assert before.json()["stages"][0]["complete"] is False
    assert before.json()["stages"][1]["complete"] is False

    saved = client.put(
        "/service/schema-mapping",
        json={"mapping": {"title": "summary", "severity": "priority"}},
    )
    assert saved.status_code == 200

    client.post(
        "/service/connectors",
        json={
            "name": "Customer feed",
            "kind": "webhook",
            "source_type": "incidents",
            "auth_token": "secret",
        },
    )

    after = client.get("/service/onboarding")
    assert after.status_code == 200
    stages = {stage["id"]: stage["complete"] for stage in after.json()["stages"]}
    assert stages["connect"] is True
    assert stages["understand"] is True
    assert stages["confirm"] is True


def test_mapping_proposal_never_invents_source_keys(client):
    response = client.post(
        "/service/schema-mapping/propose",
        json={
            "sample": {
                "summary": "CPU alert",
                "priority": "P1",
                "application": "payments",
            }
        },
    )
    assert response.status_code == 200
    mapping = response.json()["mapping"]
    assert set(mapping.values()).issubset({"summary", "priority", "application"})
