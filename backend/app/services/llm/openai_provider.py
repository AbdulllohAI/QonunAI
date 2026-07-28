"""OpenAI-compatible provider (also serves vLLM / Together / any OpenAI-shaped API)."""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

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


class OpenAIProvider(BaseLLMProvider):
    name = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(
            api_key=api_key or settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            timeout=settings.LLM_TIMEOUT_S,
        )
        self.model = model or settings.OPENAI_MODEL

    @staticmethod
    def _payload(system: str, messages: list[ChatMessage]) -> list[dict]:
        return [{"role": "system", "content": system}] + [
            {"role": m.role, "content": m.content} for m in messages
        ]

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
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=self._payload(system, messages),
                max_tokens=max_tokens,
                temperature=0.1,
            )
        except Exception as exc:
            raise LLMProviderError(f"openai: {exc}") from exc

        choice = resp.choices[0]
        usage = resp.usage
        return LLMResponse(
            text=choice.message.content or "",
            provider=self.name,
            model=resp.model,
            tokens_in=usage.prompt_tokens if usage else 0,
            tokens_out=usage.completion_tokens if usage else 0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason=choice.finish_reason,
            refused=choice.finish_reason == "content_filter",
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
        collected: list[str] = []
        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=self._payload(system, messages),
                max_tokens=max_tokens,
                temperature=0.1,
                stream=True,
                stream_options={"include_usage": True},
            )
            usage = None
            finish = None
            async for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                finish = chunk.choices[0].finish_reason or finish
                if delta and delta.content:
                    collected.append(delta.content)
                    yield StreamEvent(type="delta", text=delta.content)

            yield StreamEvent(
                type="done",
                response=LLMResponse(
                    text="".join(collected),
                    provider=self.name,
                    model=self.model,
                    tokens_in=usage.prompt_tokens if usage else 0,
                    tokens_out=usage.completion_tokens if usage else 0,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    stop_reason=finish,
                ),
            )
        except Exception as exc:
            log.exception("openai stream failed")
            yield StreamEvent(type="error", error=str(exc))
