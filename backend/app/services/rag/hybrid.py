"""Hybrid retrieval: dense + sparse + exact-article, fused with RRF, then reranked.

Pipeline
--------
    query
      ├─ language detection + script normalisation
      ├─ article-number / act-name extraction  ──► exact article fetch (pinned)
      ├─ dense search   (bge-m3 → pgvector HNSW, cosine)
      ├─ sparse search  (per-language tsvector, ts_rank_cd)
      ├─ Reciprocal Rank Fusion over the two ranked lists
      ├─ cross-encoder rerank of the fused candidates
      ├─ cross-reference expansion (pull in articles the hits point to)
      └─ precedence-aware final ordering

Why RRF rather than a weighted score blend: cosine similarity and ts_rank_cd
live on incomparable scales, and ts_rank_cd's range shifts with document length
and query term count. Tuning a weight per corpus is a maintenance burden that
RRF sidesteps by consuming only ranks.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ActType, Chunk, Language, LegalAct
from app.db.session import SessionLocal
from app.services.lang.detect import detect_language, target_search_languages
from app.services.rag.crossref import expand_cross_references
from app.services.rag.embedder import embedder
from app.services.rag.keyword import (
    extract_article_numbers,
    infer_act_types,
    keyword_searcher,
)
from app.services.rag.reranker import reranker
from app.services.rag.types import RetrievalResult, RetrievedChunk
from app.services.rag.vector_store import vector_store

log = get_logger(__name__)


def _to_retrieved(chunk: Chunk, act: LegalAct, *, sparse: float = 0.0) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.id,
        act_id=chunk.act_id,
        text=chunk.text,
        law_name=chunk.law_name,
        article_number=chunk.article_number,
        act_type=chunk.act_type,
        language=chunk.language,
        hierarchy_path=chunk.hierarchy_path,
        heading=chunk.heading,
        jurisdiction=chunk.jurisdiction,
        date_of_adoption=chunk.date_of_adoption,
        last_updated=chunk.last_updated,
        source_url=chunk.source_url or act.source_url,
        sparse_score=sparse,
    )


class HybridRetriever:
    async def retrieve(
        self,
        session: AsyncSession,
        query: str,
        *,
        language: Language | None = None,
        top_k: int | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        in_force_only: bool = True,
        expand_crossrefs: bool = True,
        cross_language: bool = True,
    ) -> RetrievalResult:
        started = time.perf_counter()
        top_k = top_k or settings.RETRIEVAL_TOP_K_FINAL
        lang = language or detect_language(query)
        search_langs = target_search_languages(lang) if cross_language else [lang]

        # Query-derived filters. An explicit "Civil Code" in the question is a
        # much stronger filter than anything the embedding will encode.
        inferred_types = list(act_types) if act_types else infer_act_types(query)
        article_numbers = extract_article_numbers(query)

        # Each branch below opens its own session rather than sharing the one
        # passed in: SQLAlchemy's AsyncSession is not safe for concurrent use,
        # and running all three under asyncio.gather on one shared session
        # raises "concurrent operations are not permitted" on whichever branch
        # loses the race — intermittently, so it's easy to miss in testing.
        # The passed-in `session` is still used later in this method, but only
        # after gather() has fully completed, which is sequential and safe.
        dense_task = self._run_isolated(
            self._dense, query, search_langs, inferred_types or None, act_ids, in_force_only
        )
        sparse_task = self._run_isolated(
            self._sparse, query, lang, search_langs, inferred_types or None, act_ids, in_force_only
        )
        exact_task = self._run_isolated_exact(
            article_numbers, inferred_types or None, act_ids, search_langs
        )

        dense_hits, sparse_hits, exact_hits = await asyncio.gather(
            dense_task, sparse_task, exact_task, return_exceptions=True
        )
        dense_hits = _unwrap(dense_hits, "dense")
        sparse_hits = _unwrap(sparse_hits, "sparse")
        exact_hits = _unwrap(exact_hits, "exact-article")

        fused = self._fuse(dense_hits, sparse_hits)

        # Exact article matches are pinned to the front — if the user asked for
        # Article 54 by name, Article 54 belongs in the context regardless of
        # what the embeddings ranked.
        pinned = self._pin_exact(exact_hits, fused)

        candidates = pinned + [c for c in fused if c.chunk_id not in {p.chunk_id for p in pinned}]
        candidates = self._dedupe_by_article(candidates)
        # Already sorted best-first (pinned exact matches, then descending
        # fused_score) — capping here only drops candidates RRF itself ranked
        # lowest, before paying the cross-encoder's per-candidate cost on them.
        candidates = candidates[: settings.RERANK_CANDIDATE_CAP]

        reranked = await reranker.rerank(query, candidates, top_k)
        reranked = [c for c in reranked if c.score >= settings.MIN_RELEVANCE_SCORE] or reranked[:3]

        # Re-pin: reranking can bury an explicitly requested article.
        reranked = self._pin_exact(pinned, reranked) + [
            c for c in reranked if c.chunk_id not in {p.chunk_id for p in pinned}
        ]

        crossref_chunks: list[RetrievedChunk] = []
        if expand_crossrefs and reranked:
            crossref_chunks = await expand_cross_references(
                session,
                reranked[: min(5, len(reranked))],
                limit=settings.CROSSREF_EXPANSION_LIMIT,
                languages=search_langs,
                exclude={c.chunk_id for c in reranked},
            )

        final = self._order_final(reranked[:top_k]) + crossref_chunks

        return RetrievalResult(
            chunks=final,
            query=query,
            detected_language=lang,
            dense_hits=len(dense_hits),
            sparse_hits=len(sparse_hits),
            crossref_hits=len(crossref_chunks),
            took_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------- session isolation
    @staticmethod
    async def _run_isolated(branch, *args):
        """Run a `_dense`/`_sparse`-shaped branch on its own session."""
        async with SessionLocal() as branch_session:
            return await branch(branch_session, *args)

    @staticmethod
    async def _run_isolated_exact(
        article_numbers: list[str],
        act_types: Sequence[ActType] | None,
        act_ids: Sequence[uuid.UUID] | None,
        languages: Sequence[Language],
    ):
        async with SessionLocal() as branch_session:
            return await keyword_searcher.by_article(
                branch_session,
                article_numbers,
                act_types=act_types,
                act_ids=act_ids,
                languages=languages,
            )

    # ------------------------------------------------------------- retrievers
    async def _dense(
        self,
        session: AsyncSession,
        query: str,
        languages: Sequence[Language],
        act_types: Sequence[ActType] | None,
        act_ids: Sequence[uuid.UUID] | None,
        in_force_only: bool,
    ) -> list[RetrievedChunk]:
        vector = await embedder.embed_query(query)
        return await vector_store.search(
            session,
            vector,
            top_k=settings.RETRIEVAL_TOP_K_DENSE,
            languages=languages,
            act_types=act_types,
            act_ids=act_ids,
            in_force_only=in_force_only,
        )

    async def _sparse(
        self,
        session: AsyncSession,
        query: str,
        language: Language,
        languages: Sequence[Language],
        act_types: Sequence[ActType] | None,
        act_ids: Sequence[uuid.UUID] | None,
        in_force_only: bool,
    ) -> list[RetrievedChunk]:
        rows = await keyword_searcher.search(
            session,
            query,
            top_k=settings.RETRIEVAL_TOP_K_SPARSE,
            language=language,
            languages=languages,
            act_types=act_types,
            act_ids=act_ids,
            in_force_only=in_force_only,
        )
        return [_to_retrieved(c, a, sparse=score) for c, a, score in rows]

    # ------------------------------------------------------------------ fusion
    @staticmethod
    def _fuse(
        dense: list[RetrievedChunk], sparse: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        """Reciprocal Rank Fusion: score = Σ 1/(k + rank)."""
        k = settings.RRF_K
        merged: dict[uuid.UUID, RetrievedChunk] = {}
        scores: dict[uuid.UUID, float] = defaultdict(float)

        for rank, chunk in enumerate(dense, start=1):
            merged[chunk.chunk_id] = chunk
            chunk.dense_rank = rank
            scores[chunk.chunk_id] += 1.0 / (k + rank)

        for rank, chunk in enumerate(sparse, start=1):
            existing = merged.get(chunk.chunk_id)
            if existing is not None:
                existing.sparse_score = chunk.sparse_score
                existing.sparse_rank = rank
            else:
                chunk.sparse_rank = rank
                merged[chunk.chunk_id] = chunk
            scores[chunk.chunk_id] += 1.0 / (k + rank)

        for chunk_id, score in scores.items():
            merged[chunk_id].fused_score = score

        return sorted(merged.values(), key=lambda c: c.fused_score, reverse=True)

    @staticmethod
    def _pin_exact(
        exact: Sequence, existing: Sequence[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        """Normalise exact-article rows into RetrievedChunk and give them max score."""
        pinned: list[RetrievedChunk] = []
        seen: set[uuid.UUID] = set()
        for item in exact:
            if isinstance(item, RetrievedChunk):
                chunk = item
            else:
                chunk_row, act, _ = item
                chunk = _to_retrieved(chunk_row, act, sparse=1.0)
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            chunk.fused_score = max(chunk.fused_score, 1.0)
            if chunk.rerank_score is not None:
                chunk.rerank_score = max(chunk.rerank_score, 0.99)
            pinned.append(chunk)
        return pinned

    @staticmethod
    def _dedupe_by_article(chunks: Sequence[RetrievedChunk]) -> list[RetrievedChunk]:
        """Keep the best chunk per (act, article, language).

        The same article often appears in several chunks when it is long. Feeding
        all of them wastes context on near-duplicates; the reranker sees the best
        one and the context builder can re-expand the full article later.
        """
        best: dict[tuple[uuid.UUID, str | None, Language], RetrievedChunk] = {}
        order: list[tuple[uuid.UUID, str | None, Language]] = []
        for chunk in chunks:
            key = (chunk.act_id, chunk.article_number, chunk.language)
            if key not in best:
                best[key] = chunk
                order.append(key)
            elif chunk.fused_score > best[key].fused_score:
                best[key] = chunk
        return [best[k] for k in order]

    @staticmethod
    def _order_final(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Relevance first, legal force as the tiebreaker.

        A Constitution article and a ministerial act that score the same should
        not be presented in arbitrary order — the higher-force norm goes first so
        the reasoning engine reads it first.
        """
        return sorted(
            chunks,
            key=lambda c: (round(c.score, 3), c.precedence),
            reverse=True,
        )


def _unwrap(result, label: str) -> list:
    if isinstance(result, BaseException):
        log.warning("retrieval branch failed", extra={"branch": label, "error": str(result)})
        return []
    return result


hybrid_retriever = HybridRetriever()
