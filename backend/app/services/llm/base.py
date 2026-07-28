"""Provider-agnostic LLM interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant"]


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(slots=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    stop_reason: str | None = None
    refused: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamEvent:
    """One SSE-able unit. `type` is 'delta' | 'done' | 'error'."""

    type: Literal["delta", "done", "error"]
    text: str = ""
    response: LLMResponse | None = None
    error: str | None = None


class LLMProviderError(RuntimeError):
    pass


class BaseLLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int,
        cacheable_system: bool = True,
    ) -> LLMResponse:
        ...

    @abstractmethod
    def stream(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int,
        cacheable_system: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        ...

    async def health(self) -> bool:
        try:
            await self.complete(
                system="Reply with OK.",
                messages=[ChatMessage(role="user", content="ping")],
                max_tokens=16,
                cacheable_system=False,
            )
            return True
        except Exception:
            return False
