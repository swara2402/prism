"""
main
====

Application entry point for PRISM.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Callable

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from api.agents import router as agents_router
from api.deps import require_api_key
from api.investigation import router as investigation_router
from api.knowledge_graph import router as kg_router
from api.memory import router as memory_router
from api.patterns import router as patterns_router
from api.predictions import router as predictions_router
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
    logger.info(
        "app_startup",
        extra={
            "env": settings.app_env,
            "host": settings.app_host,
            "port": settings.app_port,
            "api_key_configured": bool(settings.api_key),
        },
    )
    try:
        await init_db()
        logger.info("db_initialized")
    except Exception as exc:
        logger.critical("db_init_failed", extra={"error": repr(exc)})
        raise

    try:
        await MemoryStore.get().load()
        logger.info("memory_store_loaded")
    except Exception as exc:
        logger.critical("memory_store_init_failed", extra={"error": repr(exc)})
        raise

    try:
        KnowledgeGraphStore.get()
        logger.info("knowledge_graph_ready")
    except Exception as exc:
        logger.critical("kg_init_failed", extra={"error": repr(exc)})
        raise

    worker_task = None
    if settings.job_worker_enabled:
        from utils.job_queue import LocalWorker
        worker = LocalWorker(
            poll_interval=settings.job_poll_interval,
            attempts_max=settings.job_attempts_max,
        )

        async def _run_worker() -> None:
            try:
                await worker.run()
            except asyncio.CancelledError:
                logger.info("job_worker_stopping")
                raise

        worker_task = asyncio.create_task(_run_worker())
        logger.info("job_worker_started")

    yield

    if worker_task is not None:
        worker_task.cancel()
        try:
            await worker_task
        except BaseException:
            pass
    try:
        await KnowledgeGraphStore.get().close()
    except Exception as exc:
        logger.warning("knowledge_graph_close_failed", extra={"error": repr(exc)})
    try:
        await close_db()
    except Exception as exc:
        logger.warning("database_close_failed", extra={"error": repr(exc)})
    logger.info("app_shutdown")


# Keep interactive documentation available for local development, but do not
# expose Swagger/ReDoc or the OpenAPI schema on a production public service.
_docs_url = None if settings.is_production else "/docs"
_redoc_url = None if settings.is_production else "/redoc"
_openapi_url = None if settings.is_production else "/openapi.json"

app = FastAPI(
    title="PRISM — Enterprise Agentic AI Incident Investigation Framework",
    description=(
        "AI-assisted incident investigation with multi-agent analysis, causal reasoning, "
        "confidence, consensus, memory, and explainability."
    ),
    version="1.1.0",
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=_openapi_url,
    lifespan=lifespan,
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        logger.info(
            "request_received",
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "method": request.method,
            },
        )

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_request_body_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large"},
                        headers={"X-Request-ID": request_id},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                    headers={"X-Request-ID": request_id},
                )

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "unhandled_exception",
                extra={"request_id": request_id, "path": request.url.path, "method": request.method},
            )
            raise

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cache-Control"] = "no-store"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # Swagger is local-development only, so the production UI can use a
        # stricter policy. The UI still has a small amount of inline CSS/JS,
        # hence unsafe-inline remains intentionally limited to this embedded app.
        if request.url.path.startswith("/docs") or request.url.path == "/openapi.json":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://unpkg.com; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; "
                "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://unpkg.com; "
                "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
                "img-src 'self' data:; "
                "connect-src 'self'"
            )

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "request_finished",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)

_origins = settings.cors_origins_list
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "X-Request-ID", "Idempotency-Key", "X-Tenant-Id"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
elif not settings.is_production:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000", "http://localhost:8000",
            "http://127.0.0.1:3000", "http://127.0.0.1:8000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )


app.include_router(investigation_router)
app.include_router(patterns_router)
app.include_router(memory_router)
app.include_router(kg_router)
app.include_router(agents_router)
app.include_router(predictions_router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """Minimal unauthenticated liveness probe."""
    return {"status": "ok"}


@app.get("/internal/health", tags=["meta"])
async def internal_health(_api_key: str = Depends(require_api_key)) -> dict:
    """Authenticated deep health information."""
    kg_store = KnowledgeGraphStore.get()
    kg_status = "connected" if hasattr(kg_store, "_driver") and kg_store._driver else "fallback_mode"
    memory_store = MemoryStore.get()
    memory_status = "ready" if memory_store._loaded else "not_loaded"

    ollama_available = False
    try:
        from utils.llm import is_ollama_available
        ollama_available = await is_ollama_available()
    except Exception:
        pass

    return {
        "status": "ok",
        "env": settings.app_env,
        "version": "1.1.0",
        "memory_size": memory_store.size(),
        "subsystems": {
            "database": "connected",
            "memory": memory_status,
            "faiss": memory_status,
            "neo4j": kg_status,
            "ollama": "available" if ollama_available else "unavailable",
        },
    }


@app.get("/ready", tags=["meta"])
async def readiness() -> Response:
    """Readiness probe for routing traffic to a usable instance."""
    checks = {"database": False, "memory": MemoryStore.get()._loaded}
    try:
        from database.session import get_async_session_local
        AsyncSessionLocal = get_async_session_local()
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:
        logger.warning("readiness_database_failed", extra={"error": repr(exc)})

    ready = all(checks.values())
    payload = {"status": "ready" if ready else "not_ready", "checks": checks}
    if not ready:
        return JSONResponse(status_code=503, content=payload)
    return JSONResponse(content=payload)


@app.get("/", include_in_schema=False, name="ui")
async def ui_root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/info", tags=["meta"])
async def service_info(_api_key: str = Depends(require_api_key)) -> dict:
    """Authenticated service metadata for the UI."""
    return {
        "name": "PRISM — Enterprise Agentic AI Incident Investigation Framework",
        "version": "1.1.0",
        "docs": "/docs" if not settings.is_production else None,
        "auth": "X-API-Key",
        "endpoints": [
            "/incidents/investigate", "/incidents", "/incidents/{id}",
            "/incidents/{id}/root-cause", "/incidents/{id}/resolve",
            "/patterns/pending", "/patterns/approved", "/patterns/{id}/approve", "/patterns/match",
            "/memory/search", "/memory/stats", "/kg/services", "/kg/apis", "/kg/changes",
            "/kg/services/subgraph", "/agents", "/agents/{name}", "/predictions/run",
            "/predictions", "/health", "/internal/health",
        ],
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=not settings.is_production,
        log_level=settings.app_log_level.lower(),
    )
