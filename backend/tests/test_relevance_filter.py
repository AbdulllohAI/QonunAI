"""The relevance threshold must not apply to fusion scores.

MIN_RELEVANCE_SCORE is calibrated against the cross-encoder's sigmoid output.
RetrievedChunk.score falls back to the RRF fused score when no reranker ran,
and RRF scores are ~1/(60+rank) — around 0.016 at rank 1, never above ~0.065.
Against a 0.25 threshold that means *every* candidate is discarded on *every*
query, and the `or reranked[:3]` fallback silently caps results at three.

In production this made an Uzbek query return four results total and never
reach the governing article, while looking like a normal retrieval.
"""
from __future__ import annotations

from app.core.config import settings


def _apply(reranked, top_k):
    """The ranking-cutoff logic from HybridRetriever.retrieve."""
    if any(c.rerank_score is not None for c in reranked):
        return [c for c in reranked if c.score >= settings.MIN_RELEVANCE_SCORE] or reranked[:3]
    return reranked[:top_k]


class FakeChunk:
    def __init__(self, fused=0.0, rerank=None):
        self.fused_score = fused
        self.rerank_score = rerank

    @property
    def score(self):
        return self.rerank_score if self.rerank_score is not None else self.fused_score


def test_rrf_scores_are_never_above_the_threshold():
    """Establishes the premise: this is why the bug was total, not partial."""
    best_possible_rrf = 1.0 / (60 + 1)
    assert best_possible_rrf < settings.MIN_RELEVANCE_SCORE


def test_fusion_only_results_are_not_thresholded():
    chunks = [FakeChunk(fused=1.0 / (60 + r)) for r in range(1, 13)]
    assert len(_apply(chunks, top_k=12)) == 12, "must not collapse to the 3-result fallback"


def test_fusion_only_respects_top_k():
    chunks = [FakeChunk(fused=1.0 / (60 + r)) for r in range(1, 31)]
    assert len(_apply(chunks, top_k=12)) == 12


def test_reranked_results_are_still_thresholded():
    """The filter must keep working where it was designed to work."""
    chunks = [FakeChunk(rerank=0.9), FakeChunk(rerank=0.8), FakeChunk(rerank=0.01)]
    assert len(_apply(chunks, top_k=12)) == 2


def test_reranked_all_below_threshold_keeps_the_fallback():
    """Better to return three weak hits than nothing at all."""
    chunks = [FakeChunk(rerank=0.01) for _ in range(10)]
    assert len(_apply(chunks, top_k=12)) == 3


def test_partial_rerank_scores_still_threshold():
    """A mixed list means the cross-encoder ran; trust its scores."""
    chunks = [FakeChunk(rerank=0.9), FakeChunk(fused=0.02)]
    assert len(_apply(chunks, top_k=12)) == 1


def test_empty_input_is_safe():
    assert _apply([], top_k=12) == []
