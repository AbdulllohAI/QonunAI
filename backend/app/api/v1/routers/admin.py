"""Admin: ingestion control, connector health, audit log."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_admin
from app.db.models import ActType, IngestionRun, QueryLog, User
from app.db.session import get_session
from app.schemas.common import IngestRequest, SeedCsvRequest
from app.services.ingestion import csv_seed_loader, get_connector, ingestion_pipeline

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/ingest")
async def trigger_ingest(payload: IngestRequest, session: AsyncSession = Depends(get_session)):
    """Run a connector synchronously.

    For a full-corpus crawl use the Celery task instead — this endpoint holds the
    request open for the duration and will time out on anything large.
    """
    connector = get_connector(payload.connector)
    discover_kwargs: dict = {}
    if payload.connector == "lexuz":
        discover_kwargs = {
            "seeds": payload.seeds,
            "search_terms": payload.search_terms or [],
        }

    stats = await ingestion_pipeline.run_connector(
        session,
        connector,
        identifiers=payload.identifiers,
        languages=payload.languages,
        force=payload.force,
        limit=payload.limit,
        **discover_kwargs,
    )
    return stats.to_dict()


@router.post("/ingest/async")
async def trigger_ingest_async(payload: IngestRequest):
    """Queue the ingestion on Celery and return immediately."""
    from app.workers.tasks import ingest_connector_task

    task = ingest_connector_task.delay(
        connector=payload.connector,
        identifiers=payload.identifiers,
        languages=[lang.value for lang in payload.languages] if payload.languages else None,
        search_terms=payload.search_terms,
        seeds=payload.seeds,
        force=payload.force,
        limit=payload.limit,
    )
    return {"task_id": task.id, "status": "queued"}


@router.post("/seed-csv")
async def seed_csv(payload: SeedCsvRequest, session: AsyncSession = Depends(get_session)):
    """Load a pre-structured CSV corpus (fastest path to a working index)."""
    stats = await csv_seed_loader.load(
        session,
        payload.csv_path,
        short_name=payload.short_name,
        act_type=payload.act_type,
        language=payload.language,
        title=payload.title,
        source_url=payload.source_url,
    )
    if stats.errors and not stats.chunks_written:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, stats.errors[0])
    return stats.to_dict()


@router.get("/ingest/runs")
async def list_runs(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
):
    rows = await session.execute(
        select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(limit)
    )
    return [
        {
            "id": str(run.id),
            "connector": run.connector,
            "status": run.status,
            "acts_seen": run.acts_seen,
            "acts_upserted": run.acts_upserted,
            "chunks_written": run.chunks_written,
            "errors": run.errors,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        }
        for run in rows.scalars()
    ]


@router.get("/connectors/health")
async def connector_health():
    """Reachability plus, for lex.uz, whether the HTML selectors still match.

    A silent selector break is the most likely way this system degrades, so it
    gets an explicit probe rather than a bare ping.
    """
    results: dict[str, dict] = {}
    for name in ("lexuz", "norma", "gov_opendata"):
        connector = get_connector(name)
        try:
            async with connector:
                entry: dict = {"reachable": await connector.health()}
                if name == "lexuz":
                    entry["selectors"] = await connector.validate_selectors()
                results[name] = entry
        except Exception as exc:
            results[name] = {"reachable": False, "error": str(exc)}
    return results


@router.get("/logs")
async def query_logs(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=1000),
    unanswered_only: bool = False,
):
    """Interaction audit log — every query is recorded for compliance."""
    stmt = select(QueryLog).order_by(QueryLog.created_at.desc()).limit(limit)
    if unanswered_only:
        stmt = stmt.where(QueryLog.answered.is_(False))
    rows = await session.execute(stmt)
    return [
        {
            "id": str(entry.id),
            "request_id": entry.request_id,
            "user_id": str(entry.user_id) if entry.user_id else None,
            "mode": entry.mode,
            "query": entry.query,
            "detected_language": entry.detected_language,
            "citations": entry.citations,
            "answered": entry.answered,
            "refusal_reason": entry.refusal_reason,
            "provider": entry.provider,
            "model": entry.model,
            "latency_ms": entry.latency_ms,
            "created_at": entry.created_at.isoformat(),
        }
        for entry in rows.scalars()
    ]


@router.get("/logs/stats")
async def log_stats(session: AsyncSession = Depends(get_session)):
    totals = await session.execute(
        select(
            func.count(QueryLog.id),
            func.count(QueryLog.id).filter(QueryLog.answered.is_(False)),
            func.avg(QueryLog.latency_ms),
        )
    )
    total, unanswered, avg_latency = totals.one()
    by_language = await session.execute(
        select(QueryLog.detected_language, func.count(QueryLog.id)).group_by(
            QueryLog.detected_language
        )
    )
    by_reason = await session.execute(
        select(QueryLog.refusal_reason, func.count(QueryLog.id))
        .where(QueryLog.refusal_reason.isnot(None))
        .group_by(QueryLog.refusal_reason)
    )
    return {
        "total_queries": total or 0,
        "unanswered": unanswered or 0,
        "answer_rate": round(1 - (unanswered or 0) / total, 4) if total else None,
        "avg_latency_ms": round(float(avg_latency), 1) if avg_latency else None,
        "by_language": {k or "unknown": v for k, v in by_language.all()},
        "refusal_reasons": {k: v for k, v in by_reason.all()},
    }


@router.post("/reindex")
async def reindex():
    """Re-embed the whole corpus (after an embedding-model change)."""
    from app.workers.tasks import reindex_corpus_task

    task = reindex_corpus_task.delay()
    return {"task_id": task.id, "status": "queued"}
