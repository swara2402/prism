# PRISM — Technical Architecture

Deep developer documentation for **PRISM**, the Enterprise Agentic AI Incident
Investigation Framework.

This document assumes the application is installed and running. For
installation and setup, see [`README.md`](../README.md).

---

## Table of Contents

1. [Overview](#overview)
2. [System Context & Topology](#system-context--topology)
3. [Technology Stack](#technology-stack)
4. [Repository Layout](#repository-layout)
5. [The Investigation Pipeline](#the-investigation-pipeline)
6. [Ground-Truth-Gated Learning](#ground-truth-gated-learning)
7. [Core Modules](#core-modules)
8. [Data Model](#data-model)
9. [Concurrency, Rate Limits & Backpressure](#concurrency-rate-limits--backpressure)
10. [Resilience & Degradation](#resilience--degradation)
11. [Security Model](#security-model)
12. [Testing Strategy](#testing-strategy)
13. [Extending PRISM](#extending-prism)
14. [Configuration Reference](#configuration-reference)
15. [Performance Notes](#performance-notes)

---

## Overview

PRISM is a modular, production-ready Python framework that investigates
production incidents using **multiple cooperating intelligent agents**. When an
incident is reported it:

1. **Redacts** sensitive data (credentials, emails, IPs, tokens) before
   anything is stored or shown to a model.
2. Persists the incident and runs an **MDV/ADG step loop** that selects and
   executes the single highest-value agent action at each step.
3. Merges accumulated findings into a **causal graph** and propagates
   confidence (belief-propagation style).
4. Reaches a weighted **consensus** on the root cause and falls back to graph
   traversal only when explicitly enabled.
5. Generates a human-readable **explanation**, agent-usefulness analysis
   (**meta-reasoning**), and reproducibility metadata.
6. **Defer** continuous learning: nothing is learned during investigation.
   Learning only runs later, from `POST /incidents/{id}/resolve`, and only when
   the resolution carries a **confirmed (ground-truth) root cause**. PRISM's own
   consensus is treated as a hypothesis, never as truth.

The design goal is that no single agent is authoritative: findings are treated
as **voters**, and each voter's influence is weighted by its own confidence and
its continuous, learned **reliability score**.

---

## System Context & Topology

```
                        ┌───────────────────────────────┐
                        │   Incident Command Console     │
                        │   (static/ HTML + JS, served   │
                        │    at / by the API)            │
                        └──────────────┬────────────────┘
                                       │ HTTP/JSON (same origin or CORS)
                                       ▼
                        ┌───────────────────────────────┐
                        │       FastAPI application      │
                        │          (main:app)            │
                        │                                │
                        │  RequestID / Security / CORS   │
                        │  middleware                    │
                        │  ┌───────────────────────────┐ │
                        │  │  API routers              │ │
                        │  │  incidents • patterns     │ │
                        │  │  memory • kg • agents     │ │
                        │  │  predictions              │ │
                        │  └────────────┬──────────────┘ │
                        │               │                │
                        │  ┌────────────▼──────────────┐ │
                        │  │       Engines              │ │
                        │  │  investigation.tree        │ │
                        │  │  orchestrator ─▶ agents    │ │
                        │  │  causal_graph ─▶ confidence│ │
                        │  │  consensus ─▶ meta_reason  │ │
                        │  │  explainability            │ │
                        │  │  learning ─▶ prediction    │ │
                        │  └────────────┬──────────────┘ │
                        └───────────────┼────────────────┘
                                        │
        ┌───────────────┬───────────────┼────────────────┬──────────────┐
        ▼               ▼               ▼                ▼              ▼
   ┌──────────┐   ┌──────────┐   ┌──────────┐      ┌──────────┐   ┌──────────┐
   │PostgreSQL│   │  Neo4j   │   │  Ollama  │      │  FAISS   │   │ Sentences│
   │  :5432   │   │ :7687    │   │ :11434   │      │ in-mem / │   │  vectors │
   │ incidents│   │ services │   │ LLM +    │      │ persisted│   └──────────┘
   │ findings │   │ APIs/Users│  │ embedding│      └──────────┘
   │ patterns │   │ changes  │   └──────────┘
   │ memory   │   └──────────┘
   │ fits etc.│
   └──────────┘
```

All external subsystems (Neo4j, Ollama, FAISS) are **optional** and fail open to
degraded fallbacks so the system can run in CI, offline, or during partial
outages. PostgreSQL (or SQLite in tests) is the only hard dependency.

---

## Technology Stack

| Concern            | Technology                                    | Role                                            |
|--------------------|-----------------------------------------------|-------------------------------------------------|
| Web framework      | FastAPI + Uvicorn                             | HTTP API, SSE streaming, middleware             |
| Validation         | Pydantic v2 / pydantic-settings               | API contract + typed configuration              |
| ORM / DB           | SQLAlchemy 2.0 (async), asyncpg, psycopg2     | Persistence (PostgreSQL; SQLite in tests)       |
| Migrations         | Alembic                                       | Schema migrations                               |
| Graph algorithms   | NetworkX                                      | Causal graph, traversal, root-cause search      |
| Enterprise KG      | Neo4j (Cypher, MERGE semantics)               | Service/API/incident knowledge graph            |
| Embeddings         | sentence-transformers                         | Document → vector for memory search             |
| Vector index       | FAISS-CPU                                     | Fast approximate semantic retrieval             |
| LLM / agents       | Ollama + langchain/langgraph                  | Local LLM analysis agent                        |
| Semantic text ops  | numpy, custom jaccard/tokenize helpers        | Similarity clustering in consensus / memory     |
| Logging            | structlog                                     | Structured JSON-ish logs, request tracing       |
| Async / concurrency| asyncio, anyio, tenacity                      | Parallel agents, retries, backpressure          |
| Serialization      | orjson                                        | Fast JSON for evidence payloads                 |
| Testing            | pytest + pytest-asyncio + httpx               | Unit + API smoke tests (in-memory SQLite)       |

### External services summary

| Service   | Default port(s) | Used for                                      | Fallback when unavailable        |
|-----------|-----------------|-----------------------------------------------|----------------------------------|
| PostgreSQL| 5432            | Incidents, findings, patterns, memory, etc.   | SQLite (tests only)              |
| Neo4j     | 7474 / 7687     | Enterprise knowledge graph (Cypher)           | In-memory NetworkX subgraph      |
| Ollama    | 11434           | LLM reasoning + embedding model               | LLM agent falls back / degraded  |
| FAISS     | —               | Semantic incident retrieval                   | Pure-Python cosine similarity    |

---

## Repository Layout

```
prism/
├── main.py                  # FastAPI app, middleware, lifespan, router wiring
├── config/                  # Settings (pydantic-settings) + structlog config
├── agents/                  # 8 investigation agents + registry + base class
├── orchestrator/            # Adaptive agent selection + concurrent execution engine
├── investigation/           # MDV/ADG step loop, state, hypothesis manager, metrics
├── causal_graph/            # NetworkX causal graph + root-cause traversal
├── confidence/              # Belief-propagation confidence propagation
├── consensus/               # Multi-voter weighted consensus
├── memory/                  # FAISS-backed semantic incident memory + embeddings
├── knowledge_graph/         # Neo4j enterprise KG (services / APIs / users / incidents)
├── learning/                # Pattern generator + continuous learning loop
├── prediction/              # Predictive incident engine
├── explainability/          # Evidence + graph-reasoning explanation builder
├── meta_reasoning/          # Agent usefulness evaluation + improvement suggestions
├── database/                # SQLAlchemy async models, session, repositories
├── models/                  # Pydantic schemas (public API contract)
├── api/                     # FastAPI routers + API-key dependency
├── utils/                   # Redaction, LLM client, evidence, metrics, text helpers
├── static/                  # Incident command console (HTML/JS)
├── tests/                   # pytest unit + API smoke tests
├── docs/                    # This documentation set
├── data/                    # FAISS index + reliability snapshots (git-ignored)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## The Investigation Pipeline

The endpoint `POST /incidents/investigate` (and its SSE variant
`POST /incidents/investigate/stream`) drives the full pipeline.

```
IncidentCreate (logs, metrics, traces, topology, affected_services)
   │
   ▼
[1] REDACTION  utils.redaction scrubs incident evidence in place:
               JWTs, bearer/AWS/Slack/Google keys, DSN credentials,
               API keys, emails, IPs, credit cards, internal URLs.
               (BEFORE any persistence or LLM exposure.)
   │
   ▼
[2] Persist incident ─────────────► incidents (PostgreSQL)
   │        └─ dedup-aware create_incident
   │        └─ Idempotency-Key header stored on the incident row;
   │           a repeat POST under the same key REPLAYS the persisted
   │           result instead of re-running the pipeline.
   │
   ▼
[3] investigation.tree.run_tree()  MDV/ADG step loop
   │   while best_action.mdv ≥ threshold and steps < max:
   │     select_agents(state)       adaptive selection (evidence + reliability + MDV)
   │     execute_action(agent)      ONE best action at a time
   │     state.update_from_finding  hypothesis distribution update
   │     compute_adg(prev, next)    real diagnostic gain → EMA effectiveness update
   │   agents: rule_based • log • metric • trace
   │           topology • historical • kg • llm
   │
   ▼
[4] CausalGraphBuilder.build_from_findings()  NetworkX directed graph
   │           nodes: services, logs, metrics, traces, findings, hypotheses
   ▼
[5] confidence.propagate()          belief-propagation, noisy-OR merge, 5 iterations
   │
   ▼
[6] causal_graph.find_root_causes() top-k source / most-influential nodes
   │           score = 0.4·confidence + 0.3·causal_influence + 0.3·earliness
   ▼
[7] consensus.reach_consensus()     weighted multi-voter vote + quorum + graph bonus
   │            (rule • llm • memory • kg • reliability, hint clustering)
   │            correlated voters sharing a fallback codepath are DISCOUNTED
   ▼
[8] Persist root cause + causal chain ──► root_causes (PostgreSQL)
   │
   ▼
[9] explainability.build_explanation()  evidence + confidence breakdown + graph chain
   │
   ▼
[10] meta_reasoning.evaluate()       agent usefulness, optimal path, suggestions
   │
   ▼
[11] Result: InvestigationResult with
     ├─ root_cause / explanation / meta_reasoning
     ├─ agent_statuses (ok|failed|skipped + exec_ms)  ← failure semantics
     ├─ status = completed | completed_with_degraded_agents
     └─ runtime (prism version, model, learning mode, thresholds,
                 masked DB URL)                          ← reproducibility

[12] ❌ NO continuous learning here. PRISM's consensus is an unverified
     hypothesis. Learning is deferred to resolution (see next section).
```

The **investigation tree** (`investigation/tree.py`) is rebuilt at runtime for
each incident. It always begins with a `gather_evidence` root, branches into one
`analyze` step per available evidence type (logs, metrics, traces, topology),
conditionally adds `history` and `kg` steps when affected services are known,
adds a conditional `llm_deep_dive`, and ends with `decide_root_cause`.

The tree is *not* executed depth-first in the classic sense. `run_tree` runs an
**MDV-driven loop** that repeatedly picks the single highest-value action and
executes it, feeding results back into a shared `InvestigationState`.

---

## Ground-Truth-Gated Learning

Learning is the **only** place PRISM mutates long-lived knowledge, so it is the
most guarded path in the system.

### Authoritative trigger: resolution

```text
POST /incidents/{id}/resolve
   │
   ├─ resolution persisted (always)
   │
   └─ confirmed_root_cause present?  ──NO──▶ nothing learned (reason: unconfirmed)
        │
        └─ YES
           │
           ├─ LEARNING_MODE=frozen ?  ──YES──▶ nothing learned (reason: learning_mode_frozen)
           │
           └─ ONLINE ──▶ learn_from_incident(LearningInput)
                          ├─ _update_pattern_library    (extract + persist patterns)
                          ├─ _update_knowledge_graph    (link incident → services / RCA / resolution)
                          ├─ _update_agent_reliability  (requires ground_truth_source)
                          ├─ _update_memory             (embed + index compressed text)
                          └─ _persist_lessons
```

### Why PRISM never learns during investigation

The investigation endpoint produces a **consensus root cause** — a hypothesis,
not established truth. Learning from an unconfirmed RCA would bake wrong answers
into patterns, memory, knowledge-graph causal edges, and agent reliability —
and those wrong answers would then bias every future investigation. Hence
`learning_require_confirmation=True` by default.

### Agent reliability requires independent ground truth

Agent reliability is only updated when the resolution carries an explicit
`ground_truth_source`, one of:

- `engineer_confirmed`
- `incident_postmortem`
- `external_system`
- `benchmark_label`

Without it, agents would be graded against PRISM's own consensus — circular
reasoning — so the update is skipped. Each finding also persists a `provenance`
block (source type, codepath, fallback flag, execution time, model) recorded at
investigation time, so resolutions can grade agents against confirmed truth.

### Reproducible experiment mode

`LEARNING_MODE=frozen` short-circuits the entire learning pipeline
(`learned=False, reason="learning_mode_frozen"`). Every evaluation run therefore
starts from identical persistent state and scenarios stay statistically
independent — critical for honest benchmarking.

---

## Core Modules

### 1. Configuration (`config/`)

- `config/settings.py` — a lazy-cached `Settings` singleton via
  `pydantic-settings`, loaded from environment variables and a local `.env`
  file. Import `from config.settings import settings` anywhere for the shared
  instance.
  - Derived properties: `is_production`, `is_test`, `data_dir`,
    `cors_origins_list`.
  - `validate_production_secrets()` refuses to boot in production when the
    `API_KEY` is missing/short/weak, database or Neo4j passwords are weak
    defaults, or `CORS_ORIGINS` is unset. Invoked at import time.
  - `learning_mode` validator restricts to `online | frozen`.
- `config/logging.py` — `structlog` configuration producing key/value
  structured logs via `get_logger(__name__)`.

### 2. Application Entry Point (`main.py`)

- Builds the `FastAPI` app and registers all routers: `investigation`,
  `patterns`, `memory`, `knowledge_graph`, `agents`, `predictions`.
- `SecurityHeadersMiddleware`:
  - assigns an `X-Request-ID` (or propagates an inbound one),
  - enforces a request-body size limit (413 on overflow),
  - attaches security headers (`nosniff`, `DENY` framing, `no-referrer`,
    `Cache-Control: no-store`, CSP),
  - logs every request/response with duration.
- CORS middleware fails closed in production (`CORS_ORIGINS` required); dev
  defaults allow localhost origins.
- Lifecycle (`lifespan`): initializes the DB schema (idempotent), pre-warms
  incident memory (`MemoryStore.load()`), and initializes the Neo4j driver.
  Failures here are fatal rather than silently degraded.
- Serves the incident command console at `/`, Swagger UI at `/docs`, and:
  - `GET /health` — **minimal** liveness (`{"status":"ok"}`). Never leaks
    subsystem detail.
  - `GET /internal/health` — **authenticated** deep subsystem report (DB /
    memory / FAISS / Neo4j / Ollama); requires `X-API-Key`.
  - `GET /ready` — readiness probe (`SELECT 1` + memory loaded); 503 when not
    ready.

### 3. Agents (`agents/`)

All agents subclass `BaseAgent` (`agents/base.py`):

```python
class BaseAgent(abc.ABC):
    name: str
    supported_incident_types: List[str]   # or ["*"]
    requires: List[str]                   # evidence keys needed to run
    priority: int                         # lower runs earlier
    requires_external: bool               # LLM/DB/Neo4j dependency flag

    async def investigate(self, context) -> FindingPayload: ...
```

- `run()` wraps `investigate()` with latency timing and error capture; a failed
  agent yields an `error`-type `FindingPayload` with `confidence=0.0` rather
  than crashing the pipeline.
- `adjust_confidence(raw, reliability)` scales raw confidence by
  `0.5 + 0.5 * reliability` (reliability clamped to `[agent_min_reliability, 1.0]`).

`FindingPayload` is the universal agent output: `agent_name`, `finding_type`,
`description`, `confidence`, `evidence` (dict), `root_cause_hint`, `hypotheses`,
`latency_s`, `metadata`.

**Built-in agents** (registered in `agents/registry.py`):

| Agent                       | Evidence in     | Finds / produces                                  | external |
|-----------------------------|-----------------|---------------------------------------------------|----------|
| `rule_based_analyzer`       | logs            | Deterministic rules for DB / network / app / config failures | no |
| `log_analyzer`              | logs            | Anomaly + rare-event detection (evidence compression)       | yes* |
| `metric_analyzer`           | metrics         | Metric anomalies / threshold breaches                        | no |
| `trace_analyzer`            | traces          | Slow / failed / timeout spans                                | no |
| `topology_analyzer`         | topology        | Blast radius, redistribution, single points of failure      | no |
| `historical_analyzer`       | memory          | Semantic retrieval of similar past incidents                 | yes |
| `knowledge_graph_analyzer`  | kg              | Dependency subgraph, recent changes, historic root causes   | yes |
| `llm_analyzer`              | logs, ...       | Ollama LLM synthesis / deep-dive (schema-validated output)  | yes |

\* `log_analyzer` is marked external because its summarizer can route through
the embedding / LLM stack; it degrades to local heuristics when unavailable.

LLM-backed agents route their raw model output through `utils/llm.py`
strict structured-output validation (see [Security Model](#security-model) and
[Utilities](#17-utilities-utils)).

### 4. Orchestrator (`orchestrator/`)

Two cooperating pieces:

**`orchestrator/agent_registry.py`** — a static `AGENT_REGISTRY` of
`AgentCapability` records declaring `supported_evidence`,
`supported_hypotheses`, `discriminates_between`, `execution_cost`,
`expected_latency`, `repeatable`, and `default_reliability`.

**`orchestrator/selector.py`** — `select_agents()` performs adaptive selection:

1. Loads all registered agents and each agent's current reliability.
2. Applies gates: repeat protection for non-repeatable actions, explicit skip
   list, evidence-requirement gate, incident-type support gate, and
   minimum-reliability gate.
3. Computes a **discrimination score** from hypothesis competition intensity
   and whether the action discriminates between the top hypotheses.
4. Builds an action list scored by **MDV** (marginal diagnostic value), sorted
   descending, and stores it on the `InvestigationState`.

**`orchestrator/engine.py`**:

- `orchestrate()` runs all selected agents **concurrently** via
  `asyncio.gather`, reliability-adjusts each confidence, persists findings to
  PostgreSQL, and bumps agent invocation counters.
- `execute_action()` runs a *single* agent and optionally persists — used by the
  MDV step loop.
- `_persist_finding()` stores per-finding `root_cause_hint`, `hypotheses`, and a
  structured `provenance` block (source type, codepath, fallback flag,
  execution time, model) enabling later ground-truth grading.

### 5. Investigation Engine (`investigation/`)

The MDV/ADG loop coordinates "what action next".

- `investigation/state.py` — `InvestigationState`: shared diagnostic state
  (hypothesis probability distribution, entropy / `hypothesis_uncertainty`,
  `agent_disagreement`, `evidence_coverage`, seeding, executed actions).
  `update_from_finding()` folds each agent result into the hypothesis
  distribution and renormalizes.
- `investigation/metrics.py` — derives state metrics (entropy, disagreement,
  coverage, causal consistency).
- `investigation/value_engine.py` — **MDV** = weighted combination of
  `discrimination_score`, `expected_uncertainty_reduction` (EMA-learned),
  `evidence_value`, `reliability_score`, and a cost component. Supports
  `cost_efficiency` and `cost_penalty` modes.
- `investigation/action_effectiveness.py` — EMA store of expected uncertainty
  reduction per (incident-context, agent); decays on repeated executions of
  repeatable actions.
- `investigation/diagnostic_gain.py` — **ADG**: measures actual uncertainty
  reduction between pre/post state snapshots, with active-weight
  renormalization (missing metrics excluded).
- `investigation/hypothesis_manager.py` — competition intensity and
  top-competing-hypothesis helpers used by the selector.
- `investigation/reliability_store.py` — per-context reliability EMA cache
  persisted to `data/agent_reliability.json`. `update_reliability()` refuses to
  update unless valid ground truth is present (`has_ground_truth=True`).
- `investigation/experiment_logger.py` — optional iteration logging
  (`PRISM_EXPERIMENT_LOGGING`).
- `investigation/tree.py` — `StepSpec`/`InvestigationTree` models (convertible
  to NetworkX, topological ordering) and the `run_tree()` executor loop.

`run_tree` loop semantics:

```
while True:
    selection = select_agents(...)                # ranked remaining actions
    best_action = selection.remaining_actions[0]
    if best_action.mdv < MDV_THRESHOLD: break     # 0.15 default
    if iteration >= MAX_INVESTIGATION_STEPS: break # 20 default
    pre = deepcopy(state)
    execute ONE action (execute_action)
    state.update_from_finding(finding)            # hypothesis update
    adg = compute_adg(pre, state)                 # real gain
    update_effectiveness_from_adg(inc_context, agent, adg)  # EMA learning
    log_iteration(...)
reach_consensus(across all accumulated findings)
```

Stopping reasons surface on the result (`max_steps_or_no_candidates`,
`no_remaining_candidates`, `mdv_below_threshold`, `max_investigation_steps_reached`).

### 6. Causal Graph (`causal_graph/`)

`CausalGraph` wraps a NetworkX `DiGraph`. Node kinds: `log`, `metric`,
`trace`, `finding`, `service`, `hypothesis`, `external`. Edges are typed
(`causes` / `precedes` / `explains` / `correlates`) and weighted.

- `CausalGraphBuilder.build_from_findings()` ingests findings + raw evidence:
  - each affected service becomes a node,
  - anomaly logs / metric anomalies / error traces become nodes linked to the
    services they implicate,
  - each finding becomes a node linked to implicated services, cited log
    evidence, and virtual `root_cause_hint` hypothesis nodes.
- IDs are stable SHA-256 hashes so repeated building deduplicates via
  `add_or_merge_node` (confidences max-merge).
- `find_root_causes(top_k)`:
  1. source nodes (in-degree 0) are the strongest candidates;
  2. if none (cyclic), the most *causally influential* nodes (descendant count
     at least 50% of max);
  3. rank by `0.4·confidence + 0.3·causal_influence + 0.3·earliness`.
- `causal_chain_to(leaf)` returns the longest path from any source to a leaf —
  used for the human-readable root → symptom chain.

### 7. Confidence Propagation (`confidence/`)

`propagate(graph, iterations=5)` implements a simplified belief propagation:

- Each node starts from its own `confidence` prior.
- Incoming evidence is gathered from predecessors (weighted, no damping) and
  successors (damped `×0.5`), then combined with a **noisy-OR**:
  `P = 1 − ∏(1 − pᵢ·wᵢ)`.
- Posterior = `prior_weight·prior + propagation_weight·propagated`, clamped to
  `[0,1]`.
- Stops early on convergence (`max_delta < 1e-3`).

`PropagationResult` exposes `posteriors` per node and per-node `contributions`,
which `explain_confidence` turns into a ranked contributor list for
auditability.

### 8. Consensus (`consensus/`)

`reach_consensus()` converts findings into `VoterOpinion`s and treats each agent
as a voter:

1. **Hint clustering** — textually similar `root_cause_hint`s are grouped with
   token Jaccard similarity (threshold 0.4, representative tokens union).
2. **Cluster scoring** — each cluster's weight =
   `Σ voters conf·(0.4 + 0.6·reliability)` + quorum bonus + graph-confidence
   bonus.
   - **Quorum bonus uses independent codepaths**: voters that share the same
     fallback codepath count as *one* piece of evidence
     (`bonus = min(0.2, 0.05·(n_independent − 1))`), fixing the structural
     weakness where two agents running the same fallback looked like two
     independent votes.
   - Graph bonus `0.15·p₍ₕᵢₙₜ₎` from causal-graph candidates.
3. **Decision** — highest cluster wins when above `quorum_threshold`; the winner
   yields `root_cause`, `confidence`, ranked `alternatives`, and a full
   `voter_breakdown` (hint / confidence / reliability / voted-for-winner /
   source_type / codepath / fallback_used).
4. When no quorum is reached, reports a low-confidence `"undetermined"` verdict
   with an explanatory message.

### 9. Semantic Memory (`memory/`)

`MemoryStore` is a singleton holding an in-memory vector index synced with the
PostgreSQL `incident_memory` table.

- **Backends** (in priority order):
  1. FAISS `IndexFlatIP` rebuilt from in-memory vectors and snapshotted to
     `data/faiss_index.faiss` (+ `.meta.json`);
  2. pure-Python cosine similarity fallback when FAISS is disabled/unavailable.
- Records store `text_repr`, `embedding` (JSONB list), `root_cause`,
  `resolution`, `confidence`, `lessons`, `services`.
- Embeddings are produced by `memory/embeddings.py` via sentence-transformers
  (`all-MiniLM-L6-v2`, dim 384) or an Ollama embed model; `_normalize_dim`
  pads/truncates to the configured dimension, and there is a deterministic
  hash-based pseudo-embedding for offline/CI.
- `search()` returns top-`k` `MemoryHit`s above a similarity threshold; `add()`
  embeds, persists, updates the in-memory index and resnapshots FAISS.
- The store is pre-warmed at startup (`load()`), so the first query does not pay
  the embedding/index warm-up cost.

### 10. Knowledge Graph (`knowledge_graph/`)

`KnowledgeGraphStore` wraps an **async Neo4j driver** with an in-memory NetworkX
`_InMemoryKG` fallback. Labels: `Service`, `API`, `User`, `Incident`,
`RootCause`, `Resolution`, `Pattern`, `Change`. Relationships: `DEPENDS_ON`,
`OWNS`, `EXPOSES`, `AFFECTED_BY`, `CAUSED_BY`, `RESOLVED_BY`,
`MATCHES_PATTERN`, `RECENT_CHANGE`.

- All writes are idempotent (Cypher `MERGE`).
- `query_service_subgraph(services)` returns the affected-service dependency
  subgraph, recent changes, and historical root causes via one summarized Cypher
  query, shaped and de-duplicated by `_shape_neo4j_records`; the in-memory
  fallback produces the same shape.
- `update_after_investigation()` links an incident to its affected services,
  root cause, and resolution (learning path only).
- When Neo4j is unreachable, `_run_neo4j()` logs and no-ops while the in-memory
  graph keeps working.

### 11. Continuous Learning (`learning/`)

Ground-truth-gated (see [Ground-Truth-Gated Learning](#ground-truth-gated-learning)):

- `pattern_generator.py` — extracts normalized log-signature patterns
  associated with a root cause (`extract_patterns`) and persists them with a
  pending/approved lifecycle (`persist_patterns`). Approved patterns can
  short-circuit future investigations (`match_approved_patterns`, served by
  `api/patterns`).
- `continuous_learning.py` — `learn_from_incident(LearningInput)`:
  1. returns `learned=False` when `LEARNING_MODE` is not `online`
     (`reason="learning_mode_frozen"`), or when no confirmed root cause is
     present and confirmation is required (`reason="unconfirmed_incident"`);
  2. `_update_pattern_library` — extract + persist patterns;
  3. `_update_knowledge_graph` — link incident → services / root cause /
     resolution;
  4. `_update_agent_reliability` — **only when `ground_truth_source` is in
     `ALLOWED_GROUND_TRUTH_SOURCES`** (`engineer_confirmed`,
     `incident_postmortem`, `external_system`, `benchmark_label`); grades each
     agent's persisted `root_cause_hint` against the confirmed cause via Jaccard
     similarity and updates a confusion matrix (TP/FP/FN/TN), recomputing
     accuracy / precision / recall and `reliability_score = 0.5·F1 + 0.5·accuracy`;
  5. `_update_memory` — embed and index a compressed text representation;
  6. `_persist_lessons`.
- `LearningInput` carries `ground_truth_root_cause`, `ground_truth_source`,
  `ground_truth_confidence`, `confirmed_by`, `confirmed_at`.

### 12. Predictive Engine (`prediction/`)

`predict()` forecasts future incidents per `(service, failure_type)` using:

- recurrence interval of historical incidents (base probability ≈ how close we
  are to the average interval),
- linear-regression **trend slope** of intervals (negative → more frequent →
  boost ≤ 0.3),
- recent anomaly severity boost (≤ 0.3),
- topology exposure (dependent count) scales impact (`low`…`critical`),
- ETA is inversely proportional to probability (clamped to ≥ 5 min).

Predictions are persisted (clearing stale rows first) and exposed via
`POST /predictions/run` and `GET /predictions`.

### 13. Explainability (`explainability/`)

`build_explanation()` assembles a structured `Explanation`:

- `evidence_used` — each finding with summarized key evidence,
- `confidence_breakdown` — per-voter confidence + reliability plus graph
  contributor contributions,
- `graph_reasoning` — the causal chain (root → symptom) and propagation
  convergence,
- `alternative_root_causes` — ranked consensus runner-ups,
- `final_explanation` — a complete narrative combining all of the above.
- `_summarize_evidence` trims potentially huge evidence dicts to representative
  samples for response size and readability.

### 14. Meta-Reasoning (`meta_reasoning/`)

During each investigation, `evaluate()` scores every executed agent by how well
its hint relates to the final root cause weighted against confidence and latency
(`0.5·similarity + 0.3·confidence + 0.2·latency_penalty`). It produces
`useful_agents`, `unnecessary_agents`, the `optimal_path`, and actionable
`suggestions` (skip noisy agents, review skipped ones, reorder by usefulness,
add evidence if final confidence is low). Results persist to
`meta_reasoning_records`.

### 15. Database Layer (`database/`)

- `database/session.py` — async SQLAlchemy engine + session factory; the `Base`
  declarative class; `init_db`/`close_db`. The engine picks SQLite in tests.
- `database/models.py` — ORM models (see [Data Model](#data-model)); uses a
  dialect-aware `JSONBOrJSON` type decorator: `JSONB` on PostgreSQL, `JSON`
  elsewhere (SQLite).
- `database/repositories.py` — async repository functions: `create_incident`
  (similarity dedup), `get_incident`, `get_incident_by_idempotency_key`,
  `list_incidents`, `add_finding`, `save_root_cause`, `save_resolution`,
  `add_lesson`, pattern lifecycle, `add_memory`/`list_memory`,
  `upsert_agent_reliability`, prediction save, meta-reasoning save. Business
  logic is intentionally kept out of the routers.

### 16. API Layer (`api/`) & Schemas (`models/`)

- `api/deps.py` — `require_api_key` dependency with constant-time comparison.
  When `API_KEY` is configured (or in production), sensitive endpoints reject
  requests without a correct `X-API-Key`. Unauthenticated endpoints: `/health`,
  `/ready`, `/docs`, `/`, `/api/info`.
- Routers:
  - `api/investigation.py` — `POST /incidents/investigate`,
    `POST /incidents/investigate/stream` (SSE), `GET /incidents`,
    `GET /incidents/{id}`, `GET /incidents/{id}/root-cause`,
    `POST /incidents/{id}/resolve`. Investigation endpoints run under a global
    `asyncio.Semaphore` and are the only heavy path; `Idempotency-Key` header is
    honored; the SSE generator emits `pipeline_*`, `agent_completed`,
    `consensus_reached`, and `learning_deferred` events.
  - `api/patterns.py` — pending/approved lists, approve, match.
  - `api/memory.py` — semantic search + stats.
  - `api/knowledge_graph.py` — service/API upserts, dependency links, recent
    changes, service subgraph.
  - `api/agents.py` — list agents + reliability stats.
  - `api/predictions.py` — run + list predictions.
- `models/schemas.py` — the pydantic API contract: `IncidentCreate`
  (validators enforce `max_affected_services`, `max_log_lines`,
  `max_trace_items`), `IncidentOut`, `RootCauseOut`, `ResolutionCreate` /
  `ResolutionOut` with confirmation fields (`confirmed_root_cause`,
  `ground_truth_source`, `ground_truth_confidence`, `confirmed_by`), `PatternOut`,
  `MemoryHit`, `AgentReliabilityOut`, `PredictionOut`, `ExplanationOut`,
  `MetaReasoningOut`, `AgentStatus`, `InvestigationResult` (with `agent_statuses`,
  `status`, and `runtime`).

### 17. Utilities (`utils/`)

- `evidence.py` — evidence compression before feeding agents (keeps payloads
  bounded).
- `llm.py` — **LLM trust boundary**: `generate_structured()` wraps incident
  evidence in delimiters so instruction-like log content is treated as data, not
  directives (`wrap_evidence` neutralizes injection phrases like "IGNORE
  PREVIOUS INSTRUCTIONS"); `validate_structured_json()` + `locate_json_object()`
  schema-validate and clamp model output; malformed/missing JSON is rejected and
  callers fall back safely. `is_ollama_available` is the availability probe.
  `LLMClient.generate()` falls back to a deterministic rule-based summarizer
  when Ollama is unreachable.
- `redaction.py` — idempotent PII/secret scrubber (JWTs, bearer/AWS/Slack/Google
  keys, DSN credentials, API keys, emails, IPs, credit cards, internal URLs).
  Applied in both investigate paths **before** persistence or LLM exposure.
- `text.py` — `tokenize`, `jaccard_similarity`, `classify_log_severity`,
  `extract_apis`, `extract_services`.
- `metrics.py` / `timing.py` / `validation.py` — helpers.

---

## Data Model

```
incidents (id, title, description, severity, status, incident_type,
           affected_services[], raw_logs[], metrics{}, traces[],
           topology{}, context{}, started_at, resolved_at,
           idempotency_key [indexed])
   │
   ├── findings (id, agent_name, finding_type, description,
   │             confidence, evidence{}, metadata{}
   │             └── metadata_: root_cause_hint, hypotheses,
   │                          provenance {source_type, codepath,
   │                                       fallback_used, execution_ms, model})
   ├── root_causes (id, root_cause, confidence, alternatives[],
   │                explanation, causal_chain[], contributing_factors[])
   ├── resolutions (id, action, steps[], verified,
   │                metadata{confirmed_root_cause, ground_truth_source,
   │                         ground_truth_confidence, confirmed_by, confirmed_at})
   ├── lessons_learned (id, lesson, category, confidence)
   │
patterns (id, pattern_signature, pattern_text, root_cause_hint, confidence,
          occurrence_count, approved, approved_by, incident_ids[], metadata{})

incident_memory (id, text_repr, embedding[], root_cause, resolution,
                 confidence, lessons[], services[])

agent_reliability (id, agent_name, accuracy, precision, recall, latency_avg,
                   confidence_avg, false_positives, false_negatives,
                   true_positives, true_negatives, invocations,
                   reliability_score, last_invocation)

predictions (id, service, predicted_failure_type,
             UNIQUE(service, predicted_failure_type),
             probability, estimated_time_minutes, impact, rationale, evidence{})

meta_reasoning_records (id, incident_id, useful_agents[], unnecessary_agents[],
                        optimal_path, suggestions[], agent_scores{})
```

JSON columns use `JSONBOrJSON` so the schema is identical across PostgreSQL and
SQLite. Embeddings live in a `jsonb` list; there is deliberately **no
`pgvector` dependency** — FAISS (or pure-Python cosine) handles vector search,
and fallbacks work without any Postgres extension.

---

## Concurrency, Rate Limits & Backpressure

- **Agent concurrency**: within one orchestration run all selected agents run
  with `asyncio.gather`. The MDV loop runs one action at a time by design.
- **Investigation backpressure**: `POST /incidents/investigate` acquires a
  global `asyncio.Semaphore(max_concurrent_investigations)` with a 5s
  acquisition timeout → `429` when saturated.
- **Rate limit**: `INVESTIGATION_RATE_LIMIT` (default `10/minute`) is registered
  for the investigation endpoints.
- **Body size**: `MAX_REQUEST_BODY_BYTES` (1 MiB) enforced in middleware.
- **Limits**: `MAX_LOGS`, `MAX_LOG_LINE_CHARS`, `MAX_TRACES`,
  `MAX_AFFECTED_SERVICES`, `MAX_PAGINATION_LIMIT`, `MAX_PREDICTION_BATCH`,
  `MAX_MEMORY_QUERY_LENGTH` — enforced in schema validators and agent
  ingestion.
- **Idempotency**: an `Idempotency-Key` header on both investigate endpoints
  stores the key on the incident row; repeat calls replay the persisted result
  instead of duplicating agent work.

---

## Resilience & Degradation

PRISM degrades gracefully when external subsystems are unavailable:

| Subsystem down | Behavior                                                        |
|----------------|----------------------------------------------------------------|
| PostgreSQL     | **Not graceful** — startup aborts (`db_init_failed`).           |
| Neo4j          | In-memory NetworkX fallback; writes no-op; queries fall back.   |
| FAISS          | Pure-Python cosine similarity fallback.                         |
| Ollama / LLM   | `llm_analyzer` degraded or falls back to rule-based summaries; embeddings fall back to sentence-transformers or hash pseudo-embeddings; `is_ollama_available` probe in `/internal/health`. |
| An agent fails | `BaseAgent.run` wraps exceptions into a zero-confidence error payload (`finding_type="error"`) — never aborts the pipeline. |
| Consensus fails| `"undetermined"` verdict with low confidence + explanation.      |

`create_incident` performs similarity-based deduplication, and concurrent
invocation counters / reliability upserts are guarded so re-runs accumulate
correctly.

---

## Security Model

See [`docs/SECURITY.md`](./SECURITY.md) for the full treatment. Summary:

- **API key** (`api/deps.py`): constant-time comparison (`hmac.compare_digest`).
  Production startup refuses to run without a strong key; unauthenticated
  endpoints are limited to `/health`, `/ready`, `/docs`, `/`, `/api/info`.
- **Redaction** (`utils/redaction.py`): evidence is scrubbed before persistence
  or LLM exposure.
- **LLM trust boundary** (`utils/llm.py`): evidence delimiting +
  schema validation + clamping; raw model output never becomes root cause /
  trusted pattern / reliability grade without validation.
- **Learning guard**: no learning from unverified consensus; agent reliability
  requires explicit `ground_truth_source`.
- **CORS**: fail-closed in production; permissive dev defaults only outside
  production.
- **Security headers** on every response; strict CSP for the embedded UI.
- **Request IDs** (`X-Request-ID`) propagated and logged on every request.
- **Secrets**: `.env` is git-ignored; `validate_production_secrets` rejects weak
  defaults at startup.

---

## Testing Strategy

Tests run **without any external services**: an in-memory SQLite database with
Neo4j / Ollama / FAISS disabled.

```bash
pip install -r requirements.txt
pytest -v
```

Coverage areas (see `tests/`):

- agents (finding shape, error handling)
- orchestrator selection (evidence / reliability / repeat gating, MDV ordering)
- investigation tree construction + MDV/ADG loop termination
- causal graph (source-node ranking, chain reconstruction)
- confidence propagation (convergence, noisy-OR bounds)
- consensus (clustering, quorum, undetermined verdicts, correlated-voter
  discounting)
- knowledge graph store (in-memory fallback + Neo4j record shaping)
- pattern generator + continuous learning (frozen mode, unconfirmed no-op,
  confirmed learning, ground-truth gating)
- meta-reasoning evaluation
- security middleware (headers, request ID, body guard)
- API smoke tests (health, investigate, resolve, patterns, memory, KG,
  predictions) including idempotency and resolve-driven learning

`pytest.ini` wires `asyncio_mode = auto` and async fixtures via
`pytest-asyncio`. For experiment mode, set `LEARNING_MODE=frozen` in the test
environment to keep runs independent.

---

## Extending PRISM

See [`docs/DEVELOPMENT.md`](./DEVELOPMENT.md). Highlights:

### Add a new agent

1. Subclass `BaseAgent` and implement `investigate()`:

```python
# agents/my_agent.py
from agents.base import BaseAgent, FindingPayload

class MyAgent(BaseAgent):
    name = "my_agent"
    supported_incident_types = ["error_rate", "latency"]
    requires = ["logs"]
    priority = 35

    async def investigate(self, context):
        # ...analysis on context["logs"]...
        return FindingPayload(
            agent_name=self.name,
            finding_type="analysis",
            description="...",
            confidence=0.8,
            evidence={...},
            root_cause_hint="...",
        )
```

2. Register it in `agents/registry.py` (import + append to the registration
   loop).
3. Optional: declare a capability in `orchestrator/agent_registry.py` so the
   adaptive selector can rank it. Without a capability it still runs via the
   classic path (sorted by `priority`).

### Add an engine

Engines are plain async/await modules invoked from `api/investigation.py`
(`_run_investigation`) or the pipeline. Keep public entry points
side-effect-free (pure computation, then persist or return) and expose
dataclasses / pydantic outputs.

### Add an API route

Create a router in `api/`, include it in `main.py`, add schemas to
`models/schemas.py`, and — if sensitive — protect with
`Depends(require_api_key)`.

---

## Configuration Reference

All settings are environment variables (or `.env`); see `.env.example` for a
copy-with-values template and [`docs/CONFIGURATION.md`](./docs/CONFIGURATION.md)
for the complete reference.

| Variable                                | Default                         | Purpose                                        |
|-----------------------------------------|---------------------------------|------------------------------------------------|
| `APP_ENV`                               | `development`                   | `production` enables strict secret validation |
| `APP_LOG_LEVEL`                         | `INFO`                          | structlog level                               |
| `APP_HOST` / `APP_PORT`                 | `0.0.0.0` / `8000`              | Uvicorn bind                                  |
| `API_KEY`                               | *(required in prod)*            | `X-API-Key` bearer for sensitive endpoints    |
| `CORS_ORIGINS`                          | *(required in prod)*            | Comma-separated allowed origins               |
| `DATABASE_URL`                          | asyncpg URL → Postgres          | Async SQLAlchemy URL                          |
| `DATABASE_SYNC_URL`                     | psycopg2 URL                    | Sync URL for migrations/scripts               |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | `bolt://localhost:7687` + defaults | Neo4j connection                 |
| `OLLAMA_HOST` / `OLLAMA_MODEL` / `OLLAMA_EMBED_MODEL` | `localhost:11434`, `llama3.1:8b`, `nomic-embed-text` | LLM + embed models |
| `SENTENCE_TRANSFORMER_MODEL`            | `all-MiniLM-L6-v2`              | Embedding model                               |
| `FAISS_INDEX_PATH`                      | `./data/faiss_index`            | FAISS snapshot location                       |
| `MAX_CONCURRENT_INVESTIGATIONS`         | `3`                             | Semaphore slots for investigations            |
| `INVESTIGATION_RATE_LIMIT`              | `10/minute`                     | Investigation rate limit                      |
| `MAX_LOG_LINES` / `MAX_LOG_LINE_CHARS`  | `500` / `2000`                  | Log ingestion limits                          |
| `MAX_TRACES` / `MAX_AFFECTED_SERVICES`  | `200` / `50`                    | Ingestion limits                              |
| `MAX_REQUEST_BODY_BYTES`                | `1 MiB`                         | Request body guard                            |
| `PRISM_MDV_THRESHOLD`                   | `0.15`                          | MDV stop threshold for the investigation loop |
| `PRISM_MAX_INVESTIGATION_STEPS`         | `20`                            | Max loop iterations                           |
| `PRISM_EXPERIMENT_LOGGING`              | `false`                         | Enable per-iteration experiment logging       |
| `AGENT_DEFAULT_RELIABILITY` / `AGENT_MIN_RELIABILITY` | `0.5` / `0.2` | Reliability gates                |
| `AGENT_RELIABILITY_DECAY`               | `0.9`                           | EMA decay for moving averages                 |
| `AGENT_TIMEOUT_SECONDS`                 | `45.0`                          | Per-agent timeout                             |
| `CONSENSUS_MIN_VOTERS` / `CONSENSUS_CONFIDENCE_THRESHOLD` | `2` / `0.6` | Consensus tuning          |
| `LEARNING_MODE`                         | `online`                        | `online` (persistent) vs `frozen` (reproducible experiments) |
| `LEARNING_REQUIRE_CONFIRMATION`         | `true`                          | Refuse learning without a confirmed root cause |
| `ENABLE_NEO4J` / `ENABLE_OLLAMA` / `ENABLE_FAISS` | `true`×3              | Feature flags for external backends           |

---

## Performance Notes

- **Redaction + evidence compression** (`utils/redaction.py`,
  `utils/evidence.py`) truncate and sanitize large log/metric payloads before
  agents run, keeping the graph and DB writes bounded.
- **Concurrent agents** make orchestrator runs latency-bound by the slowest
  agent (`llm_analyzer` is the most expensive — `execution_cost=0.9`).
- **FAISS snapshots** on disk allow warm restarts without re-embedding history;
  the in-memory index is rebuilt lazily on load.
- The **MDV loop** avoids running every agent on every incident: LLM dives are
  conditional, and actions whose MDV falls below the threshold terminate the
  investigation early.
- **SSE streaming** lets the console render progress incrementally instead of
  waiting for the full pipeline.
- **Idempotency replay** avoids duplicate agent work on retried requests.