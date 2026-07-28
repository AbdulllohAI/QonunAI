"""Provider registry with runtime switching and fallback.

Callers ask for a provider by name (or take the configured default). Providers
are built lazily and cached, so an unconfigured OpenAI key never breaks startup
for an Anthropic-only deployment.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from app.core.config import LLMProvider, settings
from app.core.logging import get_logger
from app.services.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    LLMProviderError,
    LLMResponse,
    StreamEvent,
)

log = get_logger(__name__)


class LLMRouter:
    def __init__(self) -> None:
        self._cache: dict[str, BaseLLMProvider] = {}

    def get(self, name: str | LLMProvider | None = None) -> BaseLLMProvider:
        key = (name.value if isinstance(name, LLMProvider) else name) or settings.LLM_PROVIDER.value
        if key not in self._cache:
            self._cache[key] = self._build(key)
        return self._cache[key]

    @staticmethod
    def _build(key: str) -> BaseLLMProvider:
        match key:
            case LLMProvider.ANTHROPIC.value:
                from app.services.llm.anthropic_provider import AnthropicProvider

                return AnthropicProvider()
            case LLMProvider.OPENAI.value:
                from app.services.llm.openai_provider import OpenAIProvider

                return OpenAIProvider()
            case LLMProvider.OLLAMA.value:
                from app.services.llm.ollama_provider import OllamaProvider

                return OllamaProvider()
            case _:
                raise LLMProviderError(f"unknown LLM provider: {key}")

    def available(self) -> list[dict]:
        configured = {
            LLMProvider.ANTHROPIC.value: bool(settings.ANTHROPIC_API_KEY),
            LLMProvider.OPENAI.value: bool(settings.OPENAI_API_KEY),
            LLMProvider.OLLAMA.value: True,  # local; assume reachable, health-check separately
        }
        models = {
            LLMProvider.ANTHROPIC.value: settings.ANTHROPIC_MODEL,
            LLMProvider.OPENAI.value: settings.OPENAI_MODEL,
            LLMProvider.OLLAMA.value: settings.OLLAMA_MODEL,
        }
        return [
            {
                "provider": name,
                "model": models[name],
                "configured": ok,
                "default": name == settings.LLM_PROVIDER.value,
            }
            for name, ok in configured.items()
        ]

    async def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int | None = None,
        provider: str | None = None,
        fallback: str | None = None,
    ) -> LLMResponse:
        max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        try:
            return await self.get(provider).complete(
                system=system, messages=messages, max_tokens=max_tokens
            )
        except LLMProviderError:
            if not fallback:
                raise
            log.warning("falling back", extra={"from": provider, "to": fallback})
            return await self.get(fallback).complete(
                system=system, messages=messages, max_tokens=max_tokens
            )

    def stream(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int | None = None,
        provider: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self.get(provider).stream(
            system=system,
            messages=messages,
            max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
        )


llm_router = LLMRouter()
