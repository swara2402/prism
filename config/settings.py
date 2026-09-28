"""
config.settings
===============

Centralized configuration for the Enterprise Agentic AI Incident
Investigation Framework (PRISM).

All settings are loaded from environment variables (or a local `.env`
file) via ``pydantic-settings``.  Importing :data:`settings` anywhere in
the codebase yields a singleton configuration object.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Weak / default passwords that must never be used in production
_WEAK_PASSWORDS = {
    "incident_pass",
    "neo4j_pass",
    "password",
    "pass",
    "secret",
    "changeme",
    "change-me",
    "change_me",
    "change-me-to-a-long-random-secret-at-least-32-chars",
    "change_me_strong_password",
    "admin",
    "root",
    "neo4j",
    "postgres",
    "test",
    "123456",
    "password123",
}


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -------- PostgreSQL --------
    database_url: str = Field(
        default="postgresql+asyncpg://incident:incident_pass@localhost:5432/incident_db",
        description="Async SQLAlchemy database URL",
    )
    database_sync_url: str = Field(
        default="postgresql+psycopg2://incident:incident_pass@localhost:5432/incident_db",
        description="Sync SQLAlchemy database URL (for migrations / scripts)",
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # -------- Neo4j --------
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j_pass"

    # -------- Ollama / LLM --------
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    ollama_embed_model: str = "nomic-embed-text"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_request_timeout: float = 60.0

    # -------- Embeddings --------
    sentence_transformer_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # -------- FAISS --------
    faiss_index_path: str = str(PROJECT_ROOT / "data" / "faiss_index")

    # -------- App --------
    app_env: str = "development"
    app_log_level: str = "INFO"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # -------- API Authentication --------
    api_key: Optional[str] = Field(
        default=None,
        description="API key required for sensitive endpoints (X-API-Key header)",
    )
    api_key_min_length: int = 32

    # -------- CORS --------
    cors_origins: str = Field(
        default="",
        description="Comma-separated list of allowed origins. Empty = safe defaults by env.",
    )

    # -------- Rate / concurrency limits --------
    max_concurrent_investigations: int = 3
    investigation_rate_limit: str = "10/minute"
    # When true, incident endpoints reject requests that omit X-Tenant-Id.
    # Off by default for backward compatibility; enable for strict isolation.
    require_tenant_header: bool = False
    # Optional Redis URL enabling a shared (multi-replica) rate limiter.
    # Empty = in-process sliding window.
    redis_url: str = ""
    max_affected_services: int = 50
    max_log_lines: int = 500
    max_log_line_chars: int = 2000
    max_traces: int = 200
    max_request_body_bytes: int = 1_048_576  # 1 MiB
    max_memory_query_length: int = 2000
    max_pagination_limit: int = 100
    max_prediction_batch: int = 50

    # -------- Background job queue (Phase 14/16) --------
    # Start an in-process worker that drains investigation_jobs when true.
    # Keep false in tests / standard dev so the synchronous and SSE endpoints
    # behave exactly as before; enable on a production deploy (or run
    # ``python -m worker`` as a separate process).
    job_worker_enabled: bool = False
    job_poll_interval: float = 1.0
    job_attempts_max: int = 3

    # -------- MDV and Investigation Limits --------
    MDV_THRESHOLD: float = Field(default=0.15, description="MDV stop threshold", env="PRISM_MDV_THRESHOLD")
    MAX_INVESTIGATION_STEPS: int = Field(default=20, description="Maximum investigation iterations", env="PRISM_MAX_INVESTIGATION_STEPS")
    
    # -------- Agent Reliability --------
    agent_default_reliability: float = 0.5
    agent_min_reliability: float = 0.2
    agent_reliability_decay: float = 0.9  # EMA decay for moving averages
    agent_timeout_seconds: float = 45.0
    # -------- Continuous Learning / Experiment REPRODUCIBILITY --------
    # online = persistent learning (product mode). frozen = no learning at
    # all, so every evaluation run starts from identical state and
    # scenarios stay statistically independent.
    learning_mode: str = Field(
        default="online",
        description="Learning mode: 'online' (persistent) or 'frozen' (reproducible experiments)",
        min_length=1,
    )
    # Authoritative-learning guard: NO learning from PRISM's own unverified
    # consensus.  Only incidents whose root cause has been confirmed
    # (engineer_confirmed / incident_postmortem / external_system /
    # benchmark_label) may update patterns, memory, KG causal edges, or
    # agent reliability.
    learning_require_confirmation: bool = Field(
        default=True,
        description="Refuse learning when the root cause has not been confirmed by a human/external source",
    )
    # -------- Experiment Logging --------
    PRISM_EXPERIMENT_LOGGING: bool = Field(
        default=False,
        description="Enable experimental logging of investigation iterations",
    )

    # -------- Consensus --------
    consensus_min_voters: int = 2
    consensus_confidence_threshold: float = 0.6

    # -------- Causal Graph --------
    causal_max_depth: int = 10
    causal_min_confidence: float = 0.1

    # -------- Memory --------
    memory_top_k: int = 5
    memory_similarity_threshold: float = 0.5

    # -------- Enabled external integrations --------
    enable_neo4j: bool = True
    enable_ollama: bool = True
    enable_faiss: bool = True

    @field_validator("app_log_level")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        return v.upper()

    @field_validator("learning_mode")
    @classmethod
    def _validate_learning_mode(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in {"online", "frozen"}:
            raise ValueError("learning_mode must be 'online' or 'frozen'")
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env.lower() in {"test", "testing"}

    @property
    def data_dir(self) -> Path:
        if self.is_test:
            # Keep test fixtures out of the repo and out of prod/dev data so
            # evaluation runs start from a clean, reproducible state.
            import tempfile

            path = Path(tempfile.gettempdir()) / "prism_test_data"
        else:
            path = PROJECT_ROOT / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cors_origins_list(self) -> List[str]:
        """Parsed list of allowed CORS origins."""
        raw = (self.cors_origins or "").strip()
        if raw:
            return [o.strip() for o in raw.split(",") if o.strip()]
        if self.is_production:
            return []  # fail closed — must configure explicitly
        # Development defaults
        return [
            "http://localhost:3000",
            "http://localhost:8000",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8000",
        ]

    def validate_production_secrets(self) -> None:
        """
        Refuse to start in production when critical secrets are missing
        or still set to well-known weak defaults.
        """
        if not self.is_production:
            return

        errors: List[str] = []

        if not self.api_key:
            errors.append("API_KEY is not configured")
        elif len(self.api_key) < self.api_key_min_length:
            errors.append(
                f"API_KEY is too short (min {self.api_key_min_length} characters)"
            )
        elif self.api_key.lower() in _WEAK_PASSWORDS:
            errors.append("API_KEY is still set to a placeholder/default value")

        # Check DB password embedded in URL
        db_lower = self.database_url.lower()
        for weak in _WEAK_PASSWORDS:
            if weak in db_lower:
                errors.append(
                    f"DATABASE_URL contains a weak/default password ({weak!r})"
                )
                break

        if self.neo4j_password.lower() in _WEAK_PASSWORDS:
            errors.append(
                f"NEO4J_PASSWORD is weak/default ({self.neo4j_password!r})"
            )

        if not self.cors_origins_list:
            errors.append(
                "CORS_ORIGINS must be set to explicit frontend origin(s) in production"
            )

        if errors:
            msg = (
                "PRISM refused to start in production due to insecure configuration:\n  - "
                + "\n  - ".join(errors)
            )
            print(msg, file=sys.stderr)
            raise SystemExit(1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    s = Settings()
    s.validate_production_secrets()
    return s


# Module-level singleton
settings = get_settings()
