# Scaling PRISM

PRISM runs as a single FastAPI process by default. This document covers what is
already in place for multi-replica operation and what remains for Phase 14/16.

## In-process (current default)

- **Concurrency cap** — `MAX_CONCURRENT_INVESTIGATIONS` is enforced by an
  `asyncio.Semaphore` in `api/investigation.py`; excess requests get `429`.
- **Rate limit** — `INVESTIGATION_RATE_LIMIT` (e.g. `10/minute`) is enforced
  per caller (tenant → API-key hash → client IP) with a sliding window in
  `utils/rate_limit.py`.

Both are **process-local**. With N replicas, the effective limits are N× the
configured value.

## Shared rate limiting (implemented)

Set `REDIS_URL` to enable `RedisRateLimiter` (`utils/rate_limit.py`), a Redis
sorted-set sliding window shared by all replicas:

```env
INVESTIGATION_RATE_LIMIT=10/minute
REDIS_URL=redis://redis:6379/0
```

If `REDIS_URL` is empty, PRISM falls back to the in-process limiter. The
backend is selected once at startup via `build_rate_limiter()`.

## Durable job queue (Phase 14/16 — not yet implemented)

Investigations currently run inline within the request. For horizontal scale
and durability across restarts, move the pipeline behind a queue. Recommended
design:

1. **Table** `investigation_jobs`
   - `id` (uuid), `idempotency_key` (unique, nullable), `tenant_id` (indexed),
     `status` (`queued|running|done|failed`), `attempts` (int),
     `payload` (JSONB), `result_incident_id` (nullable FK),
     `error` (text), `created_at`, `updated_at`, `locked_at`.
2. **API** — `POST /incidents/investigate` inserts a `queued` row and returns
   `202 Accepted` with the job id (keep the synchronous path behind a feature
   flag for backward compatibility).
3. **Worker** — a separate process (`python -m worker`) claims jobs with
   `SELECT ... FOR UPDATE SKIP LOCKED`, runs `_run_investigation`, and writes
   the result. Retries use exponential backoff up to `attempts_max`.
4. **Idempotency** — reuse the existing unique `idempotency_key` so retries and
   duplicate submissions collapse onto one job.
5. **Status API** — `GET /incidents/jobs/{id}` for polling; optionally push
   progress over the existing SSE stream.
6. **Ops** — run workers as a separate deployment; scale workers independently
   of the API. Redis or Postgres can back the queue; Postgres avoids a new
   dependency and gives transactional enqueue with the incident row.

Until this lands, keep `MAX_CONCURRENT_INVESTIGATIONS` low on each replica and
rely on the shared Redis rate limit to bound total load.
