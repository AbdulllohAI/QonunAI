"""Vector search backends.

pgvector is the production default: the vectors live beside the metadata, so a
filtered search ("only acts in force, only the Criminal Code") is one SQL query
with no cross-store join. FAISS and Chroma backends exist for offline/air-gapped
deployments where Postgres isn't available.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import VectorBackend, settings
from app.core.logging import get_logger
from app.db.models import ActStatus, ActType, Chunk, LegalAct, Language
from app.services.ingestion.anchors import build_deep_link
from app.services.rag.types import RetrievedChunk

log = get_logger(__name__)


def _row_to_chunk(chunk: Chunk, act: LegalAct, distance: float) -> RetrievedChunk:
    # pgvector cosine distance is in [0, 2]; convert to a [0, 1] similarity.
    similarity = max(0.0, 1.0 - float(distance))
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
        source_url=build_deep_link(chunk.source_url or act.source_url, chunk.lexuz_anchor_id),
        dense_score=similarity,
    )


class VectorStore(ABC):
    @abstractmethod
    async def search(
        self,
        session: AsyncSession,
        query_vector: Sequence[float],
        *,
        top_k: int,
        languages: Sequence[Language] | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        in_force_only: bool = True,
    ) -> list[RetrievedChunk]:
        ...


class PgVectorStore(VectorStore):
    """HNSW-indexed cosine search with metadata filters pushed into SQL."""

    async def search(
        self,
        session: AsyncSession,
        query_vector: Sequence[float],
        *,
        top_k: int,
        languages: Sequence[Language] | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        in_force_only: bool = True,
    ) -> list[RetrievedChunk]:
        distance = Chunk.embedding.cosine_distance(list(query_vector)).label("distance")
        stmt: Select = (
            select(Chunk, LegalAct, distance)
            .join(LegalAct, LegalAct.id == Chunk.act_id)
            .where(Chunk.embedding.isnot(None))
        )
        stmt = _apply_filters(stmt, languages, act_types, act_ids, in_force_only)
        stmt = stmt.order_by(distance).limit(top_k)

        rows = (await session.execute(stmt)).all()
        return [_row_to_chunk(chunk, act, dist) for chunk, act, dist in rows]


class FaissVectorStore(VectorStore):
    """Flat IP index over normalised vectors (== cosine). Metadata filters are
    applied post-hoc against Postgres, so it over-fetches to compensate."""

    def __init__(self, index_path: str | None = None) -> None:
        self.index_path = Path(index_path or settings.FAISS_INDEX_PATH)
        self._index = None
        self._ids: list[uuid.UUID] = []

    def _load(self):
        if self._index is not None:
            return self._index
        import faiss  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        idx_file = self.index_path / "index.faiss"
        ids_file = self.index_path / "ids.npy"
        if not idx_file.exists():
            raise FileNotFoundError(
                f"FAISS index missing at {idx_file}. Run `python -m app.workers.build_faiss`."
            )
        self._index = faiss.read_index(str(idx_file))
        self._ids = [uuid.UUID(bytes=b.tobytes()) for b in np.load(ids_file)]
        return self._index

    async def search(
        self,
        session: AsyncSession,
        query_vector: Sequence[float],
        *,
        top_k: int,
        languages: Sequence[Language] | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        in_force_only: bool = True,
    ) -> list[RetrievedChunk]:
        import numpy as np  # noqa: PLC0415

        index = self._load()
        # Over-fetch: filters are applied after the ANN search.
        fetch = top_k * 5 if (languages or act_types or act_ids) else top_k
        scores, positions = index.search(
            np.asarray([query_vector], dtype=np.float32), min(fetch, index.ntotal)
        )

        hit_ids = [self._ids[p] for p in positions[0] if p >= 0]
        if not hit_ids:
            return []
        score_by_id = {self._ids[p]: float(s) for p, s in zip(positions[0], scores[0]) if p >= 0}

        stmt = (
            select(Chunk, LegalAct)
            .join(LegalAct, LegalAct.id == Chunk.act_id)
            .where(Chunk.id.in_(hit_ids))
        )
        stmt = _apply_filters(stmt, languages, act_types, act_ids, in_force_only)
        rows = (await session.execute(stmt)).all()

        results = [
            _row_to_chunk(chunk, act, 1.0 - score_by_id.get(chunk.id, 0.0))
            for chunk, act in rows
        ]
        results.sort(key=lambda c: c.dense_score, reverse=True)
        return results[:top_k]


class ChromaVectorStore(VectorStore):
    """Chroma-backed variant for local prototyping."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or settings.CHROMA_PATH
        self._collection = None

    def _get_collection(self):
        if self._collection is None:
            import chromadb  # noqa: PLC0415

            client = chromadb.PersistentClient(path=self.path)
            self._collection = client.get_or_create_collection(
                "uzlex_chunks", metadata={"hnsw:space": "cosine"}
            )
        return self._collection

    async def search(
        self,
        session: AsyncSession,
        query_vector: Sequence[float],
        *,
        top_k: int,
        languages: Sequence[Language] | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        in_force_only: bool = True,
    ) -> list[RetrievedChunk]:
        where: dict = {}
        if languages:
            where["language"] = {"$in": [lang.value for lang in languages]}
        if act_types:
            where["act_type"] = {"$in": [a.value for a in act_types]}

        res = self._get_collection().query(
            query_embeddings=[list(query_vector)],
            n_results=top_k,
            where=where or None,
        )
        ids = [uuid.UUID(i) for i in (res.get("ids") or [[]])[0]]
        distances = (res.get("distances") or [[]])[0]
        if not ids:
            return []

        dist_by_id = dict(zip(ids, distances))
        stmt = (
            select(Chunk, LegalAct)
            .join(LegalAct, LegalAct.id == Chunk.act_id)
            .where(Chunk.id.in_(ids))
        )
        stmt = _apply_filters(stmt, None, None, act_ids, in_force_only)
        rows = (await session.execute(stmt)).all()
        out = [_row_to_chunk(chunk, act, dist_by_id.get(chunk.id, 1.0)) for chunk, act in rows]
        out.sort(key=lambda c: c.dense_score, reverse=True)
        return out


def _apply_filters(
    stmt: Select,
    languages: Sequence[Language] | None,
    act_types: Sequence[ActType] | None,
    act_ids: Sequence[uuid.UUID] | None,
    in_force_only: bool,
) -> Select:
    if languages:
        stmt = stmt.where(Chunk.language.in_(list(languages)))
    if act_types:
        stmt = stmt.where(Chunk.act_type.in_(list(act_types)))
    if act_ids:
        stmt = stmt.where(Chunk.act_id.in_(list(act_ids)))
    if in_force_only:
        stmt = stmt.where(
            LegalAct.status.in_([ActStatus.IN_FORCE, ActStatus.AMENDED])
        )
    return stmt


def get_vector_store() -> VectorStore:
    match settings.VECTOR_BACKEND:
        case VectorBackend.FAISS:
            return FaissVectorStore()
        case VectorBackend.CHROMA:
            return ChromaVectorStore()
        case _:
            return PgVectorStore()


vector_store = get_vector_store()
