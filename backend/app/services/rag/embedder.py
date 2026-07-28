"""Embedding generation with a Redis-backed cache.

Default model is BAAI/bge-m3: multilingual (handles uz/ru/en in one space),
8k context, and strong on retrieval — which matters because Uzbek is poorly
covered by most English-first embedding models.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Sequence

import numpy as np
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import get_logger
from app.services.lang.translit import normalize

log = get_logger(__name__)

# bge-m3 was trained with an instruction prefix on the query side only.
QUERY_PREFIX = "Represent this legal question for retrieving relevant statutes: "

_CACHE_TTL_S = 60 * 60 * 24 * 30


class Embedder:
    """Thread-safe, lazily-loaded sentence-transformer wrapper."""

    def __init__(self) -> None:
        self._model = None
        self._lock = asyncio.Lock()
        self._redis: aioredis.Redis | None = None

    # -------------------------------------------------------------- lifecycle
    async def _get_model(self):
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    log.info("loading embedding model", extra={"model": settings.EMBEDDING_MODEL})
                    self._model = await asyncio.to_thread(
                        SentenceTransformer,
                        settings.EMBEDDING_MODEL,
                        device=settings.EMBEDDING_DEVICE,
                    )
        return self._model

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(str(settings.REDIS_DSN), decode_responses=False)
        return self._redis

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    # ---------------------------------------------------------------- caching
    @staticmethod
    def _cache_key(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"emb:{settings.EMBEDDING_MODEL}:{digest}"

    async def _cache_get_many(self, texts: Sequence[str]) -> list[np.ndarray | None]:
        try:
            r = await self._get_redis()
            raw = await r.mget([self._cache_key(t) for t in texts])
        except Exception as exc:  # cache is strictly an optimisation
            log.warning("embedding cache read failed", extra={"error": str(exc)})
            return [None] * len(texts)
        return [
            np.frombuffer(b, dtype=np.float32) if b else None
            for b in raw
        ]

    async def _cache_set_many(self, texts: Sequence[str], vectors: Sequence[np.ndarray]) -> None:
        try:
            r = await self._get_redis()
            pipe = r.pipeline()
            for text, vec in zip(texts, vectors):
                pipe.setex(self._cache_key(text), _CACHE_TTL_S, vec.astype(np.float32).tobytes())
            await pipe.execute()
        except Exception as exc:
            log.warning("embedding cache write failed", extra={"error": str(exc)})

    # ---------------------------------------------------------------- encoding
    async def embed_texts(
        self, texts: Sequence[str], *, use_cache: bool = True
    ) -> list[list[float]]:
        if not texts:
            return []
        cleaned = [normalize(t) for t in texts]

        cached = await self._cache_get_many(cleaned) if use_cache else [None] * len(cleaned)
        missing_idx = [i for i, v in enumerate(cached) if v is None]

        if missing_idx:
            model = await self._get_model()
            to_encode = [cleaned[i] for i in missing_idx]
            encoded = await asyncio.to_thread(
                model.encode,
                to_encode,
                batch_size=settings.EMBEDDING_BATCH_SIZE,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            for slot, vec in zip(missing_idx, encoded):
                cached[slot] = np.asarray(vec, dtype=np.float32)
            if use_cache:
                await self._cache_set_many(to_encode, [cached[i] for i in missing_idx])

        return [v.tolist() for v in cached]  # type: ignore[union-attr]

    async def embed_query(self, query: str) -> list[float]:
        vectors = await self.embed_texts([QUERY_PREFIX + query])
        return vectors[0]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self.embed_texts(texts)


embedder = Embedder()
