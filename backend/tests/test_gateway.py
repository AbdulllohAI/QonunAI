"""Model gateway: fallback, circuit breaking, and the streaming boundary.

The streaming tests matter most. Falling back mid-stream would splice two
different completions into one answer — text neither model actually produced —
which in a legal tool is a correctness failure, not a cosmetic one.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.services.llm.base import ChatMessage, LLMProviderError, LLMResponse, StreamEvent
from app.services.llm.gateway import (
    FAILURE_THRESHOLD,
    ModelCandidate,
    ModelGateway,
)


class FakeProvider:
    """Provider stub with scripted behaviour."""

    def __init__(self, name: str, *, fail: bool = False, deltas: list[str] | None = None,
                 fail_after_delta: bool = False):
        self.name = name
        self.model = name
        self.fail = fail
        self.deltas = deltas if deltas is not None else ["hello"]
        self.fail_after_delta = fail_after_delta
        self.calls = 0

    async def complete(self, *, system, messages, max_tokens, cacheable_system=True):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} unavailable")
        return LLMResponse(text="ok", provider=self.name, model=self.name,
                           tokens_in=10, tokens_out=5, latency_ms=100)

    async def stream(self, *, system, messages, max_tokens, cacheable_system=True
                     ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} unavailable")
        for chunk in self.deltas:
            yield StreamEvent(type="delta", text=chunk)
            if self.fail_after_delta:
                raise RuntimeError(f"{self.name} died mid-stream")
        yield StreamEvent(type="done", response=LLMResponse(
            text="".join(self.deltas), provider=self.name, model=self.name,
            tokens_in=10, tokens_out=5, latency_ms=100))


def _gateway(*providers) -> ModelGateway:
    return ModelGateway([ModelCandidate(provider=p, label=p.name) for p in providers])


MSG = [ChatMessage(role="user", content="hi")]


# ------------------------------------------------------------- non-streaming

@pytest.mark.asyncio
async def test_first_healthy_model_serves():
    primary, backup = FakeProvider("primary"), FakeProvider("backup")
    res = await _gateway(primary, backup).complete(system="s", messages=MSG, max_tokens=10)
    assert res.provider == "primary"
    assert backup.calls == 0, "backup must not be called when primary works"


@pytest.mark.asyncio
async def test_falls_back_when_primary_fails():
    primary, backup = FakeProvider("primary", fail=True), FakeProvider("backup")
    res = await _gateway(primary, backup).complete(system="s", messages=MSG, max_tokens=10)
    assert res.provider == "backup"


@pytest.mark.asyncio
async def test_retries_before_falling_back():
    """A transient blip should not cost a rung of the chain."""
    primary = FakeProvider("primary", fail=True)
    await _gateway(primary, FakeProvider("backup")).complete(
        system="s", messages=MSG, max_tokens=10)
    assert primary.calls == 2, "expected one retry before fallback"


@pytest.mark.asyncio
async def test_all_models_failing_raises():
    gw = _gateway(FakeProvider("a", fail=True), FakeProvider("b", fail=True))
    with pytest.raises(LLMProviderError, match="all models failed"):
        await gw.complete(system="s", messages=MSG, max_tokens=10)


@pytest.mark.asyncio
async def test_breaker_opens_and_stops_calling_dead_model():
    primary = FakeProvider("primary", fail=True)
    gw = _gateway(primary, FakeProvider("backup"))
    for _ in range(FAILURE_THRESHOLD):
        await gw.complete(system="s", messages=MSG, max_tokens=10)
    calls_before = primary.calls
    await gw.complete(system="s", messages=MSG, max_tokens=10)
    assert primary.calls == calls_before, "breaker should skip the dead model entirely"


@pytest.mark.asyncio
async def test_usage_is_accounted_per_model():
    gw = _gateway(FakeProvider("primary"))
    await gw.complete(system="s", messages=MSG, max_tokens=10)
    await gw.complete(system="s", messages=MSG, max_tokens=10)
    row = gw.stats().per_model[0]
    assert row["requests"] == 2
    assert row["tokens_in"] == 20 and row["tokens_out"] == 10
    assert row["avg_latency_ms"] == 100


@pytest.mark.asyncio
async def test_fallback_is_counted():
    gw = _gateway(FakeProvider("primary", fail=True), FakeProvider("backup"))
    await gw.complete(system="s", messages=MSG, max_tokens=10)
    assert gw.stats().fell_back == 1


# ------------------------------------------------------------------ streaming

@pytest.mark.asyncio
async def test_stream_falls_back_before_first_delta():
    gw = _gateway(FakeProvider("primary", fail=True),
                  FakeProvider("backup", deltas=["a", "b"]))
    events = [e async for e in gw.stream(system="s", messages=MSG, max_tokens=10)]
    assert "".join(e.text for e in events if e.type == "delta") == "ab"
    assert not [e for e in events if e.type == "error"]


@pytest.mark.asyncio
async def test_stream_does_not_switch_models_mid_answer():
    """Splicing two completions would produce text neither model generated."""
    backup = FakeProvider("backup", deltas=["should-not-appear"])
    gw = _gateway(FakeProvider("primary", deltas=["partial"], fail_after_delta=True), backup)

    events = [e async for e in gw.stream(system="s", messages=MSG, max_tokens=10)]
    text = "".join(e.text for e in events if e.type == "delta")

    assert text == "partial", "must not append a second model's output"
    assert backup.calls == 0, "must not start the backup after emitting"
    assert events[-1].type == "error", "the failure must surface, not be hidden"


@pytest.mark.asyncio
async def test_empty_stream_is_treated_as_failure():
    """A silent empty completion is a failure, not a valid empty answer."""
    gw = _gateway(FakeProvider("primary", deltas=[]), FakeProvider("backup", deltas=["real"]))
    events = [e async for e in gw.stream(system="s", messages=MSG, max_tokens=10)]
    assert "".join(e.text for e in events if e.type == "delta") == "real"


@pytest.mark.asyncio
async def test_stream_exhaustion_yields_error_event():
    gw = _gateway(FakeProvider("a", fail=True), FakeProvider("b", fail=True))
    events = [e async for e in gw.stream(system="s", messages=MSG, max_tokens=10)]
    assert events[-1].type == "error"
    assert "all models failed" in events[-1].error
