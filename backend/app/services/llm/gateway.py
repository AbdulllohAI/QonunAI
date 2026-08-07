"""Model gateway: priority fallback, circuit breaking, and usage accounting.

A single upstream model is a single point of failure, and this system already
lost a day of production answers to one: the OpenRouter account ran out of
credit and every request returned 402 with the raw provider JSON reaching the
user. A gateway with a fallback chain turns that from an outage into a
degradation.

Ordering is by capability, so the chain is also a cost gradient — the strongest
model is tried first and cheaper ones catch the overflow.

**Streaming fallback has a hard boundary worth understanding.** Once a model has
emitted its first delta, the client has already rendered text; silently
switching models mid-answer would splice two different completions together and
produce something neither model actually said. So streaming falls back only
*before* the first delta. After that, a failure is surfaced as an error rather
than papered over. Non-streaming has no such constraint and falls back freely.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.services.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    LLMProviderError,
    LLMResponse,
    StreamEvent,
)

log = get_logger(__name__)

__all__ = ["ModelCandidate", "ModelGateway", "GatewayStats"]

#: Consecutive failures before a model is taken out of rotation.
FAILURE_THRESHOLD = 3
#: How long a tripped model stays out before one probe request is allowed.
COOLDOWN_SECONDS = 60.0
#: Attempts per model before moving down the chain. The retry covers transient
#: 5xx and timeouts; anything that fails twice is treated as the model's problem.
ATTEMPTS_PER_MODEL = 2
#: Backoff between attempts on the same model.
RETRY_BACKOFF_SECONDS = 0.75


@dataclass(slots=True)
class ModelCandidate:
    """One rung of the fallback chain."""

    provider: BaseLLMProvider
    label: str
    """Stable name for metrics, e.g. ``openai/gpt-5.5``."""

    consecutive_failures: int = 0
    opened_at: float | None = None
    """When the breaker tripped; None while closed."""

    requests: int = 0
    failures: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    total_latency_ms: int = 0

    def is_available(self, now: float) -> bool:
        """Closed breaker, or open long enough to allow one probe."""
        if self.opened_at is None:
            return True
        return (now - self.opened_at) >= COOLDOWN_SECONDS

    def record_success(self, response: LLMResponse) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self.requests += 1
        self.tokens_in += response.tokens_in
        self.tokens_out += response.tokens_out
        self.total_latency_ms += response.latency_ms

    def record_failure(self) -> None:
        self.requests += 1
        self.failures += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= FAILURE_THRESHOLD and self.opened_at is None:
            self.opened_at = time.monotonic()
            log.warning(
                "model_breaker_open",
                extra={"model": self.label, "consecutive_failures": self.consecutive_failures},
            )

    def snapshot(self) -> dict:
        avg = self.total_latency_ms // self.requests if self.requests else 0
        return {
            "model": self.label,
            "state": "open" if self.opened_at is not None else "closed",
            "requests": self.requests,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "avg_latency_ms": avg,
        }


@dataclass(slots=True)
class GatewayStats:
    served: int = 0
    fell_back: int = 0
    exhausted: int = 0
    per_model: list[dict] = field(default_factory=list)


class ModelGateway:
    """Tries each candidate in order until one answers."""

    def __init__(self, candidates: list[ModelCandidate]) -> None:
        if not candidates:
            raise ValueError("ModelGateway needs at least one candidate")
        self._candidates = candidates
        self._served = 0
        self._fell_back = 0
        self._exhausted = 0

    # ------------------------------------------------------------------ non-streaming
    async def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int,
        cacheable_system: bool = True,
    ) -> LLMResponse:
        now = time.monotonic()
        errors: list[str] = []
        skipped_first = False

        for index, candidate in enumerate(self._candidates):
            if not candidate.is_available(now):
                errors.append(f"{candidate.label}: breaker open")
                skipped_first = skipped_first or index == 0
                continue

            for attempt in range(ATTEMPTS_PER_MODEL):
                try:
                    response = await candidate.provider.complete(
                        system=system,
                        messages=messages,
                        max_tokens=max_tokens,
                        cacheable_system=cacheable_system,
                    )
                except Exception as exc:  # provider SDKs raise many shapes
                    if attempt + 1 < ATTEMPTS_PER_MODEL:
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS)
                        continue
                    candidate.record_failure()
                    errors.append(f"{candidate.label}: {exc}")
                    log.warning(
                        "model_failed",
                        extra={"model": candidate.label, "error": str(exc)[:200]},
                    )
                    break

                candidate.record_success(response)
                self._served += 1
                if index > 0 or skipped_first:
                    self._fell_back += 1
                    log.info(
                        "model_fallback_used",
                        extra={"model": candidate.label, "rung": index, "skipped": errors},
                    )
                return response

        self._exhausted += 1
        raise LLMProviderError("all models failed: " + "; ".join(errors))

    # ------------------------------------------------------------------ streaming
    async def stream(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int,
        cacheable_system: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        """Stream from the first model that produces a delta.

        Falls back only while nothing has been emitted yet — see module docstring.
        """
        now = time.monotonic()
        errors: list[str] = []

        for index, candidate in enumerate(self._candidates):
            if not candidate.is_available(now):
                errors.append(f"{candidate.label}: breaker open")
                continue

            emitted = False
            try:
                async for event in candidate.provider.stream(
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                    cacheable_system=cacheable_system,
                ):
                    if event.type == "error":
                        # Only recoverable before the first delta.
                        if emitted:
                            candidate.record_failure()
                            yield event
                            return
                        raise LLMProviderError(event.error or "stream error")

                    if event.type == "delta":
                        emitted = True
                    elif event.type == "done" and event.response is not None:
                        candidate.record_success(event.response)

                    yield event

                if emitted:
                    self._served += 1
                    if index > 0:
                        self._fell_back += 1
                        log.info("model_fallback_used", extra={"model": candidate.label})
                    return
                # A stream that ended without any content is a failure, not an
                # empty answer — fall through to the next model.
                raise LLMProviderError("stream produced no content")

            except Exception as exc:
                if emitted:
                    # Too late to switch: the client already has partial text.
                    candidate.record_failure()
                    yield StreamEvent(type="error", error=str(exc))
                    return
                candidate.record_failure()
                errors.append(f"{candidate.label}: {exc}")
                log.warning(
                    "model_stream_failed",
                    extra={"model": candidate.label, "error": str(exc)[:200]},
                )
                continue

        self._exhausted += 1
        yield StreamEvent(type="error", error="all models failed: " + "; ".join(errors))

    # ------------------------------------------------------------------ metrics
    def stats(self) -> GatewayStats:
        return GatewayStats(
            served=self._served,
            fell_back=self._fell_back,
            exhausted=self._exhausted,
            per_model=[c.snapshot() for c in self._candidates],
        )
