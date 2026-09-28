from __future__ import annotations

import pytest

from config.settings import Settings
from memory.store import MemoryStore


def test_production_postgres_password_validation_does_not_match_url_scheme() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://incident:CorrectHorseBatteryStaple@db:5432/incident_db",
        database_sync_url="postgresql+psycopg2://incident:CorrectHorseBatteryStaple@db:5432/incident_db",
        api_key="a" * 48,
        neo4j_password="a-strong-neo4j-secret",
        cors_origins="https://waypoint.example.com",
    )
    settings.validate_production_secrets()


def test_production_rejects_default_database_password() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://incident:incident_pass@db:5432/incident_db",
        database_sync_url="postgresql+psycopg2://incident:incident_pass@db:5432/incident_db",
        api_key="a" * 48,
        neo4j_password="a-strong-neo4j-secret",
        cors_origins="https://waypoint.example.com",
    )
    with pytest.raises(SystemExit):
        settings.validate_production_secrets()


def test_memory_store_isolation_uses_distinct_instances() -> None:
    MemoryStore.reset()
    tenant_a = MemoryStore.get("tenant-a")
    tenant_b = MemoryStore.get("tenant-b")
    assert tenant_a is not tenant_b
    assert tenant_a._tenant_scope == "tenant-a"
    assert tenant_b._tenant_scope == "tenant-b"
    assert tenant_a._index_path != tenant_b._index_path
    MemoryStore.reset()
