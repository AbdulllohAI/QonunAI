"""Background tasks.

Celery is synchronous; the services are async. Each task therefore opens its own
event loop with `asyncio.run` and its own DB session — never reuse the API's
request-scoped session here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update

from app.core.logging import configure_logging, get_logger
from app.db.models import (
    Chunk,
    CrossReference,
    Language,
    LegalAct,
    LegalAlert,
    QueryLog,
)
from app.db.session import SessionLocal, engine
from app.services.ingestion import get_connector, ingestion_pipeline
from app.services.rag.embedder import embedder
from app.workers.celery_app import celery_app

configure_logging()
log = get_logger(__name__)


def _run(coro):
    """Run an async coroutine in a fresh loop and dispose the engine after.

    Disposing matters: a Celery worker with `max_tasks_per_child` recycles
    processes, and a pool left open across a fork produces connections shared
    between processes.
    """

    async def wrapper():
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


# ---------------------------------------------------------------- ingestion


@celery_app.task(name="app.workers.tasks.ingest_connector_task", bind=True)
def ingest_connector_task(
    self,
    connector: str = "lexuz",
    identifiers: list[str] | None = None,
    languages: list[str] | None = None,
    search_terms: list[str] | None = None,
    seeds: bool = True,
    force: bool = False,
    limit: int | None = None,
) -> dict:
    async def job() -> dict:
        async with SessionLocal() as session:
            conn = get_connector(connector)
            discover_kwargs: dict = {}
            if connector == "lexuz":
                discover_kwargs = {"seeds": seeds, "search_terms": search_terms or []}
            elif connector == "norma":
                discover_kwargs = {"max_pages": 3}

            stats = await ingestion_pipeline.run_connector(
                session,
                conn,
                identifiers=identifiers,
                languages=[Language(v) for v in languages] if languages else None,
                force=force,
                limit=limit,
                **discover_kwargs,
            )
            return stats.to_dict()

    log.info("ingest task starting", extra={"connector": connector, "task_id": self.request.id})
    return _run(job())


@celery_app.task(name="app.workers.tasks.seed_csv_task")
def seed_csv_task(
    csv_path: str,
    short_name: str,
    act_type: str = "code",
    language: str = "uz-Latn",
    title: str | None = None,
    source_url: str | None = None,
) -> dict:
    from app.db.models import ActType
    from app.services.ingestion import csv_seed_loader

    async def job() -> dict:
        async with SessionLocal() as session:
            stats = await csv_seed_loader.load(
                session,
                csv_path,
                short_name=short_name,
                act_type=ActType(act_type),
                language=Language(language),
                title=title,
                source_url=source_url,
            )
            return stats.to_dict()

    return _run(job())


# ------------------------------------------------------------------ indexing


@celery_app.task(name="app.workers.tasks.reindex_corpus_task", bind=True)
def reindex_corpus_task(self, batch_size: int = 128, only_missing: bool = False) -> dict:
    """Re-embed chunks. Run after changing `EMBEDDING_MODEL`.

    Vectors from different models are not comparable, so a model change without
    a reindex silently degrades retrieval rather than failing loudly.
    """

    async def job() -> dict:
        processed = 0
        async with SessionLocal() as session:
            stmt = select(func.count(Chunk.id))
            if only_missing:
                stmt = stmt.where(Chunk.embedding.is_(None))
            total = (await session.execute(stmt)).scalar_one()

            offset = 0
            while True:
                query = select(Chunk).order_by(Chunk.created_at).limit(batch_size).offset(offset)
                if only_missing:
                    query = select(Chunk).where(Chunk.embedding.is_(None)).limit(batch_size)
                    offset = 0  # the filter itself advances the cursor

                chunks = list((await session.execute(query)).scalars())
                if not chunks:
                    break

                vectors = await embedder.embed_documents([c.text for c in chunks])
                for chunk, vector in zip(chunks, vectors):
                    chunk.embedding = vector
                await session.commit()

                processed += len(chunks)
                if not only_missing:
                    offset += batch_size
                self.update_state(
                    state="PROGRESS", meta={"processed": processed, "total": total}
                )
                log.info("reindex progress", extra={"processed": processed, "total": total})

        return {"processed": processed}

    return _run(job())


@celery_app.task(name="app.workers.tasks.build_faiss_index_task")
def build_faiss_index_task() -> dict:
    """Export pgvector embeddings into a FAISS index for the offline profile."""

    async def job() -> dict:
        import uuid as _uuid
        from pathlib import Path

        import faiss
        import numpy as np

        from app.core.config import settings

        async with SessionLocal() as session:
            rows = list(
                (
                    await session.execute(
                        select(Chunk.id, Chunk.embedding).where(Chunk.embedding.isnot(None))
                    )
                ).all()
            )
        if not rows:
            return {"indexed": 0, "reason": "no embedded chunks"}

        ids = np.array([_uuid.UUID(str(cid)).bytes for cid, _ in rows], dtype="|S16")
        vectors = np.asarray([vec for _, vec in rows], dtype=np.float32)
        faiss.normalize_L2(vectors)

        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)

        out = Path(settings.FAISS_INDEX_PATH)
        out.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(out / "index.faiss"))
        np.save(out / "ids.npy", ids)
        return {"indexed": int(index.ntotal), "path": str(out)}

    return _run(job())


# ----------------------------------------------------------------- upkeep


@celery_app.task(name="app.workers.tasks.resolve_pending_crossrefs_task")
def resolve_pending_crossrefs_task(limit: int = 5000) -> dict:
    """Resolve cross-references whose target act was not yet ingested.

    Citations are extracted eagerly at ingest, so an act referenced before it was
    crawled leaves a dangling edge. This sweeps them up once the target exists.
    """

    async def job() -> dict:
        from app.services.rag.keyword import keyword_searcher

        resolved = 0
        async with SessionLocal() as session:
            pending = list(
                (
                    await session.execute(
                        select(CrossReference)
                        .where(
                            CrossReference.target_act_id.is_(None),
                            CrossReference.target_raw.isnot(None),
                        )
                        .limit(limit)
                    )
                ).scalars()
            )
            for edge in pending:
                act_name = (edge.target_raw or "").split("—")[0].strip()
                if not act_name:
                    continue
                matches = await keyword_searcher.find_acts_by_name(session, act_name, limit=1)
                if matches:
                    edge.target_act_id = matches[0].id
                    edge.confidence = 0.75
                    resolved += 1
            await session.commit()
        return {"pending": len(pending), "resolved": resolved}

    return _run(job())


@celery_app.task(name="app.workers.tasks.connector_selfcheck_task")
def connector_selfcheck_task() -> dict:
    """Verify lex.uz markup selectors still match. Alerts on failure."""

    async def job() -> dict:
        connector = get_connector("lexuz")
        async with connector:
            report = await connector.validate_selectors()
        if not report.get("ok"):
            log.error("lexuz selector check FAILED — ingestion will produce empty acts",
                      extra=report)
        else:
            log.info("lexuz selector check passed", extra=report)
        return report

    return _run(job())


@celery_app.task(name="app.workers.tasks.prune_logs_task")
def prune_logs_task(days: int = 365) -> dict:
    """Retention: query logs are kept for compliance, not forever."""

    async def job() -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with SessionLocal() as session:
            result = await session.execute(
                delete(QueryLog).where(QueryLog.created_at < cutoff)
            )
            await session.commit()
            return {"deleted": result.rowcount or 0, "cutoff": cutoff.isoformat()}

    return _run(job())


@celery_app.task(name="app.workers.tasks.corpus_stats_task")
def corpus_stats_task() -> dict:
    async def job() -> dict:
        async with SessionLocal() as session:
            acts = (await session.execute(select(func.count(LegalAct.id)))).scalar_one()
            chunks = (await session.execute(select(func.count(Chunk.id)))).scalar_one()
            embedded = (
                await session.execute(
                    select(func.count(Chunk.id)).where(Chunk.embedding.isnot(None))
                )
            ).scalar_one()
            alerts = (await session.execute(select(func.count(LegalAlert.id)))).scalar_one()
        return {"acts": acts, "chunks": chunks, "embedded": embedded, "alerts": alerts}

    return _run(job())
