"""The remote reranker must fail soft, and say so.

Every failure path here returns the fused order rather than an error: Recall@5
is 0.931 with no reranking at all, so a reranker that cannot answer is never
worth failing a legal query over. The matching risk is the one this codebase
has already been bitten by twice — a pipeline stage that stops running and
tells nobody — so each path logs at error level, and these tests pin both
halves.
"""
from __future__ import annotations

import pytest

from app.services.rag.reranker import Reranker
from app.services.rag.types import RetrievedChunk


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, *, post=None, get=None, raises=None):
        self._post, self._get, self._raises = post, get, raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        if self._raises:
            raise self._raises
        return self._post

    async def get(self, url):
        if self._raises:
            raise self._raises
        return self._get


def _patch_httpx(monkeypatch, client):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)


@pytest.mark.asyncio
async def test_scores_come_back_in_order(monkeypatch):
    _patch_httpx(monkeypatch, FakeClient(post=FakeResponse({"scores": [0.1, 0.9]})))
    assert await Reranker()._score_remote("q", ["a", "b"]) == [0.1, 0.9]


@pytest.mark.asyncio
async def test_unreachable_service_returns_none(monkeypatch):
    """None means "keep the order you had", not "raise"."""
    _patch_httpx(monkeypatch, FakeClient(raises=OSError("connection refused")))
    assert await Reranker()._score_remote("q", ["a"]) is None


@pytest.mark.asyncio
async def test_timeout_returns_none(monkeypatch):
    _patch_httpx(monkeypatch, FakeClient(raises=TimeoutError("timed out")))
    assert await Reranker()._score_remote("q", ["a"]) is None


@pytest.mark.asyncio
async def test_http_error_returns_none(monkeypatch):
    _patch_httpx(monkeypatch, FakeClient(post=FakeResponse({}, status=503)))
    assert await Reranker()._score_remote("q", ["a"]) is None


@pytest.mark.asyncio
async def test_wrong_number_of_scores_is_rejected(monkeypatch):
    """Zipping mismatched lists would silently attach one passage's score to
    another passage — worse than not reranking, because it looks like it worked."""
    _patch_httpx(monkeypatch, FakeClient(post=FakeResponse({"scores": [0.5]})))
    assert await Reranker()._score_remote("q", ["a", "b", "c"]) is None


@pytest.mark.asyncio
async def test_failure_is_logged_at_error_level(monkeypatch, caplog):
    _patch_httpx(monkeypatch, FakeClient(raises=OSError("down")))
    with caplog.at_level("ERROR"):
        await Reranker()._score_remote("q", ["a"])
    assert any(r.levelname == "ERROR" for r in caplog.records)


# ------------------------------------------------------------------- probe

@pytest.mark.asyncio
async def test_probe_accepts_a_loaded_service(monkeypatch):
    _patch_httpx(monkeypatch, FakeClient(get=FakeResponse({"loaded": True, "model": "m"})))
    assert await Reranker()._probe_remote() is True


@pytest.mark.asyncio
async def test_probe_rejects_a_service_without_its_model(monkeypatch):
    """The service answers /health even when the model failed to load. Treating
    that as ready would mean discovering it on a user's question."""
    _patch_httpx(monkeypatch, FakeClient(get=FakeResponse({"loaded": False, "error": "oom"})))
    assert await Reranker()._probe_remote() is False


@pytest.mark.asyncio
async def test_probe_rejects_an_unreachable_service(monkeypatch):
    _patch_httpx(monkeypatch, FakeClient(raises=OSError("no route to host")))
    assert await Reranker()._probe_remote() is False


# ------------------------------------------------------- end-to-end ordering

def _chunk(article: str, text: str) -> RetrievedChunk:
    from app.db.models import ActType, Language
    import uuid

    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        act_id=uuid.uuid4(),
        text=text,
        law_name="Test Code",
        article_number=article,
        act_type=ActType.CODE,
        language=Language.RU,
        hierarchy_path="",
        heading=f"{article}-modda. {text}",
    )


@pytest.mark.asyncio
async def test_remote_scores_reorder_the_results(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.RERANKER_ENABLED", True)
    monkeypatch.setattr("app.core.config.settings.RERANKER_BACKEND", "remote")
    _patch_httpx(monkeypatch, FakeClient(post=FakeResponse({"scores": [-2.0, 3.0]})))

    chunks = [_chunk("1", "irrelevant"), _chunk("2", "governing")]
    ordered = await Reranker().rerank("q", chunks, top_k=2)
    assert [c.article_number for c in ordered] == ["2", "1"]


@pytest.mark.asyncio
async def test_a_dead_service_leaves_the_fused_order_intact(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.RERANKER_ENABLED", True)
    monkeypatch.setattr("app.core.config.settings.RERANKER_BACKEND", "remote")
    _patch_httpx(monkeypatch, FakeClient(raises=OSError("down")))

    chunks = [_chunk("1", "first"), _chunk("2", "second")]
    ordered = await Reranker().rerank("q", chunks, top_k=2)
    assert [c.article_number for c in ordered] == ["1", "2"]
