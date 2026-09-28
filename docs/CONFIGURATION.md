# PRISM — Configuration

PRISM is configured through environment variables, optionally loaded from a
`.env` file in the project root (`.env` is git-ignored — never commit it).
A documented template lives at `.env.example`.

Settings are read once at startup into a cached singleton via
`config/settings.py` (`pydantic-settings`). Environment variable names are
**case-insensitive**; unknown variables are ignored.

---

## Reference tables

### App

| Variable              | Default         | Description                                            |
|-----------------------|-----------------|--------------------------------------------------------|
| `APP_ENV`             | `development`   | App environment. `production` triggers compile-time secret validation and fail-closed CORS defaults. `test`/`testing` is used by the test suite. |
| `APP_LOG_LEVEL`       | `INFO`          | Log level (normalized to uppercase).                   |
| `APP_HOST`            | `0.0.0.0`       | Bind address for the API server.                       |
| `APP_PORT`            | `8000`          | Bind port for the API server.                          |

### API authentication

| Variable              | Default | Description                                                |
|-----------------------|---------|------------------------------------------------------------|
| `API_KEY`             | (none)  | Key sent via the `X-API-Key` header on sensitive endpoints. **Required in production** (see below). Generate one with `openssl rand -hex 32`. |
| `API_KEY_MIN_LENGTH`  | `32`    | Minimum accepted key length.                              |

### CORS

| Variable      | Default                     | Description                                              |
|---------------|-----------------------------|----------------------------------------------------------|
| `CORS_ORIGINS`| (empty)                     | Comma-separated allowed origins. Empty means environment defaults: the known localhost dev origins in development, and **no origins at all (fail closed)** in production. |

### PostgreSQL

| Variable              | Default                                                              | Description                                            |
|-----------------------|----------------------------------------------------------------------|--------------------------------------------------------|
| `DATABASE_URL`        | `postgresql+asyncpg://incident:incident_pass@localhost:5432/incident_db` | Async SQLAlchemy URL used by the application.    |
| `DATABASE_SYNC_URL`   | `postgresql+psycopg2://incident:incident_pass@localhost:5432/incident_db` | Sync URL for Alembic migrations and scripts.  |
| `DB_POOL_SIZE`        | `10`                                                                 | Async engine pool size.                               |
| `DB_MAX_OVERFLOW`     | `20`                                                                 | Max overflow connections.                             |
| `DB_ECHO`             | `false`                                                              | Echo SQL to logs (debugging).                         |

Compose note: PostgreSQL is published on host port `5433`; inside the API
container the URL uses the internal `postgres:5432` address.

### Neo4j

| Variable         | Default                | Description                                   |
|------------------|------------------------|-----------------------------------------------|
| `NEO4J_URI`      | `bolt://localhost:7687`| Bolt endpoint.                               |
| `NEO4J_USER`     | `neo4j`                | Username.                                    |
| `NEO4J_PASSWORD` | `neo4j_pass`           | Password. **Must be a strong value in production.** |

### Ollama

| Variable          | Default               | Description                              |
|-------------------|-----------------------|------------------------------------------|
| `OLLAMA_HOST`     | `http://localhost:11434` | Base URL of the Ollama server.        |
| `OLLAMA_MODEL`    | `llama3.1:8b`         | Chat model for `llm_analyzer` and structured generation. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text`  | Embedding model for memory/SQL.          |
| `LLM_TEMPERATURE` | `0.2`                 | LLM sampling temperature.                |
| `LLM_MAX_TOKENS`  | `2048`                | Max tokens per LLM call.                 |
| `LLM_REQUEST_TIMEOUT` | `60.0`            | Seconds before an LLM call times out.    |

### Embeddings & FAISS

| Variable                    | Default                | Description                              |
|-----------------------------|------------------------|------------------------------------------|
| `SENTENCE_TRANSFORMER_MODEL`| `all-MiniLM-L6-v2`     | Sentence-transformer model (384-dim).    |
| `EMBEDDING_DIM`             | `384`                  | Embedding dimension (must match model).  |
| `FAISS_INDEX_PATH`          | `<project>/data/faiss_index` | On-disk FAISS index location.      |

### Limits / rate limiting

| Variable                       | Default    | Description                                          |
|--------------------------------|------------|------------------------------------------------------|
| `MAX_CONCURRENT_INVESTIGATIONS`| `3`        | Concurrent pipeline runs (asyncio semaphore). Excess waiting requests get HTTP 429 after a 5s acquire timeout. |
| `INVESTIGATION_RATE_LIMIT`     | `10/minute`| Per-client investigation rate limit (sliding window, per tenant/API-key/IP). |
| `REDIS_URL`                    | *(empty)*  | Optional Redis URL for a shared multi-replica rate limiter. Empty → in-process limiter. |
| `REQUIRE_TENANT_HEADER`        | `false`    | When `true`, incident endpoints reject requests without `X-Tenant-Id` (400). |
| `MAX_AFFECTED_SERVICES`        | `50`       | Max services per incident.                          |
| `MAX_LOG_LINES`                | `500`      | Max raw log lines accepted per incident.            |
| `MAX_LOG_LINE_CHARS`           | `2000`     | Max length of a single log line.                    |
| `MAX_TRACES`                   | `200`      | Max trace entries per incident.                     |
| `MAX_REQUEST_BODY_BYTES`       | `1048576`  | Max HTTP request body (1 MiB). Oversized → 413.     |
| `MAX_MEMORY_QUERY_LENGTH`      | `2000`     | Max chars allowed for a memory search query.        |
| `MAX_PAGINATION_LIMIT`         | `100`      | Max `limit` for paginated list endpoints.           |
| `MAX_PREDICTION_BATCH`         | `50`       | Max predictions per batch run.                      |

### Investigation loop (MDV)

| Variable                    | Default | Description                                           |
|-----------------------------|---------|-------------------------------------------------------|
| `PRISM_MDV_THRESHOLD`       | `0.15`  | Marginal diagnostic value stop threshold for the investigation tree. |
| `PRISM_MAX_INVESTIGATION_STEPS` | `20`| Cap on tree exploration iterations per incident.      |
| `PRISM_EXPERIMENT_LOGGING`  | `false` | Log per-iteration investigation details (research mode). |

### Agent reliability

| Variable                    | Default | Description                                         |
|-----------------------------|---------|-----------------------------------------------------|
| `AGENT_DEFAULT_RELIABILITY` | `0.5`   | Starting reliability score for unseen agents.       |
| `AGENT_MIN_RELIABILITY`     | `0.2`   | Floor for reliability scores.                       |
| `AGENT_RELIABILITY_DECAY`   | `0.9`   | EMA decay factor for moving averages.               |
| `AGENT_TIMEOUT_SECONDS`     | `45.0`  | Per-agent hard timeout; a timed-out agent produces a zero-confidence error finding. |

### Consensus

| Variable                        | Default | Description                                   |
|---------------------------------|---------|-----------------------------------------------|
| `CONSENSUS_MIN_VOTERS`          | `2`     | Minimum voters to attempt consensus.          |
| `CONSENSUS_CONFIDENCE_THRESHOLD`| `0.6`   | Required confidence to lock a root cause.     |
| (quorum threshold)              | internal| Further thresholds are read defensively (`hasattr`) so older installs stay compatible. |

### Causal graph

| Variable             | Default | Description                          |
|----------------------|---------|--------------------------------------|
| `CAUSAL_MAX_DEPTH`   | `10`    | Max depth for causal path traversal. |
| `CAUSAL_MIN_CONFIDENCE` | `0.1`| Minimum edge confidence considered.  |

### Memory

| Variable                       | Default | Description                          |
|--------------------------------|---------|--------------------------------------|
| `MEMORY_TOP_K`                 | `5`     | Default results per memory search.   |
| `MEMORY_SIMILARITY_THRESHOLD`  | `0.5`   | Default similarity cutoff.           |

### Continuous learning

| Variable                         | Default | Description                                              |
|----------------------------------|---------|----------------------------------------------------------|
| `LEARNING_MODE`                  | `online`| `online` = persistent learning (product mode). `frozen` = no learning at all, for independent, reproducible experiment runs. Any other value is rejected at startup. |
| `LEARNING_REQUIRE_CONFIRMATION`  | `true`  | Only learn from incidents whose root cause was confirmed (`engineer_confirmed`, `incident_postmortem`, `external_system`, or `benchmark_label`). PRISM never learns from its own unverified consensus. |

### Feature flags

| Variable        | Default | Description                                     |
|-----------------|---------|-------------------------------------------------|
| `ENABLE_NEO4J`  | `true`  | Use the Neo4j knowledge graph when reachable (else in-memory fallback). |
| `ENABLE_OLLAMA` | `true`  | Enable LLM-backed agents.                       |
| `ENABLE_FAISS`  | `true`  | Use the FAISS index for memory (else a degraded similarity path). |

---

## Production requirements

When `APP_ENV=production`, PRISM **refuses to start** (exit code 1) unless:

1. `API_KEY` is set, ≥ `API_KEY_MIN_LENGTH` (32) characters, and not a
   well-known placeholder (e.g. `password`, `secret`, `changeme`,
   `admin`, `123456`).
2. `DATABASE_URL` does not embed a weak/default password.
3. `NEO4J_PASSWORD` is not a weak/default value.
4. `CORS_ORIGINS` is set to explicit frontend origin(s).

This is enforced in `Settings.validate_production_secrets()` at startup.

## Loading order and caching

- Base defaults → values from `.env` → real environment variables win.
- The settings object is a process-wide singleton (`@lru_cache` in
  `get_settings()`); changes to `.env` require a restart.
- The unique exception is `LEARNING_MODE=online` vs `frozen`, which is read
  at runtime from settings at the moment learning is about to run, so a
  single process always uses one consistent mode.