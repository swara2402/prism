# WayPoint Quick Start

WayPoint is an AI-assisted incident investigation console. The fastest way to try it is Docker Compose.

## 1. Start WayPoint

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8000`.

## 2. First run

WayPoint ships with ready-made investigation scenarios in the **Investigate** screen:

- Memory / OOM
- DB pool exhaustion
- 502/504 spike
- CPU saturation
- Cascading authentication outage

Pick a scenario and click **Run investigation**. You do not need to construct logs, metrics, or traces manually for the demo.

## 3. API authentication

Development can run without an API key. For a protected deployment, set a random value of at least 32 characters:

```env
APP_ENV=production
API_KEY=<generate-a-long-random-secret>
CORS_ORIGINS=https://your-prism-ui.example
```

Production refuses to start when required secrets are missing or obvious defaults are used.

## 4. Production checklist

Before exposing WayPoint to the internet:

- Use HTTPS at the load balancer/reverse proxy.
- Set `APP_ENV=production`.
- Set a strong `API_KEY`.
- Set explicit `CORS_ORIGINS`.
- Use non-default PostgreSQL and Neo4j credentials.
- Keep PostgreSQL, Neo4j, and Ollama on a private network.
- Put PRISM behind a WAF/reverse proxy and rate-limit at the edge.
- Back up PostgreSQL and test restoration.
- Run investigations through `/incidents/investigate/async` for long-running workloads.

Swagger/ReDoc are intentionally disabled automatically in production. They remain available in development at `/docs` and `/redoc`.

## 5. Important security model

WayPoint treats incoming logs, traces, metrics, and context as **untrusted evidence**. Evidence is redacted before persistence/LLM exposure, and continuous learning is deferred until an incident has a confirmed root cause.

The current API-key model is suitable for a protected single-tenant/internal deployment. It is **not** a complete multi-tenant SaaS identity system. For SaaS, add OIDC/OAuth2 users, tenant-bound credentials, RBAC, and database-level tenant isolation before onboarding unrelated organizations.
