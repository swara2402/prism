# PRISM — Development

How to set up, test, and extend PRISM.

---

## Local setup

Requires **Python 3.11+**.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# If you have server-side integrations locally (optional):
cp .env.example .env
docker compose up -d            # postgres (localhost:5433), neo4j, ollama
```

With no `.env` and no external services, PRISM still runs: the knowledge
graph falls back to an in-memory graph, LLM agents report `unavailable`, and
memory runs in degraded mode. For the full experience, bring up the Compose
stack and set the real URLs/keys in `.env`.

```bash
uvicorn main:app --reload        # dev; auto-reload disabled in production
```

Smoke check: `curl -s localhost:8000/health` → `{"status":"ok"}`. Interactive
docs: http://localhost:8000/docs. Web console: http://localhost:8000/.

## Running the tests

The suite runs **fully offline** — in-memory SQLite, with Neo4j, Ollama and
FAISS stubbed/disabled via `tests/conftest.py`. No Docker or external services
required.

```bash
python3 -m pytest -q -p no:cacheprovider        # 96 tests, all expected to pass
python3 -m pytest -q -p no:cacheprovider -k agent
```

- `pytest.ini` sets `asyncio_mode = auto`, so async tests need no explicit
  markers.
- `conftest.py` forces `APP_ENV=test` + memory SQLite + a throwaway API key
  before any settings import, and exposes `db_session`, `api_headers`, and
  `client` fixtures.
- Lint with `python3 -m ruff check .` (must stay clean).

Test layout:

| File                          | Covers                                        |
|-------------------------------|-----------------------------------------------|
| `tests/test_agents.py`        | Agent contract, structured findings           |
| `tests/test_api.py`           | Endpoint behavior, auth, limits, idempotency  |
| `tests/test_causal_graph.py`  | `CausalGraph`, `find_root_causes`, chains      |
| `tests/test_consensus.py`     | Majority/weighted consensus, independent codepaths |
| `tests/test_explainability.py`| Evidence aggregation, confidence breakdown    |
| `tests/test_governance.py`    | Learning gates, ground-truth validation, pattern approval |
| `tests/test_investigation_tree.py` | `InvestigationTree`, StepSpec, run_tree |
| `tests/test_knowledge_graph_store.py` | Neo4j / in-memory graph fallback |
| `tests/test_learning.py`      | `learn_from_incident`, pattern generation, reliability updates |
| `tests/test_mdv_loop.py`      | MDV stop threshold, agent selection           |
| `tests/test_meta_reasoning.py`| Agent usefulness scoring                      |
| `tests/test_pattern_generator.py` | Signature extraction, dedup               |
| `tests/test_security.py`      | Redaction, auth fail-closed, weak secrets     |
| `tests/test_utils.py`         | LLM JSON extraction, general helpers          |

## Code layout

```
main.py                 App entry: lifespan, middleware, routers, probes
config/                 pydantic-settings + structlog configuration
api/                    FastAPI routers + auth dependency (deps.py)
models/                 Pydantic schemas shared across the API
agents/                 The 8 analyzers (rule-based, log, metric, trace,
                        topology, historical, knowledge-graph, llm)
orchestrator/           Registry, adaptive selector, investigation engine,
                        finding/graph persistence wiring
investigation/          Tree exploration (MDV), state, metrics, hypothesis
                        manager, reliability store, experiment logger,
                        value (MDV), action-effectiveness (EMA), diagnostic-gain
causal_graph/           CausalGraph + builder + root-cause search
confidence/             Noisy-OR belief propagation (5 iterations)
consensus/              Weighted consensus, voter opinions
memory/                 Embeddings + FAISS memory store + SQL fallback
knowledge_graph/        Neo4j store + in-memory fallback
learning/               Pattern generator + continuous learning (gated)
prediction/             Failure forecasting + persistence
explainability/         Evidence aggregation + explanations
meta_reasoning/         Agent usefulness meta-scores
database/               Async engine, session, models, repositories
utils/                  Redaction, LLM trust-boundary helpers
static/                 Embedded command console
data/                   Local FAISS index + agent reliability JSON
tests/                  96 offline tests
```

See `docs/ARCHITECTURE.md` for a module-by-module deep dive.

## Extending PRISM

### Add an agent

1. Implement `AsyncGenerateOutput | bytes | None`-ish behavior in a new module
   under `agents/` — mirror an existing analyzer (they all return a
   `structured` finding dict: `finding_type`, `summary`, `confidence`,
   `evidence`, and on failure `{"finding_type": "error", ...}` with zero
   confidence). Agents must never raise: catch exceptions and report them.
2. Register it in the registry with its skills (associations to incident
   types), default reliability, and dependencies (e.g. `external=True` for
   anything hitting a remote system).
3. Wire it in `orchestrator/agent_registry.py` so the selector can rank it.
4. Add tests under `tests/test_agents.py`.
5. Reliability grading applies automatically once findings come back through
   the orchestrator's persistence path.

### Add a route/endpoint

- Create/extend a router under `api/` (see `api/patterns.py` for the lightest
  pattern), include it in `main.py`, and guard sensitive operations with
  `Depends(require_api_key)` from `api/deps.py`.
- Add the path to `GET /api/info`'s `endpoints` list and to
  `docs/API.md`.

### Add a learning sink

`learning/continuous_learning.py` `learn_from_incident` persists patterns,
memory, KG edges, and per-agent reliability — all behind the confirmation
gate. A new sink must:

- only run from `learn_from_incident` (never from the investigation path),
- honor `learning_mode == "online"` and `learning_require_confirmation`
  (the resolve API already enforces ground-truth source via
  `ALLOWED_GROUND_TRUTH_SOURCES`),
- record its provenance (who/what confirmed it), and
- come with a governance test in `tests/test_governance.py`.

### Experiment / evaluation mode

For reproducible evaluation:

```bash
LEARNING_MODE=frozen uvicorn main:app
```

`frozen` disables all learning, so every run starts from identical state and
scenarios stay statistically independent. Services remain fully functional.

## Contributing

- Keep lint clean: `python3 -m ruff check .`
- Keep the full suite green: `python3 -m pytest -q -p no:cacheprovider`
- Preserve the trust boundaries: no learning from unconfirmed consensus, no
  raw credentials/PII persisted or sent to models (run evidence through
  `utils/redaction.scrub`).
- Update `docs/` alongside code: endpoint changes → `docs/API.md`; new
  settings → `docs/CONFIGURATION.md`; security-relevant changes →
  `docs/SECURITY.md`; architectural changes → `docs/ARCHITECTURE.md`.
- Never commit `.env`, real keys, or local `data/` artifacts (already in
  `.gitignore`).