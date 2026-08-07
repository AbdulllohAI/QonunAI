"""Cross-encoder reranking.

Fusion gets the right ~40 candidates into the room; the cross-encoder decides
which 12 actually answer the question. This matters more in legal retrieval than
in general QA because statutes are lexically similar to one another — dozens of
articles mention "shartnoma" — and only a joint query/passage encoder separates
"the article about contract *termination*" from "the article about contract
*form*".
"""
from __future__ import annotations

import asyncio
from typing import Sequence

from app.core.config import settings
from app.core.logging import get_logger
from app.services.rag.types import RetrievedChunk

log = get_logger(__name__)


class Reranker:
    def __init__(self) -> None:
        self._model = None
        self._lock = asyncio.Lock()
        self._unavailable = False

    async def _get_model(self):
        if self._model is None and not self._unavailable:
            async with self._lock:
                if self._model is None and not self._unavailable:
                    try:
                        from sentence_transformers import CrossEncoder

                        log.info("loading reranker", extra={"model": settings.RERANKER_MODEL})
                        self._model = await asyncio.to_thread(
                            CrossEncoder,
                            settings.RERANKER_MODEL,
                            device=settings.EMBEDDING_DEVICE,
                            max_length=1024,
                        )
                    except Exception as exc:
                        # Degrade to fusion-only ranking rather than failing the
                        # query -- but say so at error level. This warning sat in
                        # production logs while the cross-encoder never once ran,
                        # and the ranking it was supposed to provide was simply
                        # absent from every answer.
                        log.error(
                            "reranker_unavailable_using_fusion_order_only",
                            extra={"error": str(exc), "model": settings.RERANKER_MODEL},
                        )
                        self._unavailable = True
        return self._model

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        if not settings.RERANKER_ENABLED:
            return list(chunks)[:top_k]

        model = await self._get_model()
        if model is None:
            return list(chunks)[:top_k]

        # Include the citation line: it tells the cross-encoder which law the
        # passage belongs to, which the passage body often omits.
        pairs = [[query, f"{c.citation}\n{c.heading or ''}\n{c.text}"] for c in chunks]
        try:
            scores = await asyncio.to_thread(
                model.predict, pairs, batch_size=settings.EMBEDDING_BATCH_SIZE, show_progress_bar=False
            )
        except Exception as exc:
            log.warning("rerank failed", extra={"error": str(exc)})
            return list(chunks)[:top_k]

        for chunk, score in zip(chunks, scores):
            chunk.rerank_score = _sigmoid(float(score))

        ordered = sorted(chunks, key=lambda c: c.rerank_score or 0.0, reverse=True)
        return ordered[:top_k]


def _sigmoid(x: float) -> float:
    import math

    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


reranker = Reranker()
