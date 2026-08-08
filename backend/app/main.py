"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, new_request_id, request_id_ctx
from app.db.models import Chunk, LegalAct
from app.db.session import SessionLocal, engine
from app.schemas.common import HealthResponse
from app.services.llm import LLMProviderError, llm_router
from app.services.rag.embedder import embedder
from app.services.rag.reranker import reranker

VERSION = "1.0.0"
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("DEBUG" if settings.DEBUG else "INFO")
    log.info(
        "starting",
        extra={
            "version": VERSION,
            "env": settings.ENV,
            "vector_backend": settings.VECTOR_BACKEND.value,
            "llm_provider": settings.LLM_PROVIDER.value,
        },
    )
    # Run migrations here instead of as a separate startCommand step, so the
    # process binds its port immediately and migrations happen in the background
    # of the same PID uvicorn's healthcheck is probing.
    try:
        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config("alembic.ini")
        await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
        log.info("migrations applied")
    except Exception as exc:
        log.warning("migration step failed", extra={"error": str(exc)})

    # Warm the embedding model so the first user query does not pay the load —
    # in the background, deliberately.
    #
    # Awaiting it here would hold the port closed until bge-m3 is resident, and
    # when the weights are not baked into the image that means waiting on a
    # ~2.3 GB download. The platform health check starts probing well before
    # that finishes, so a blocking warmup turns a slow first request into a
    # failed deploy. Booting immediately and degrading is the better trade:
    # /health reports `dense_retrieval: false` until the model is ready, and
    # queries in the meantime are served by the keyword branches.
    async def _warm() -> None:
        try:
            await embedder.embed_texts(["warmup"], use_cache=False)
            log.info("embedding_model_warm")
            # Warm the cross-encoder too. Left lazy, its first load happens
            # inside whichever user request arrives first, and pulling ~2.3 GB
            # takes longer than Fly's proxy will wait — that request dies with
            # a 502 rather than merely being slow.
            if await reranker.warm():
                log.info("reranker_model_warm")
        except Exception as exc:
            # error, not warning: if the warmup cannot embed, *no* query can,
            # and the service answers every question from keyword search while
            # still looking healthy. This failing is a production incident.
            log.error(
                "embedding_warmup_failed_dense_retrieval_disabled",
                extra={"error": str(exc), "model": settings.EMBEDDING_MODEL},
            )

    warmup_task = asyncio.create_task(_warm())

    yield

    warmup_task.cancel()

    await embedder.close()
    await engine.dispose()
    log.info("stopped")


app = FastAPI(
    title=settings.APP_NAME,
    version=VERSION,
    description=(
        "AI legal research platform for the Republic of Uzbekistan. "
        f"{settings.DISCLAIMER}"
    ),
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or new_request_id()
    request_id_ctx.set(request_id)
    started = time.perf_counter()

    response = await call_next(request)

    duration_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-ms"] = str(duration_ms)
    # SSE responses are long-lived by design; logging their duration is noise.
    if response.media_type != "text/event-stream":
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
    return response


_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limit", "quota", "tpd", "tpm")


@app.exception_handler(LLMProviderError)
async def llm_provider_error(request: Request, exc: LLMProviderError):
    """Surface upstream LLM failures as themselves, not as a generic 500.

    An exhausted provider quota is a transient, client-actionable condition —
    collapsing it into 500 makes it indistinguishable from a real defect, so
    callers cannot tell "retry later" from "this endpoint is broken" and
    retry logic has nothing to key on.
    """
    message = str(exc)
    is_rate_limited = any(m in message.lower() for m in _RATE_LIMIT_MARKERS)
    log.warning(
        "llm provider error",
        extra={"path": request.url.path, "rate_limited": is_rate_limited, "error": message[:300]},
    )
    return JSONResponse(
        status_code=(
            status.HTTP_429_TOO_MANY_REQUESTS
            if is_rate_limited
            else status.HTTP_502_BAD_GATEWAY
        ),
        content={
            "detail": (
                "LLM provider rate limit or quota exceeded — retry later, or switch "
                "provider via LLM_PROVIDER."
                if is_rate_limited
                else "LLM provider unavailable."
            ),
            "provider_error": message[:500],
            "request_id": request_id_ctx.get(),
        },
        headers={"Retry-After": "300"} if is_rate_limited else None,
    )


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    log.exception("unhandled error", extra={"path": request.url.path})
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "internal server error",
            "request_id": request_id_ctx.get(),
        },
    )


app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health():
    database_ok = False
    corpus = {"acts": 0, "chunks": 0, "embedded_chunks": 0}

    async def _check_db() -> None:
        nonlocal database_ok
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
            database_ok = True
            corpus["acts"] = (
                await session.execute(select(func.count(LegalAct.id)))
            ).scalar_one()
            corpus["chunks"] = (
                await session.execute(select(func.count(Chunk.id)))
            ).scalar_one()
            corpus["embedded_chunks"] = (
                await session.execute(
                    select(func.count(Chunk.id)).where(Chunk.embedding.isnot(None))
                )
            ).scalar_one()

    try:
        await asyncio.wait_for(_check_db(), timeout=3)
    except Exception as exc:
        log.warning("database health check failed", extra={"error": str(exc)})

    redis_ok = False

    async def _check_redis() -> None:
        nonlocal redis_ok
        client = aioredis.from_url(str(settings.REDIS_DSN))
        try:
            await client.ping()
            redis_ok = True
        finally:
            await client.aclose()

    try:
        await asyncio.wait_for(_check_redis(), timeout=3)
    except Exception as exc:
        log.warning("redis health check failed", extra={"error": str(exc)})

    # Whether dense retrieval can actually run. `corpus.embedded_chunks` only
    # says the *documents* were embedded; it stays green even when the query
    # side cannot embed anything, in which case every search silently runs on
    # keyword matching alone. Checking the query side is the part that matters.
    dense_ok = False
    dense_error: str | None = None

    try:
        dense_ok, dense_error = embedder.availability()
    except Exception as exc:  # noqa: BLE001
        dense_error = str(exc)

    if not dense_ok:
        log.error("dense_retrieval_unavailable", extra={"error": dense_error})

    return HealthResponse(
        status="ok" if (database_ok and dense_ok) else "degraded",
        version=VERSION,
        database=database_ok,
        redis=redis_ok,
        vector_backend=settings.VECTOR_BACKEND.value,
        llm_providers=llm_router.available(),
        corpus=corpus,
        dense_retrieval=dense_ok,
        dense_retrieval_error=dense_error,
    )


@app.get("/", tags=["meta"])
async def root():
    return {
        "name": settings.APP_NAME,
        "version": VERSION,
        "jurisdiction": "Uzbekistan",
        "languages": ["uz-Latn", "uz-Cyrl", "ru", "en"],
        "docs": "/docs",
        "disclaimer": settings.DISCLAIMER,
    }
