# PRISM authentication, RBAC and tenant isolation

PRISM now has two authentication modes:

1. **Human console:** email/password login, an HttpOnly `prism_session` cookie, workspace-bound identity and RBAC.
2. **Machine integrations:** the existing `X-API-Key` credential remains available for automation. It is intentionally tenantless and should be used only by a trusted single-tenant integration until you issue per-tenant service credentials.

## Roles

| Role | Read | Investigate / resolve | Admin APIs | Manage workspace |
|---|---|---|---|---|
| Viewer | Yes | No | No | No |
| Engineer | Yes | Yes | No | No |
| Admin | Yes | Yes | Yes | No |
| Owner | Yes | Yes | Yes | Yes |

Pattern approval, Knowledge Graph writes, agent/prediction administration and workspace membership require elevated roles.

## First login

Set these values before first startup:

```env
PRISM_JWT_SECRET=<32+ random characters>
PRISM_BOOTSTRAP_EMAIL=you@company.com
PRISM_BOOTSTRAP_PASSWORD=<12+ character unique password>
PRISM_BOOTSTRAP_TENANT_NAME=My PRISM Workspace
```

On the first startup, PRISM creates the workspace and owner account. After that, remove `PRISM_BOOTSTRAP_PASSWORD` from the runtime environment. The bootstrap routine does nothing once a user exists.

Open `/`. You are redirected to the clean sign-in page. The browser never needs a long-lived API key and the session is kept in an HttpOnly, SameSite cookie.

## Tenant isolation

Authenticated console requests derive the tenant from the signed session, not from a user-controlled `X-Tenant-Id`. Existing incident and background-job APIs receive that server-derived tenant automatically and reject cross-workspace object access.

Do not treat `X-Tenant-Id` as an authentication mechanism. For machine integrations, issue a dedicated tenant-bound credential before enabling multi-tenant automation.

## Production requirements

- `PRISM_JWT_SECRET` must be at least 32 random characters.
- Use HTTPS so the production session cookie is `Secure`.
- Configure explicit `CORS_ORIGINS`.
- Keep the legacy API key out of browser code.
- Rotate bootstrap credentials after initial provisioning.
- Add tenant IDs to any newly persisted product data before exposing it to multiple organizations.
