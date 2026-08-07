"""Shared retrieval value objects."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from app.db.models import ActType, Language


@dataclass(slots=True)
class RetrievedChunk:
    """One retrieval hit, carrying everything needed to cite it."""

    chunk_id: uuid.UUID
    act_id: uuid.UUID
    text: str
    law_name: str
    article_number: str | None
    act_type: ActType
    language: Language
    hierarchy_path: str
    heading: str | None = None
    jurisdiction: str = "Uzbekistan"
    date_of_adoption: date | None = None
    last_updated: date | None = None
    source_url: str | None = None
    act_status: str | None = None
    """Whether the act is still in force — a citation card must not imply that
    a repealed provision is current law."""

    dense_score: float = 0.0
    sparse_score: float = 0.0
    rerank_score: float | None = None
    fused_score: float = 0.0
    dense_rank: int | None = None
    sparse_rank: int | None = None

    # Set when the chunk was pulled in by cross-reference expansion rather than
    # by matching the query directly — the prompt labels these differently.
    via_crossref_from: str | None = None

    @property
    def citation(self) -> str:
        if self.article_number:
            return f"Article {self.article_number} of {self.law_name}"
        return self.law_name

    @property
    def score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.fused_score

    @property
    def precedence(self) -> int:
        return self.act_type.precedence


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    query: str = ""
    detected_language: Language | None = None
    dense_hits: int = 0
    sparse_hits: int = 0
    crossref_hits: int = 0
    took_ms: int = 0

    degraded_branches: dict[str, str] = field(default_factory=dict)
    """Branches that raised, mapped to why. A branch that is broken returns the
    same empty list as a branch that simply matched nothing, so without this
    the two are indistinguishable — which is how a completely dead dense
    branch served production traffic unnoticed."""

    def top(self, n: int) -> list[RetrievedChunk]:
        return self.chunks[:n]

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded_branches)

    @property
    def is_empty(self) -> bool:
        return not self.chunks
