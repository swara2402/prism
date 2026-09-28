# PRISM 2.0 implementation map

This branch turns the audit into an implementation sequence without replacing
working PRISM subsystems.

## Phase 0 — Measurement

Implemented:

- explicit epistemic levels: observed/evidence/inference/confirmed
- investigation trace primitive
- runtime metadata and benchmark scenarios
- CI test/dependency checks

Acceptance: every future RCA benchmark must preserve ground truth outside the
investigation payload and record the evidence path used to reach the RCA.

## Phase 1 — Reliability

Implemented:

- hard per-agent execution deadline
- explicit `timeout`, `error`, and `degraded` finding states
- production schema startup no longer performs auto-DDL
- durable Alembic migration for service accounts
- memory persistence is authoritative before FAISS indexing

Remaining integration gate:

- wire `InvestigationTrace` into the orchestration loop and persist its summary
- add worker lease-expiry/requeue integration test against PostgreSQL

## Phase 2 — Reasoning quality

Implemented:

- consensus requires configured minimum independent voters
- duplicate findings from the same voter do not create quorum
- support score is explicitly not presented as calibrated probability
- historical similarity no longer accumulates into a false 1.0 confidence
- agent findings default to inference provenance
- confirmation is an explicit external/human event

Remaining integration gate:

- replace hypothesis normalization with explicit support/contradiction/missing-evidence bookkeeping throughout `InvestigationState`
- calibrate any future probability model against the benchmark

## Phase 3 — UX

The login/onboarding flow already uses secure session cookies and removes the
legacy browser API key. The investigation console should expose four states:

1. Observed
2. Evidence
3. Inference
4. Confirmed

Remaining integration gate:

- remove all stale API-key UI code from the main console
- render agent status, contradictions, missing evidence and degraded services
  directly from the investigation response

## Phase 4 — Security and tenancy

Implemented:

- authenticated tenant context propagated with a ContextVar
- tenant-bound machine service accounts
- hashed, expiring service-account credentials
- server-side tenant injection overwrites caller-supplied tenant headers
- tenant-scoped historical memory retrieval
- tenant-scoped FAISS snapshots

Remaining integration gate:

- add explicit tenant columns and repository filters to every persisted global
  entity that does not already derive ownership through `incident_id`
- remove the legacy global `API_KEY` setting after service-account migration

## Phase 5 — Scalability

The existing job queue, PostgreSQL locking, and optional Redis limiter remain
in place. Scale-out should happen only after Phase 1 and Phase 4 integration
acceptance tests pass.

Target deployment:

```text
Browser
  -> load balancer
  -> stateless PRISM API replicas
  -> Redis rate-limit / coordination
  -> PostgreSQL durable state
  -> worker pool
  -> Neo4j
  -> tenant-scoped vector indexes
```

## Phase 6 — RCA quality evaluation

Implemented benchmark fixtures and an external runner in `evals/`.

Required release metrics:

- RCA top-1 accuracy
- top-3 hypothesis recall
- unsupported-claim rate
- evidence attribution coverage
- contradiction coverage
- abstention accuracy
- latency
- timeout/error/degraded dependency rate
- calibration error if a probability model is introduced

## Phase 7 — Production / SaaS

The architecture is ready to evolve toward SaaS, but billing and integrations
must remain downstream of the correctness gates.

Required before SaaS launch:

- complete tenant coverage
- service-account rotation/revocation UI
- audit log retention policy
- usage metering by tenant
- quotas and concurrency limits per tenant
- SSO/OIDC
- secret management outside environment files
- backup/restore drills
- migration rollback drills
- incident benchmark regression gate in CI
- observability dashboards and alerting

## Release rule

A feature is not considered production-ready merely because its code path or
configuration exists. It must have a runtime test, a failure-mode test, and an
end-to-end acceptance criterion.
