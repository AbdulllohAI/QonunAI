"""Retrieval degradation must be visible.

These tests exist because of a real production failure: `sentence-transformers`
was missing from the image, so `embed_query` raised on every request, the dense
branch was unwrapped to `[]`, and the hybrid retriever served national legal
queries from keyword search alone. Nothing reported a problem — `/health`
showed `embedded_chunks: 11538` and a green status the whole time, because
that field counts *document* embeddings and says nothing about whether a query
can be embedded.

The defect that made it survive was not the missing package. It was that a
broken branch and an empty branch were the same observable event.
"""
from __future__ import annotations

import pytest

from app.services.rag.embedder import Embedder, EmbeddingUnavailable
from app.services.rag.hybrid import _unwrap
from app.services.rag.types import RetrievalResult


# --------------------------------------------------------------- branch unwrap

def test_empty_branch_is_not_reported_as_degraded():
    """Finding nothing is a normal outcome, not a fault."""
    degraded: dict[str, str] = {}
    assert _unwrap([], "dense", degraded) == []
    assert degraded == {}


def test_failed_branch_is_recorded_with_its_reason():
    degraded: dict[str, str] = {}
    exc = ModuleNotFoundError("No module named 'sentence_transformers'")

    assert _unwrap(exc, "dense", degraded) == [], "must still degrade, not crash the query"
    assert "dense" in degraded
    assert "sentence_transformers" in degraded["dense"]


def test_failure_and_emptiness_are_distinguishable():
    """The whole point: these two produced identical output before."""
    broken: dict[str, str] = {}
    empty: dict[str, str] = {}

    _unwrap(RuntimeError("connection refused"), "dense", broken)
    _unwrap([], "dense", empty)

    assert broken != empty


def test_one_dead_branch_does_not_take_down_the_others():
    degraded: dict[str, str] = {}
    dense = _unwrap(RuntimeError("no embedder"), "dense", degraded)
    sparse = _unwrap(["a", "b"], "sparse", degraded)

    assert dense == [] and sparse == ["a", "b"]
    assert list(degraded) == ["dense"], "only the branch that failed is flagged"


def test_unwrap_without_a_collector_still_works():
    """Callers that don't care about degradation must not be forced to."""
    assert _unwrap(RuntimeError("boom"), "dense") == []


# ------------------------------------------------------------- result plumbing

def test_result_reports_degradation():
    assert not RetrievalResult().is_degraded
    assert RetrievalResult(degraded_branches={"dense": "no embedder"}).is_degraded


def test_degraded_result_can_still_carry_chunks():
    """A degraded answer is the dangerous case — it looks completely normal."""
    result = RetrievalResult(chunks=["x"], degraded_branches={"dense": "no embedder"})
    assert not result.is_empty
    assert result.is_degraded


# ------------------------------------------------------------ embedder probe

def test_availability_flags_a_missing_package(monkeypatch):
    monkeypatch.setattr(
        "app.services.rag.embedder.importlib.util.find_spec", lambda name: None
    )
    ok, reason = Embedder().availability()

    assert ok is False
    assert "sentence-transformers" in reason
    assert "keyword search" in reason, "reason must state the user-visible consequence"


def test_availability_reports_a_sticky_load_failure():
    emb = Embedder()
    emb._load_error = "not enough memory to load BAAI/bge-m3"

    ok, reason = emb.availability()
    assert ok is False
    assert "memory" in reason


def test_availability_is_true_once_the_model_is_loaded():
    emb = Embedder()
    emb._model = object()
    assert emb.availability() == (True, None)


def test_availability_does_not_load_the_weights(monkeypatch):
    """/health is polled every 30s. Loading 2.3GB per probe would OOM the box."""
    loaded = False

    def _boom(*args, **kwargs):
        nonlocal loaded
        loaded = True
        raise AssertionError("availability() must never construct the model")

    monkeypatch.setattr("app.services.rag.embedder.importlib.util.find_spec", lambda n: object())
    Embedder().availability()
    assert loaded is False


@pytest.mark.asyncio
async def test_load_failure_is_sticky_and_not_retried(monkeypatch):
    """Retrying a multi-second load on every request turns degradation into an outage."""
    emb = Embedder()
    attempts = 0

    def _fail(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ImportError("No module named 'sentence_transformers'")

    monkeypatch.setitem(
        __import__("sys").modules, "sentence_transformers", None
    )
    monkeypatch.setattr(emb, "_model", None)
    emb._load_error = "sentence-transformers is not installed"

    for _ in range(3):
        with pytest.raises(EmbeddingUnavailable):
            await emb._get_model()

    assert attempts == 0, "a known-bad load must not be reattempted"
