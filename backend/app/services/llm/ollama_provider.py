"""Local LLaMA 3.x via Ollama — for on-prem / low-VRAM and air-gapped deployments.

Defaults to a q4_K_M quantisation so an 8B model fits in ~6 GB VRAM. `num_ctx`
is raised to 8192 because a legal RAG context with a dozen articles routinely
exceeds Ollama's 2048-token default, which would otherwise silently truncate the
retrieved statutes — the single most dangerous failure mode in this system.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

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


class OllamaProvider(BaseLLMProvider):
    name = "ollama"

    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL

    def _body(self, system: str, messages: list[ChatMessage], max_tokens: int, stream: bool):
        return {
            "model": self.model,
            "stream": stream,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "options": {
                "temperature": 0.1,
                "num_predict": max_tokens,
                "num_ctx": 8192,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
            },
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
        async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_S) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json=self._body(system, messages, max_tokens, stream=False),
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise LLMProviderError(f"ollama: {exc}") from exc
            data = resp.json()

        return LLMResponse(
            text=data.get("message", {}).get("content", ""),
            provider=self.name,
            model=self.model,
            tokens_in=data.get("prompt_eval_count", 0),
            tokens_out=data.get("eval_count", 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason=data.get("done_reason"),
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
            async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT_S) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/chat",
                    json=self._body(system, messages, max_tokens, stream=True),
                ) as resp:
                    resp.raise_for_status()
                    final: dict = {}
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        payload = json.loads(line)
                        piece = payload.get("message", {}).get("content", "")
                        if piece:
                            collected.append(piece)
                            yield StreamEvent(type="delta", text=piece)
                        if payload.get("done"):
                            final = payload

            yield StreamEvent(
                type="done",
                response=LLMResponse(
                    text="".join(collected),
                    provider=self.name,
                    model=self.model,
                    tokens_in=final.get("prompt_eval_count", 0),
                    tokens_out=final.get("eval_count", 0),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    stop_reason=final.get("done_reason"),
                ),
            )
        except Exception as exc:
            log.exception("ollama stream failed")
            yield StreamEvent(type="error", error=str(exc))
