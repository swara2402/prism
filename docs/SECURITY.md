# PRISM — Security

Security model of the PRISM framework: authentication, input hardening,
redaction, the LLM trust boundary, and the authoritative-learning design that
prevents the system from poisoning itself.

---

## Authentication

Sensitive endpoints require an **API key** sent via the `X-API-Key` header
(enforced by `api/deps.py`).

- **Fail closed**: when `API_KEY` is configured (always in `production`), a
  missing or invalid key returns `401` with a `WWW-Authenticate: ApiKey`
  challenge. No key is compared, no endpoint is exposed.
- **Constant-time comparison**: keys are compared with
  `hmac.compare_digest` — no timing side-channel leak.
- **Production refuses to start without a strong key**: if `APP_ENV=production`
  and `API_KEY` is missing, shorter than `API_KEY_MIN_LENGTH`, or a well-known
  placeholder, startup exits with code 1 (see `Settings.validate_production_secrets`
  in `config/settings.py`).
- **Open mode is dev-only**: with no configured key, endpoints run
  unauthenticated so local development/offline evaluation works. In
  production, missing configuration returns `503 "API authentication is not configured"`
  rather than silently opening.

The following stay **unauthenticated** by design (no sensitive data):

`GET /health` (liveness only), `GET /ready`, `GET /api/info`, `GET /docs`,
`GET /openapi.json`, `GET /` (command console).

## CORS

- In `production`, `CORS_ORIGINS` must be set explicitly; otherwise CORS is
  **not enabled at all** (fail closed). The settings validator refuses to
  start otherwise.
- In development, an empty `CORS_ORIGINS` defaults to
  `http://localhost:3000` / `http://localhost:8000` / loopback variants.
- Credentials are only ever allowed for explicit, non-wildcard origins; the
  middleware is never registered with `allow_origins=["*"]` plus credentials.

## HTTP hardening (in `main.py`)

Every response gets, via `SecurityHeadersMiddleware`:

| Header                       | Value                          | Purpose                          |
|------------------------------|--------------------------------|----------------------------------|
| `X-Request-ID`               | inbound value or new UUID      | Traceability; propagated to logs |
| `X-Content-Type-Options`     | `nosniff`                      | MIME-sniffing protection         |
| `X-Frame-Options`            | `DENY`                         | Clickjacking protection          |
| `Referrer-Policy`            | `no-referrer`                  | Leak prevention                  |
| `Cache-Control`              | `no-store`                     | No sensitive caching             |
| `Content-Security-Policy`    | per-path restricted policy     | Script/style/font/img/connect sources |

The CSP is tightened for the embedded console and relaxed only where strictly
needed for the Swagger UI (`/docs`, `/openapi.json`) to load its CDN assets.
`connect-src 'self'` is always enforced — no outbound browser connections.

**Body-size guard**: requests whose `Content-Length` exceeds
`MAX_REQUEST_BODY_BYTES` are rejected with `413` before they reach a router.

**Request IDs**: an inbound `X-Request-ID` (≤ 128 chars) is honored and echoed
back; otherwise a UUID is assigned. It appears in every response header and in
all structured request logs.

## Input validation & concurrency limits

- Pydantic models enforce field limits (title length, `description` cap,
  `affected_services` ≤ 50, logs ≤ 500 × 2000 chars, traces ≤ 200, search
  query ≤ 2000 chars, idempotency key ≤ 128 chars, etc.) → `422` on breach.
- Global concurrency: maximum `MAX_CONCURRENT_INVESTIGATIONS` (default 3)
  pipeline runs via an `asyncio.Semaphore`; a run that cannot acquire the
  semaphore within 5 seconds returns `429`.
- `INVESTIGATION_RATE_LIMIT` (default `10/minute`) throttles per-client
  investigation requests.

## PII & secret redaction

Every investigation path (regular and streaming) runs incident evidence
through `utils/redaction.py` **before persistence and before any LLM call.**

Patterns handled (ordered so specific matches win first):

- **JWTs** (`eyJ…` triple segment)
- **Bearer / Basic** authorization values
- **AWS keys** (`AKIA…` / `ASIA…`, and `aws_secret_access_key=…`)
- **Private keys** (`-----BEGIN … PRIVATE KEY-----`)
- **Credential pairs**: `password=`, `secret=`, `token=`, `api_key=`,
  `passphrase=`, `client_secret=`, `access_token=`, `auth_token=`, …
- **Hyphenated credential tokens** (`secret-token-…`, `api-key-9f42`, …)
- **DSN / connection strings embedding credentials** (`scheme://user:pass@host`)
- **Session IDs / Authorization / Set-Cookie** values
- **Google API keys** (`AIza…`) and **Slack tokens** (`xox…-…`)
- **Emails**, **IPv4 addresses**, **credit-card-like digit runs**
- **Internal URLs** (`localhost`, loopback, RFC 1918 private ranges)

Each match is replaced with an opaque marker (`[REDACTED_SECRET]`,
`[REDACTED_EMAIL]`, `[REDACTED_IP]`, `[REDACTED_KEY]`, `[REDACTED_URL]`).
Redaction is **idempotent**: markers can never match a later pattern, so
running it twice yields the same output as once, and re-processing memory is
safe. The approach is deliberately conservative — over-redaction is preferred
over leaking a credential.

## LLM trust boundary (`utils/llm.py`)

LLM output never becomes executable input:

- Structured outputs must be valid JSON (`validate_structured_json`); a
  resilient extractor (`locate_json_object`) pulls the first valid object from
  noisy completions; malformed output fails the call rather than being
  interpolated.
- Incident evidence is fenced with explicit delimiters
  (`wrap_evidence`, `<<<PRISM_INCIDENT_DATA_START>>>`) so the model can
  separate data from instruction, reducing prompt-injection surface.
- Calls are bounded by `LLM_MAX_TOKENS`, `LLM_REQUEST_TIMEOUT`, and
  `LLM_TEMPERATURE`.
- If the LLM is unavailable, times out, or returns unusable output, the agent
  degrades to a **zero-confidence `finding_type="error"` result** — it may lower
  confidence but never fabricates a root cause.

## Authoritative learning (anti-self-poisoning)

The most important property: **PRISM cannot learn from its own unverified
consensus.** Learning is gated:

1. Only an engineer-confirmed resolution (`POST /incidents/{id}/resolve`)
   can trigger learning — never the investigation pipeline itself.
2. The confirmation must carry an allowed ground-truth source:
   `engineer_confirmed`, `incident_postmortem`, `external_system`,
   `benchmark_label`. Anything else is refused (`422`) or skipped.
3. `LEARNING_REQUIRE_CONFIRMATION=true` (default) enforces that no learning
   happens when confirmation is absent.
4. `LEARNING_MODE=frozen` disables learning entirely for reproducible
   evaluation runs.
5. Every learned artifact records its **provenance**: findings persist
   `source_type`, `codepath`, and `fallback_used`; consensus voters can be
   de-weighted when they share a codepath; agent reliability updates carry the
   ground-truth source that justified them.
6. Learned patterns are surfaced as **pending** and require explicit
   approval (`POST /patterns/{id}/approve`) before they influence future
   investigations.

## Runtime metadata

Incident results embed a `runtime` block (`prism_version`, `llm_model`,
`learning_mode`, `mdv_threshold`, and a **masked** `database_url`,
`postgresql+asyncpg://***@…`), and agent findings record `execution_ms` per
call — giving audits a precise, reproducible account of how a verdict was
reached, without exposing connection details.

## Production posture & known limitations

- Intended to sit **behind a trusted network perimeter / reverse proxy** (TLS
  termination, additional IP allow-listing) — PRISM authenticates callers but
  does not terminate TLS itself.
- Coordination endpoints (`/incidents/investigate/stream`) carry the same
  auth as their JSON counterparts.
- Keep `ENABLE_OLLAMA`/`ENABLE_NEO4J` defaults in mind: when disable flags are
  off, integrations fall back to in-memory implementations — fine for
  evaluation labs, but a deployment must make its storage/LLM policy explicit.
- The `.env` file is git-ignored; never commit real keys. Rotate `API_KEY` in
  production per your organization's policy.