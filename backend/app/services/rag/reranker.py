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
                        kwargs = {
                            "device": settings.EMBEDDING_DEVICE,
                            "max_length": settings.RERANK_MAX_LENGTH,
                        }
                        if settings.RERANKER_TRUST_REMOTE_CODE:
                            # Executes code from the model repo. See the setting.
                            kwargs["trust_remote_code"] = True
                            log.warning(
                                "reranker_trusting_remote_code",
                                extra={"model": settings.RERANKER_MODEL},
                            )
                        self._model = await asyncio.to_thread(
                            CrossEncoder, settings.RERANKER_MODEL, **kwargs
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

    async def warm(self) -> bool:
        """Load the cross-encoder ahead of the first request.

        Left lazy, the ~2.3 GB load happens inside whichever user request
        arrives first, and that takes longer than Fly's proxy will wait — the
        request dies with a 502 rather than merely being slow. Returns whether
        the model is usable, so callers can log the outcome.
        """
        if not settings.RERANKER_ENABLED:
            return False
        if settings.RERANKER_BACKEND == "remote":
            # Nothing to load here; the model lives in the reranker service.
            # Probe it so a misconfigured URL surfaces at boot rather than on
            # the first user question.
            return await self._probe_remote()
        return await self._get_model() is not None

    async def _probe_remote(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                payload = (await client.get(f"{settings.RERANKER_URL}/health")).json()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "reranker_service_unreachable_using_fusion_order_only",
                extra={"url": settings.RERANKER_URL, "error": str(exc)[:200]},
            )
            return False
        if not payload.get("loaded"):
            log.error(
                "reranker_service_has_no_model",
                extra={"url": settings.RERANKER_URL, "error": payload.get("error")},
            )
            return False
        log.info("reranker_service_ready", extra={"model": payload.get("model")})
        return True

    async def _score_remote(self, query: str, passages: list[str]) -> list[float] | None:
        """Scores from the reranker service, or None meaning "keep your order".

        Every failure here is soft by design — the fused order already scores
        Recall@5 = 0.931, so a reranker that cannot answer in time is not worth
        failing a legal query over — but each one is logged at error level,
        because a pipeline stage that silently stops running is precisely the
        failure this codebase has already been bitten by.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=settings.RERANKER_TIMEOUT_S) as client:
                response = await client.post(
                    f"{settings.RERANKER_URL}/rerank",
                    json={"query": query, "passages": passages},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "rerank_service_call_failed",
                extra={"error": str(exc)[:200], "passages": len(passages)},
            )
            return None

        scores = payload.get("scores") or []
        if len(scores) != len(passages):
            log.error(
                "rerank_service_returned_wrong_length",
                extra={"expected": len(passages), "got": len(scores)},
            )
            return None
        return [float(s) for s in scores]

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        if not settings.RERANKER_ENABLED:
            return list(chunks)[:top_k]

        # Include the citation line: it tells the cross-encoder which law the
        # passage belongs to, which the passage body often omits.
        passages = [f"{c.citation}\n{c.heading or ''}\n{c.text}" for c in chunks]

        if settings.RERANKER_BACKEND == "remote":
            scores = await self._score_remote(query, passages)
            if scores is None:
                return list(chunks)[:top_k]
        else:
            model = await self._get_model()
            if model is None:
                return list(chunks)[:top_k]
            try:
                scores = await asyncio.to_thread(
                    model.predict,
                    [[query, passage] for passage in passages],
                    batch_size=settings.EMBEDDING_BATCH_SIZE,
                    show_progress_bar=False,
                )
            except Exception as exc:
                log.error("rerank_failed", extra={"error": str(exc)[:200]})
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
