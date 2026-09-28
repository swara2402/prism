# PRISM — API Reference

Complete reference for the PRISM HTTP API. Interactive docs are served at
`/docs` (Swagger UI) and the OpenAPI JSON at `/openapi.json` when the app is
running.

---

## Authentication

Most endpoints require an **API key**. When `API_KEY` is set in `.env` (or the
app is in `production`), send it as a header:

```
X-API-Key: <your-key>
```

- Comparison is constant-time (`hmac.compare_digest`).
- In `production`, startup is refused if `API_KEY` is missing, shorter than
  `API_KEY_MIN_LENGTH` (32), or a well-known weak value.
- In development with no key configured, endpoints run in open mode.

**Unauthenticated endpoints**: `GET /health`, `GET /ready`, `GET /docs`,
`GET /openapi.json`, `GET /` (console), `GET /api/info`.

### Request ID

Every response carries `X-Request-ID`. If you send an inbound
`X-Request-ID`, it is propagated (and logged); otherwise the middleware assigns
a UUID.

### Idempotency (investigations)

Both investigation endpoints accept an `Idempotency-Key` header:

```
Idempotency-Key: e7f2a9...
```

A repeated request with the same key for the same incident **replays the
previously persisted result** instead of re-running the agent pipeline.

---

## Endpoint Summary

| Method | Path                              | Auth | Description                                    |
|--------|-----------------------------------|------|------------------------------------------------|
| GET    | `/health`                         | no   | Minimal liveness probe                         |
| GET    | `/internal/health`                | yes  | Deep subsystem status (DB/Neo4j/Ollama/FAISS) |
| GET    | `/ready`                          | no   | Readiness probe (503 until ready)              |
| GET    | `/api/info`                       | no   | Service metadata + endpoint list               |
| GET    | `/`                               | no   | Incident command console (HTML)                |
| POST   | `/incidents/investigate`          | yes  | Run the full investigation pipeline            |
| POST   | `/incidents/investigate/stream`   | yes  | Same, with SSE progress events                 |
| GET    | `/incidents`                      | yes  | List recent incidents                          |
| GET    | `/incidents/{id}`                 | yes  | Get one incident                               |
| GET    | `/incidents/{id}/root-cause`      | yes  | Get the consensus root cause                   |
| POST   | `/incidents/{id}/resolve`         | yes  | Record resolution (+ learning trigger)         |
| GET    | `/patterns/pending`               | yes  | Pending patterns                               |
| GET    | `/patterns/approved`              | yes  | Approved patterns                              |
| POST   | `/patterns/{id}/approve`          | yes  | Approve a pattern                              |
| POST   | `/patterns/match`                 | yes  | Match logs against approved patterns           |
| POST   | `/memory/search`                  | yes  | Semantic search over incident memory           |
| GET    | `/memory/stats`                   | yes  | Memory index stats                             |
| POST   | `/kg/services`                    | yes  | Upsert a service                               |
| POST   | `/kg/apis`                        | yes  | Upsert an API endpoint                         |
| POST   | `/kg/services/{name}/depends-on/{dep}` | yes | Record a service dependency                |
| POST   | `/kg/changes`                     | yes  | Record a recent change                         |
| POST   | `/kg/services/subgraph`           | yes  | Fetch dependency subgraph for services         |
| GET    | `/agents`                         | yes  | List agents + reliability stats                |
| GET    | `/agents/{name}`                  | yes  | One agent's reliability stats                  |
| POST   | `/predictions/run`                | yes  | Run / persist predictions                      |
| GET    | `/predictions`                    | yes  | List persisted predictions                     |

---

## Health & Metadata

### `GET /health`

Minimal liveness — never leaks infrastructure detail.

```json
{ "status": "ok" }
```

### `GET /internal/health`  (auth required)

```
X-API-Key: ...
```

Deep subsystem report:

```json
{
  "status": "ok",
  "env": "development",
  "version": "1.1.0",
  "memory_size": 12,
  "subsystems": {
    "database": "connected",
    "memory": "ready",
    "faiss": "ready",
    "neo4j": "connected",
    "ollama": "available"
  }
}
```

`neo4j` reports `fallback_mode` when the in-memory graph is in use; `ollama`
reports `unavailable` until the configured models are reachable.

### `GET /ready`

```json
{ "status": "ready", "checks": { "database": true, "memory": true } }
```

Returns HTTP `503` with `"status": "not_ready"` until the DB answers
`SELECT 1` and memory is loaded.

### `GET /api/info`

```json
{
  "name": "PRISM — Enterprise Agentic AI Incident Investigation Framework",
  "version": "1.1.0",
  "docs": "/docs",
  "auth": "X-API-Key header required on sensitive endpoints when API_KEY is set",
  "endpoints": ["/incidents/investigate", "..."]
}
```

---

## Investigations

### `POST /incidents/investigate`  (auth required)

Runs the full pipeline: redact → persist → investigation tree (MDV/ADG loop) →
causal graph → confidence propagation → consensus → persist root cause →
explainability → meta-reasoning.

**Headers**

```
Content-Type: application/json
X-API-Key: <key>
Idempotency-Key: <optional, max 128 chars>
```

**Request body** — `IncidentCreate`

| Field              | Type                | Required | Notes                                   |
|--------------------|---------------------|----------|-----------------------------------------|
| `title`            | string              | yes      | 3–512 chars                             |
| `description`      | string              | no       | ≤ 10,000 chars                          |
| `severity`         | string              | no       | `P0`–`P4` (default `P3`)                |
| `incident_type`    | string              | no       | e.g. `latency`, `error_rate`, `availability`, `data`, `security` |
| `affected_services`| string[]            | no       | ≤ `max_affected_services` (50)          |
| `raw_logs`         | string[]            | no       | ≤ `max_log_lines` (500), each ≤ 2000 chars |
| `metrics`          | object              | no       | service → value series                  |
| `traces`           | object[]            | no       | ≤ `max_traces` (200)                    |
| `topology`         | object              | no       | dependency / topology data              |
| `context`          | object              | no       | free-form additional context            |
| `started_at`       | datetime            | no       | ISO-8601                                |

**Response** — `InvestigationResult` (200 OK):

```json
{
  "incident_id": "abc123...",
  "root_cause": {
    "incident_id": "abc123...",
    "root_cause": "Database connection pool exhaustion.",
    "confidence": 0.72,
    "alternatives": [
      { "cause": "Network latency to DB", "confidence": 0.31, "evidence": ["..."] }
    ],
    "explanation": "Consensus root cause: 'Database connection pool exhaustion.' ...",
    "causal_chain": [ { "node_id": "...", "label": "..." } ],
    "contributing_factors": ["..."]
  },
  "explanation": {
    "incident_id": "abc123...",
    "evidence_used": [ { "agent": "metric_analyzer", "evidence": { "...": "..." } } ],
    "confidence_breakdown": {},
    "graph_reasoning": "root -> symptom chain ...",
    "alternative_root_causes": [],
    "final_explanation": "..."
  },
  "meta_reasoning": {
    "incident_id": "abc123...",
    "useful_agents": ["rule_based_analyzer"],
    "unnecessary_agents": ["llm_analyzer"],
    "optimal_path": "rule_based_analyzer:32ms; metric_analyzer:41ms",
    "suggestions": ["..."],
    "agent_scores": { "rule_based_analyzer": 0.8 }
  },
  "agents_used": ["rule_based_analyzer", "metric_analyzer"],
  "duration_seconds": 1.234,
  "agent_statuses": [
    { "agent_name": "rule_based_analyzer", "status": "ok", "execution_ms": 32.1, "finding_type": "analysis" }
  ],
  "status": "completed",
  "runtime": {
    "prism_version": "1.1.0",
    "llm_model": "llama3.1:8b",
    "learning_mode": "online",
    "mdv_threshold": 0.15,
    "database_url_masked": "postgresql+asyncpg://***@localhost:5433/incident_db"
  }
}
```

**Failure semantics**: `agent_statuses` lists every agent with
`status: ok | failed | skipped` and its execution time. If any agent failed,
`status` becomes `"completed_with_degraded_agents"`.

**Errors**

| Status | Meaning                                                    |
|--------|------------------------------------------------------------|
| 401    | Missing or invalid `X-API-Key`                            |
| 413    | Request body exceeds `MAX_REQUEST_BODY_BYTES`             |
| 422    | Validation failure (bad field, excess logs/services, etc.) |
| 429    | Too many concurrent investigations (semaphore timeout)     |

### `POST /incidents/investigate/stream`  (auth required)

Same input / auth as `/investigate`, but returns `text/event-stream`. Events
(`event:` names) in order:

| Event                  | Payload includes                                      |
|------------------------|-------------------------------------------------------|
| `pipeline_started`     | `message`, `request_id`, `title`                      |
| `incident_persisted`   | `incident_id`, `title`, `severity`, `affected_services` |
| `agents_dispatched`    | `incident_type`, `affected_services`, `message`       |
| `agent_completed`      | `agent`, `finding_type`, `confidence`, `summary`      |
| `causal_graph_built`   | `nodes_count`, `edges_count`, `message`               |
| `confidence_propagated`| `iterations`, `candidate_count`, `top_candidate`      |
| `consensus_reached`    | `root_cause`, `confidence`, `alternatives_count`      |
| `verdict`              | `root_cause`, `confidence`, `explanation`             |
| `explanation_built`    | `summary`                                             |
| `learning_deferred`    | `detail` (learning waits for confirmed resolution)    |
| `investigation_result` | the full `InvestigationResult` object                 |
| `error`                | `error` (e.g. concurrency limit)                      |
| `idempotent_replay`    | `incident_id`, `detail` (repeat under same key)       |

On an idempotent replay, the stream emits `idempotent_replay` followed by
`investigation_result` and ends.

### `GET /incidents?limit=50`  (auth required)

Lists recent incidents (max `max_pagination_limit`). Each `IncidentOut`:

```json
{
  "id": "abc123...",
  "title": "Login latency spike",
  "description": null,
  "severity": "P2",
  "status": "investigating",
  "incident_type": "latency",
  "affected_services": ["auth-service", "user-service"],
  "created_at": "2024-01-01T12:00:03Z",
  "resolved_at": null
}
```

### `GET /incidents/{id}`  (auth required)

Returns one `IncidentOut` (404 when not found).

### `GET /incidents/{id}/root-cause`  (auth required)

Returns a `RootCauseOut` (404 when none existed):

```json
{
  "incident_id": "abc123...",
  "root_cause": "Database connection pool exhaustion.",
  "confidence": 0.72,
  "alternatives": [],
  "explanation": "...",
  "causal_chain": [],
  "contributing_factors": []
}
```

### `POST /incidents/{id}/resolve`  (auth required)

Records an engineer-supplied resolution. **This is the authoritative learning
trigger**: when `confirmed_root_cause` is supplied, continuous learning runs
against that ground truth.

**Request body** — `ResolutionCreate`:

| Field                    | Type    | Required | Notes                                            |
|--------------------------|---------|----------|--------------------------------------------------|
| `action`                 | string  | yes      | 1–2048 chars (the remediation taken)             |
| `steps`                  | string[]| no       | ≤ 100 steps                                      |
| `verified`               | bool    | no       | default `false`                                  |
| `confirmed_root_cause`   | string  | optional | **Unlocks learning.** PRISM never learns from its own consensus. |
| `ground_truth_source`    | string  | optional | `engineer_confirmed` \| `incident_postmortem` \| `external_system` \| `benchmark_label` |
| `ground_truth_confidence`| float   | no       | 0.0–1.0                                          |
| `confirmed_by`           | string  | no       | ≤ 128 chars (engineer / tool / runbook)          |

Rules:

- `confirmed_root_cause` **requires** `ground_truth_source` and/or
  `confirmed_by` (else 422).
- Agent reliability grading **additionally** requires `ground_truth_source` to
  be one of the four allowed values.
- An incident can only be resolved once (409 on a second resolve).
- With `LEARNING_MODE=frozen` or `LEARNING_REQUIRE_CONFIRMATION` and no
  confirmed cause, learning is skipped; the resolution itself is still saved.

**Response** — `ResolutionOut` (200 OK):

```json
{
  "incident_id": "abc123...",
  "action": "Increased DB connection pool from 20 to 100.",
  "steps": ["Updated pool size", "Deployed"],
  "verified": true,
  "confirmed_root_cause": "Database connection pool exhaustion.",
  "ground_truth_source": "engineer_confirmed",
  "ground_truth_confidence": 0.95,
  "confirmed_by": "oncall-engineer"
}
```

---

## Pattern library

### `GET /patterns/pending` / `GET /patterns/approved`  (auth)

List `PatternOut` rows (pending / approved).

```json
[
  {
    "id": "pat_...",
    "pattern_signature": "ERROR connection pool exhausted",
    "pattern_text": "...",
    "root_cause_hint": "Database connection pool exhaustion.",
    "confidence": 0.7,
    "approved": false,
    "occurrence_count": 1
  }
]
```

### `POST /patterns/{id}/approve`  (auth)

Approve a pattern. Body: `{ "pattern_id": "<id>", "approver": "<name>" }`.
Returns `{ "approved": true, "pattern_id": "<id>" }` (404 when missing, 400 on
`pattern_id` mismatch).

### `POST /patterns/match`  (auth)

Match raw logs against approved patterns. Body: `{ "logs": ["..."] }`.

```json
[
  {
    "pattern_id": "pat_...",
    "root_cause_hint": "Database connection pool exhaustion.",
    "confidence": 0.7,
    "matched_signature": "ERROR connection pool exhausted"
  }
]
```

---

## Memory

### `POST /memory/search`  (auth)

Semantic search over confirmed incident memory.

```json
{
  "query": "auth service latency spike after deploy",
  "top_k": 5,
  "similarity_threshold": 0.0
}
```

`top_k` clamp: `1 ≤ top_k ≤ memory_top_k × 20` (default 100). Returns:

```json
[
  {
    "incident_id": "abc123...",
    "similarity": 0.81,
    "root_cause": "Database connection pool exhaustion.",
    "resolution": "Increased pool size.",
    "services": ["auth-service"],
    "confidence": 0.72
  }
]
```

Errors: 422 when `query` exceeds 2000 chars.

### `GET /memory/stats`  (auth)

```json
{ "size": 12, "backend": "faiss" }
```

---

## Knowledge Graph

### `POST /kg/services`  (auth)

Upsert a service — body `{ "name": "...", "team": "...", "tier": "..." }`
(team/tier optional). Returns `{ "ok": true }`.

### `POST /kg/apis`  (auth)

Upsert an API — body `{ "path": "/v1/login", "method": "POST", "service": "auth-service" }`.
Returns `{ "ok": true }`.

### `POST /kg/services/{name}/depends-on/{dep}`  (auth)

Record a dependency — optional query param `weight` (default 1.0).
Returns `{ "ok": true }`.

### `POST /kg/changes`  (auth)

Record a recent change — body
`{ "service": "...", "change_type": "deploy", "timestamp": "...", "description": "..." }`.
Returns `{ "ok": true }`.

### `POST /kg/services/subgraph`  (auth)

Fetch the dependency subgraph for services — body `{ "services": ["..."] }`.

```json
{
  "services": [ "...", "..." ],
  "dependencies": [ ["auth-service", "user-service"] ],
  "recent_changes": [],
  "historical_root_causes": []
}
```

---

## Agents

### `GET /agents`  (auth)

```json
[
  { "name": "rule_based_analyzer", "reliability": 0.62, "invocations": 17 }
]
```

### `GET /agents/{name}`  (auth)

`AgentReliabilityOut` (404 when the agent has no stats):

```json
{
  "agent_name": "rule_based_analyzer",
  "accuracy": 0.8,
  "precision": 0.85,
  "recall": 0.75,
  "latency_avg": 0.012,
  "confidence_avg": 0.6,
  "false_positives": 1,
  "false_negatives": 2,
  "invocations": 17,
  "reliability_score": 0.77
}
```

---

## Predictions

### `POST /predictions/run`  (auth)

Run (and persist) incident forecasts. Body (all optional):

```json
{
  "service_filter": "auth-service",
  "recent_anomalies": { "auth-service": [0.2, 0.9, 1.4] },
  "topology_dependents": { "auth-service": 5 }
}
```

Response: `List[PredictionOut]`:

```json
[
  {
    "id": "pred_...",
    "service": "auth-service",
    "predicted_failure_type": "latency",
    "probability": 0.64,
    "estimated_time_minutes": 42,
    "impact": "high",
    "rationale": "...",
    "created_at": "2024-01-01T12:00:03Z",
    "updated_at": "2024-01-01T12:00:03Z"
  }
]
```

### `GET /predictions?limit=50`  (auth)

Lists persisted predictions (max 100).