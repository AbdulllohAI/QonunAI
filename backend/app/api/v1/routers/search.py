"""Search endpoints: hybrid retrieval and 'Ask by Article' mode."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import rate_limit
from app.db.session import get_session
from app.schemas.common import ArticleRequest, SearchHit, SearchRequest, SearchResponse
from app.services.lang.detect import detect_language
from app.services.rag.hybrid import hybrid_retriever
from app.services.rag.keyword import keyword_searcher
from app.services.reasoning import reasoning_engine

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse, dependencies=[Depends(rate_limit)])
async def search(payload: SearchRequest, session: AsyncSession = Depends(get_session)):
    """Raw hybrid retrieval — no LLM. Use this to inspect what the RAG layer
    actually finds, and as the backing endpoint for the corpus browser."""
    result = await hybrid_retriever.retrieve(
        session,
        payload.query,
        language=payload.language,
        top_k=payload.top_k,
        act_types=payload.act_types,
        act_ids=payload.act_ids,
        in_force_only=payload.in_force_only,
        expand_crossrefs=payload.expand_crossrefs,
    )
    return SearchResponse(
        query=result.query,
        detected_language=result.detected_language.value if result.detected_language else "",
        hits=[
            SearchHit(
                chunk_id=str(c.chunk_id),
                act_id=str(c.act_id),
                citation=c.citation,
                law_name=c.law_name,
                article_number=c.article_number,
                act_type=c.act_type.value,
                language=c.language.value,
                hierarchy_path=c.hierarchy_path,
                heading=c.heading,
                text=c.text,
                score=round(c.score, 4),
                dense_score=round(c.dense_score, 4),
                sparse_score=round(c.sparse_score, 4),
                rerank_score=round(c.rerank_score, 4) if c.rerank_score is not None else None,
                source_url=c.source_url,
                via_crossref_from=c.via_crossref_from,
            )
            for c in result.chunks
        ],
        dense_hits=result.dense_hits,
        sparse_hits=result.sparse_hits,
        crossref_hits=result.crossref_hits,
        took_ms=result.took_ms,
    )


@router.post("/article", dependencies=[Depends(rate_limit)])
async def by_article(payload: ArticleRequest, session: AsyncSession = Depends(get_session)):
    """'Ask by Article': fetch a named article and, optionally, explain it."""
    act_ids = [payload.act_id] if payload.act_id else None

    if act_ids is None and payload.act_name:
        acts = await keyword_searcher.find_acts_by_name(session, payload.act_name, limit=3)
        if not acts:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no act matched '{payload.act_name}'"
            )
        act_ids = [a.id for a in acts]

    language = payload.language
    rows = await keyword_searcher.by_article(
        session,
        [payload.article_number],
        act_ids=act_ids,
        languages=[language] if language else None,
        limit=20,
    )
    if not rows:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"article {payload.article_number} not found"
            + (f" in '{payload.act_name}'" if payload.act_name else ""),
        )

    articles = [
        {
            "chunk_id": str(chunk.id),
            "act_id": str(act.id),
            "law_name": chunk.law_name,
            "article_number": chunk.article_number,
            "act_type": chunk.act_type.value,
            "language": chunk.language.value,
            "hierarchy_path": chunk.hierarchy_path,
            "heading": chunk.heading,
            "text": chunk.text,
            "source_url": chunk.source_url or act.source_url,
            "citation": chunk.citation,
        }
        for chunk, act, _ in rows
    ]

    response: dict = {"article_number": payload.article_number, "articles": articles}

    if payload.explain:
        first = rows[0][0]
        question = (
            f"Explain {first.citation}. What does it require, when does it apply, and "
            f"what are its practical effects?"
        )
        answer = await reasoning_engine.answer(
            session,
            question,
            mode="by_article",
            language=language or detect_language(first.text),
            act_ids=[rows[0][1].id],
        )
        response["explanation"] = answer.to_dict()

    return response
