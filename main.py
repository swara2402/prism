"""PRISM application entry point."""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Callable

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from api.agents import router as agents_router
from api.auth import router as auth_router
from api.deps import require_api_key
from api.investigation import router as investigation_router
from api.knowledge_graph import router as kg_router
from api.memory import router as memory_router
from api.patterns import router as patterns_router
from api.predictions import router as predictions_router
from auth.security import COOKIE_NAME, bootstrap_owner
from config.logging import configure_logging, get_logger
from config.settings import settings
from database.session import close_db, init_db
from knowledge_graph.store import KnowledgeGraphStore
from memory.store import MemoryStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("app_startup", extra={"env": settings.app_env, "host": settings.app_host, "port": settings.app_port})
    await init_db()
    await bootstrap_owner()
    # Memory is deliberately NOT loaded globally. FAISS indexes are tenant
    # scoped and are hydrated lazily by the historical analyzer.
    KnowledgeGraphStore.get()
    worker_task = None
    if settings.job_worker_enabled:
        from utils.job_queue import LocalWorker
        worker = LocalWorker(poll_interval=settings.job_poll_interval, attempts_max=settings.job_attempts_max)

        async def _run_worker():
            try:
                await worker.run()
            except asyncio.CancelledError:
                raise

        worker_task = asyncio.create_task(_run_worker())
    yield
    if worker_task is not None:
        worker_task.cancel()
        try:
            await worker_task
        except BaseException:
            pass
    try:
        await KnowledgeGraphStore.get().close()
    except Exception:
        logger.exception("knowledge_graph_close_failed")
    await close_db()


_docs_url = None if settings.is_production else "/docs"
_redoc_url = None if settings.is_production else "/redoc"
_openapi_url = None if settings.is_production else "/openapi.json"
app = FastAPI(
    title="PRISM — Incident Intelligence",
    version="2.0.0",
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=_openapi_url,
    lifespan=lifespan,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_request_body_bytes:
                    return JSONResponse(status_code=413, content={"detail": "Request body too large"}, headers={"X-Request-ID": request_id})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header"}, headers={"X-Request-ID": request_id})
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cache-Control"] = "no-store"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; img-src 'self' data:; connect-src 'self'"
        logger.info("request_finished", extra={"request_id": request_id, "method": request.method, "path": request.url.path, "status": response.status_code, "duration_ms": round((time.perf_counter() - start) * 1000, 2)})
        return response


app.add_middleware(SecurityHeadersMiddleware)
_origins = settings.cors_origins_list
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-API-Key", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
elif not settings.is_production:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://localhost:8000", "http://127.0.0.1:3000", "http://127.0.0.1:8000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth_router)
app.include_router(investigation_router)
app.include_router(patterns_router)
app.include_router(memory_router)
app.include_router(kg_router)
app.include_router(agents_router)
app.include_router(predictions_router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok", "version": "2.0.0"}


@app.get("/internal/health", tags=["meta"])
async def internal_health(_api_key: str = Depends(require_api_key)) -> dict:
    kg_store = KnowledgeGraphStore.get()
    memory_store = MemoryStore.get()
    return {
        "status": "ok",
        "env": settings.app_env,
        "version": "2.0.0",
        "memory_scope": "tenant-bound-lazy",
        "memory_size": memory_store.size(),
        "subsystems": {
            "database": "connected",
            "memory": "lazy",
            "faiss": "tenant-scoped-lazy" if settings.enable_faiss else "disabled",
            "neo4j": "connected" if getattr(kg_store, "_driver", None) else "fallback_mode",
        },
    }


@app.get("/ready", tags=["meta"])
async def readiness() -> Response:
    checks = {"database": False}
    try:
        from database.session import get_async_session_local
        async with get_async_session_local() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        pass
    payload = {"status": "ready" if all(checks.values()) else "not_ready", "checks": checks}
    return JSONResponse(status_code=200 if all(checks.values()) else 503, content=payload)


@app.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/", include_in_schema=False, name="ui")
async def ui_root(request: Request):
    if not request.cookies.get(COOKIE_NAME):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/app.js", include_in_schema=False)
async def ui_bundle() -> Response:
    """Serve the existing console bundle with the session bridge prepended.

    Keeping the bridge at the HTTP boundary lets us preserve the large legacy
    console bundle while migrating browser authentication away from API keys.
    """
    legacy = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    bridge = (STATIC_DIR / "session-bridge.js").read_text(encoding="utf-8")
    return Response(bridge + "\n" + legacy, media_type="application/javascript")


@app.get("/api/info", tags=["meta"])
async def service_info(_api_key: str = Depends(require_api_key)) -> dict:
    return {
        "name": "PRISM — Incident Intelligence",
        "version": "2.0.0",
        "docs": "/docs" if not settings.is_production else None,
        "auth": "PRISM session cookie or tenant-bound service account",
        "epistemic_model": ["observed", "evidence", "inference", "confirmed"],
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.app_host, port=settings.app_port, reload=not settings.is_production, log_level=settings.app_log_level.lower())
