"""Anthropic provider (default).

Two things worth noting for this workload:

* **Prompt caching.** The legal system prompt is long and byte-identical across
  every request, so it carries a `cache_control` breakpoint. The retrieved
  context and the user's question go in `messages`, after the breakpoint, so
  they never invalidate the cached prefix.
* **Refusals.** Claude Opus 5 can decline via `stop_reason: "refusal"` on an
  HTTP 200. Reading `content[0]` unconditionally would crash, so `stop_reason`
  is checked first and a refusal is surfaced as `LLMResponse.refused`.
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

import anthropic

from app.core.config import settings
from app.core.logging import get_logger
from app.services.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    LLMProviderError,
    LLMResponse,
    StreamEvent,
)

log = get_logger(__name__)


class AnthropicProvider(BaseLLMProvider):
    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        key = api_key or settings.ANTHROPIC_API_KEY
        # A bare client also resolves an `ant auth login` profile, so a missing
        # env var is not necessarily a misconfiguration.
        self.client = anthropic.AsyncAnthropic(api_key=key, timeout=settings.LLM_TIMEOUT_S)
        self.model = model or settings.ANTHROPIC_MODEL

    def _system_blocks(self, system: str, cacheable: bool) -> list[dict]:
        block: dict = {"type": "text", "text": system}
        if cacheable:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _kwargs(self, system: str, messages: list[ChatMessage], max_tokens: int, cacheable: bool):
        return {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": self._system_blocks(system, cacheable),
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": settings.ANTHROPIC_EFFORT},
        }

    async def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int,
        cacheable_system: bool = True,
    ) -> LLMResponse:
        started = time.perf_counter()
        try:
            # Streaming even for "non-streaming" callers: max_tokens is large
            # enough that a plain request risks an HTTP timeout.
            async with self.client.messages.stream(
                **self._kwargs(system, messages, max_tokens, cacheable_system)
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise LLMProviderError(f"anthropic {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMProviderError("anthropic connection error") from exc

        refused = message.stop_reason == "refusal"
        text = "" if refused else "".join(
            b.text for b in message.content if getattr(b, "type", None) == "text"
        )

        return LLMResponse(
            text=text,
            provider=self.name,
            model=message.model,
            tokens_in=message.usage.input_tokens,
            tokens_out=message.usage.output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason=message.stop_reason,
            refused=refused,
            raw={
                "cache_read_input_tokens": getattr(
                    message.usage, "cache_read_input_tokens", 0
                ),
                "cache_creation_input_tokens": getattr(
                    message.usage, "cache_creation_input_tokens", 0
                ),
            },
        )

    async def stream(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int,
        cacheable_system: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        started = time.perf_counter()
        try:
            async with self.client.messages.stream(
                **self._kwargs(system, messages, max_tokens, cacheable_system)
            ) as stream:
                async for delta in stream.text_stream:
                    yield StreamEvent(type="delta", text=delta)
                message = await stream.get_final_message()

            refused = message.stop_reason == "refusal"
            yield StreamEvent(
                type="done",
                response=LLMResponse(
                    text="".join(
                        b.text for b in message.content if getattr(b, "type", None) == "text"
                    ),
                    provider=self.name,
                    model=message.model,
                    tokens_in=message.usage.input_tokens,
                    tokens_out=message.usage.output_tokens,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    stop_reason=message.stop_reason,
                    refused=refused,
                ),
            )
        except anthropic.APIStatusError as exc:
            log.error("anthropic stream failed", extra={"status": exc.status_code})
            yield StreamEvent(type="error", error=f"anthropic {exc.status_code}")
        except Exception as exc:
            log.exception("anthropic stream crashed")
            yield StreamEvent(type="error", error=str(exc))
