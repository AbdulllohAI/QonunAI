"""Corpus browsing: acts, their structural trees, and change timelines."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActStatus, ActType, Language, LegalAct, LegalNode
from app.db.session import get_session
from app.schemas.common import ActOut, NodeOut
from app.services.alerts.service import alert_service

router = APIRouter(prefix="/laws", tags=["laws"])


@router.get("", response_model=list[ActOut])
async def list_acts(
    session: AsyncSession = Depends(get_session),
    act_type: ActType | None = None,
    status_filter: ActStatus | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(LegalAct).order_by(LegalAct.act_type, LegalAct.short_name)
    if act_type:
        stmt = stmt.where(LegalAct.act_type == act_type)
    if status_filter:
        stmt = stmt.where(LegalAct.status == status_filter)
    if q:
        needle = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(LegalAct.short_name).like(needle),
                func.lower(LegalAct.title_uz).like(needle),
                func.lower(LegalAct.title_ru).like(needle),
                func.lower(LegalAct.title_en).like(needle),
            )
        )
    rows = await session.execute(stmt.limit(limit).offset(offset))
    return list(rows.scalars())


@router.get("/articles")
async def article_index(session: AsyncSession = Depends(get_session)):
    """Every article number and title in the corpus, grouped by act.

    Exists so the benchmark's gold labels can be checked against the corpus
    they claim to describe (`benchmarks/audit_gold.py`). A benchmark can be
    wrong in ways that look exactly like the system being wrong: four items
    named a gold article whose title was shared by another article in the same
    act, and retrieval was marked incorrect for returning an equally correct
    provision.

    Public because the content is published legislation, and the index is what
    makes the benchmark's provenance claims auditable by someone who does not
    have database access.
    """
    from app.db.models import Chunk

    rows = await session.execute(
        select(
            Chunk.act_id,
            Chunk.article_number,
            Chunk.heading,
            Chunk.language,
            Chunk.law_name,
        )
        .where(Chunk.article_number.isnot(None))
        .where(Chunk.heading.isnot(None))
        .distinct()
    )
    return {
        "rows": [
            {
                "act_id": str(act_id),
                "article_number": article_number,
                "heading": heading,
                "language": language.value if language else None,
                "law_name": law_name,
            }
            for act_id, article_number, heading, language, law_name in rows
        ]
    }


@router.get("/stats")
async def corpus_stats(session: AsyncSession = Depends(get_session)):
    from app.db.models import Chunk

    by_type = await session.execute(
        select(LegalAct.act_type, func.count(LegalAct.id)).group_by(LegalAct.act_type)
    )
    by_language = await session.execute(
        select(Chunk.language, func.count(Chunk.id)).group_by(Chunk.language)
    )
    totals = await session.execute(
        select(
            func.count(func.distinct(LegalAct.id)),
            func.count(func.distinct(LegalNode.id)),
        ).select_from(LegalAct).outerjoin(LegalNode, LegalNode.act_id == LegalAct.id)
    )
    chunk_total = await session.execute(select(func.count(Chunk.id)))
    act_count, node_count = totals.one()

    return {
        "acts": act_count,
        "nodes": node_count,
        "chunks": chunk_total.scalar_one(),
        "acts_by_type": {t.value: c for t, c in by_type.all()},
        "chunks_by_language": {lang.value: c for lang, c in by_language.all()},
    }


@router.get("/{act_id}", response_model=ActOut)
async def get_act(act_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    act = (
        await session.execute(select(LegalAct).where(LegalAct.id == act_id))
    ).scalar_one_or_none()
    if act is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "act not found")
    return act


@router.get("/{act_id}/tree", response_model=list[NodeOut])
async def get_tree(
    act_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    language: Language = Language.UZ_LATN,
    include_body: bool = False,
):
    """Full structural tree: Qism → Bo'lim → Bob → Modda → Band."""
    rows = list(
        (
            await session.execute(
                select(LegalNode)
                .where(LegalNode.act_id == act_id, LegalNode.language == language)
                .order_by(LegalNode.path, LegalNode.ordinal)
            )
        ).scalars()
    )
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no structure found for this act in {language.value}",
        )

    by_id: dict[uuid.UUID, NodeOut] = {}
    roots: list[NodeOut] = []
    for node in rows:
        by_id[node.id] = NodeOut(
            id=node.id,
            node_type=node.node_type.value,
            number=node.number,
            article_number=node.article_number,
            heading=node.heading,
            body=node.body if include_body else None,
            path=node.path,
            children=[],
        )
    for node in rows:
        item = by_id[node.id]
        parent = by_id.get(node.parent_id) if node.parent_id else None
        (parent.children if parent else roots).append(item)
    return roots


@router.get("/{act_id}/articles/{article_number}/timeline")
async def article_timeline(
    act_id: uuid.UUID,
    article_number: str,
    session: AsyncSession = Depends(get_session),
    language: Language = Language.UZ_LATN,
    include_body: bool = False,
):
    """Version history for one article — the 'what changed and when' view."""
    entries = await alert_service.article_timeline(
        session, act_id, article_number, language=language, include_body=include_body
    )
    if not entries:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no version history for this article")
    return {
        "act_id": str(act_id),
        "article_number": article_number,
        "language": language.value,
        "versions": [e.to_dict(include_body=include_body) for e in entries],
    }


@router.get("/versions/diff")
async def diff_versions(
    old_version_id: uuid.UUID,
    new_version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    result = await alert_service.diff_versions(session, old_version_id, new_version_id)
    if "error" in result:
        raise HTTPException(status.HTTP_404_NOT_FOUND, result["error"])
    return result
