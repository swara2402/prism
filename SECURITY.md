# PRISM Security Guide

## Security model

PRISM is an incident-investigation service. Treat logs, traces, metrics, topology metadata, and incident descriptions as **untrusted input**.

Production deployments should use:

- `APP_ENV=production`
- a random `API_KEY` of at least 32 characters
- explicit `CORS_ORIGINS`
- strong PostgreSQL credentials
- strong Neo4j credentials
- TLS at the reverse proxy/load balancer
- private networking for PostgreSQL, Neo4j, and Ollama
- backups and tested restores

The production application disables interactive API documentation. Development keeps `/docs` and `/redoc` available for convenience.

## Authentication

Sensitive endpoints use the `X-API-Key` header. API-key authentication is suitable for an internal deployment or a first production hardening stage, but it is **not a complete SaaS identity system**.

For a multi-user SaaS deployment, add OIDC/OAuth2 authentication, organization membership, RBAC, service accounts, and server-enforced tenant isolation before exposing customer data to multiple organizations.

Do not treat a client-supplied `X-Tenant-Id` header as proof of tenant identity.

## Browser security

PRISM's browser console is designed for same-origin deployment. Avoid embedding it in a page that can inject arbitrary JavaScript. Keep CSP, security headers, and HTTPS enabled in production.

Do not paste production API keys into shared/public machines. Prefer short-lived credentials when a proper identity provider is available.

## AI and data poisoning

Incident evidence can contain attacker-controlled text. Logs must be treated as evidence, never as instructions. PRISM redacts sensitive fields before persistence/LLM exposure and defers continuous learning until a root cause has been confirmed.

Never automatically promote an unverified incident into a trusted detection pattern.

## Production checklist

- [ ] HTTPS enabled
- [ ] `API_KEY` rotated from any development value
- [ ] `CORS_ORIGINS` contains only the real frontend origin(s)
- [ ] PostgreSQL is private and uses a least-privileged account
- [ ] Neo4j Bolt/HTTP ports are private
- [ ] Ollama is private
- [ ] `/docs`, `/redoc`, and `/openapi.json` are not publicly exposed
- [ ] reverse proxy/WAF rate limiting configured
- [ ] database backups tested
- [ ] application logs do not contain secrets
- [ ] incident evidence retention policy defined
- [ ] customer/tenant isolation implemented before multi-tenant use
- [ ] OIDC/RBAC implemented before enterprise multi-user SaaS use

## Reporting a vulnerability

Do not publish credentials, API keys, customer logs, or other sensitive data in a public issue. Report security problems privately to the repository owner through an appropriate private channel.
