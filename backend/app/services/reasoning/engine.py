"""The legal reasoning engine: retrieve → build context → reason → validate → score.

`answer()` returns a complete result; `stream()` yields SSE-shaped events for the
chat UI. Both go through the same validation and risk stages — streaming does not
get to skip the guardrails, so the final `done` event carries the validated text
and the client replaces what it streamed.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ActType, Language
from app.services.lang.detect import detect_language
from app.services.llm import ChatMessage, llm_router
from app.services.rag.context_builder import BuiltContext, context_builder
from app.services.rag.hybrid import hybrid_retriever
from app.services.rag.types import RetrievalResult
from app.services.reasoning import hierarchy as hierarchy_mod
from app.services.reasoning import risk as risk_mod
from app.services.reasoning import validator as validator_mod
from app.services.reasoning.prompts import (
    DISCLAIMER_BY_LANG,
    GREETING_RESPONSES,
    NO_CONTEXT_MESSAGES,
    build_system_prompt,
    context_message,
    is_greeting,
)

log = get_logger(__name__)


@dataclass(slots=True)
class LegalAnswer:
    answer: str
    citations: list[dict] = field(default_factory=list)
    risk: dict = field(default_factory=dict)
    hierarchy: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    warning: str | None = None
    disclaimer: str = ""
    language: str = "uz-Latn"
    mode: str = "qa"
    provider: str | None = None
    model: str | None = None
    retrieval_ms: int = 0
    llm_ms: int = 0
    total_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    retrieved_chunk_ids: list[str] = field(default_factory=list)
    answered: bool = True
    refusal_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "citations": self.citations,
            "risk": self.risk,
            "hierarchy": self.hierarchy,
            "validation": self.validation,
            "warning": self.warning,
            "disclaimer": self.disclaimer,
            "language": self.language,
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "timings": {
                "retrieval_ms": self.retrieval_ms,
                "llm_ms": self.llm_ms,
                "total_ms": self.total_ms,
            },
            "usage": {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out},
            "answered": self.answered,
            "refusal_reason": self.refusal_reason,
        }


class ReasoningEngine:
    async def _prepare(
        self,
        session: AsyncSession,
        question: str,
        *,
        mode: str,
        language: Language | None,
        act_types: Sequence[ActType] | None,
        act_ids: Sequence[uuid.UUID] | None,
        top_k: int | None,
    ) -> tuple[Language, RetrievalResult, BuiltContext]:
        lang = language or detect_language(question)
        retrieval = await hybrid_retriever.retrieve(
            session,
            question,
            language=lang,
            top_k=top_k or settings.RETRIEVAL_TOP_K_FINAL,
            act_types=act_types,
            act_ids=act_ids,
            expand_crossrefs=mode != "law_search",
        )
        context = context_builder.build(retrieval.chunks, answer_language=lang)
        return lang, retrieval, context

    def _no_context_answer(
        self, lang: Language, mode: str, retrieval_ms: int, started: float
    ) -> LegalAnswer:
        return LegalAnswer(
            answer=NO_CONTEXT_MESSAGES[lang],
            disclaimer=DISCLAIMER_BY_LANG[lang],
            language=lang.value,
            mode=mode,
            risk=risk_mod.RiskAssessment(
                level=risk_mod.RiskLevel.HIGH,
                factors=["No legal provisions were retrieved to support an answer."],
            ).to_dict(),
            retrieval_ms=retrieval_ms,
            total_ms=int((time.perf_counter() - started) * 1000),
            answered=False,
            refusal_reason="no_context",
        )

    def _greeting_answer(self, lang: Language, mode: str, started: float) -> LegalAnswer:
        """Short-circuit before retrieval even runs.

        Retrieval's own top-3 fallback (for when nothing clears the relevance
        threshold — meant to rescue a genuine question with merely imprecise
        embeddings) can't tell "weakly relevant" apart from "not a legal
        question at all." Left unguarded, a bare "salom" fed the model 3
        essentially-random chunks and it built a confident, fully-cited HIGH
        risk answer out of them — observed producing an answer about criminal
        liability for religious extremism in response to "hello." Catching
        this before retrieval is both correct and free: no wasted retrieval
        pass, no LLM call, no fabrication risk to guard against after the
        fact.
        """
        return LegalAnswer(
            answer=GREETING_RESPONSES[lang],
            disclaimer=DISCLAIMER_BY_LANG[lang],
            language=lang.value,
            mode=mode,
            risk=risk_mod.RiskAssessment(
                level=risk_mod.RiskLevel.LOW,
                factors=["Greeting — no legal question was asked."],
            ).to_dict(),
            total_ms=int((time.perf_counter() - started) * 1000),
            answered=True,
        )

    # ------------------------------------------------------------------ answer
    async def answer(
        self,
        session: AsyncSession,
        question: str,
        *,
        mode: str = "qa",
        language: Language | None = None,
        history: Sequence[ChatMessage] | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        top_k: int | None = None,
        provider: str | None = None,
        document_text: str | None = None,
    ) -> LegalAnswer:
        started = time.perf_counter()
        lang = language or detect_language(question)
        if not document_text and is_greeting(question):
            return self._greeting_answer(lang, mode, started)

        retrieval_query = _retrieval_query(question, document_text)
        lang, retrieval, context = await self._prepare(
            session,
            retrieval_query,
            mode=mode,
            language=lang,
            act_types=act_types,
            act_ids=act_ids,
            top_k=top_k,
        )

        if context.is_empty:
            return self._no_context_answer(lang, mode, retrieval.took_ms, started)

        system = build_system_prompt(mode, lang)
        user_turn = context_message(
            context.text, _user_payload(question, document_text), mode=mode
        )
        messages = list(history or []) + [ChatMessage(role="user", content=user_turn)]

        llm_response = await llm_router.complete(
            system=system, messages=messages, provider=provider
        )

        if llm_response.refused:
            return LegalAnswer(
                answer=NO_CONTEXT_MESSAGES[lang],
                disclaimer=DISCLAIMER_BY_LANG[lang],
                language=lang.value,
                mode=mode,
                provider=llm_response.provider,
                model=llm_response.model,
                risk=risk_mod.RiskAssessment(
                    level=risk_mod.RiskLevel.HIGH,
                    factors=["The model declined to answer this request."],
                ).to_dict(),
                retrieval_ms=retrieval.took_ms,
                llm_ms=llm_response.latency_ms,
                total_ms=int((time.perf_counter() - started) * 1000),
                answered=False,
                refusal_reason="model_refusal",
            )

        return self._finalise(
            question=question,
            raw_answer=llm_response.text,
            context=context,
            retrieval=retrieval,
            lang=lang,
            mode=mode,
            provider=llm_response.provider,
            model=llm_response.model,
            tokens_in=llm_response.tokens_in,
            tokens_out=llm_response.tokens_out,
            retrieval_ms=retrieval.took_ms,
            llm_ms=llm_response.latency_ms,
            started=started,
        )

    # ------------------------------------------------------------------ stream
    async def stream(
        self,
        session: AsyncSession,
        question: str,
        *,
        mode: str = "qa",
        language: Language | None = None,
        history: Sequence[ChatMessage] | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        provider: str | None = None,
        document_text: str | None = None,
    ) -> AsyncIterator[dict]:
        started = time.perf_counter()
        lang = language or detect_language(question)
        if not document_text and is_greeting(question):
            # "meta" was already sent by the caller before stream() was
            # invoked (see chat.py) — the contract from here on is
            # sources -> delta -> done, same as the retrieval path below,
            # just with an empty sources list since none were fetched.
            result = self._greeting_answer(lang, mode, started)
            yield {"type": "sources", "sources": [], "retrieval_ms": 0, "language": lang.value}
            yield {"type": "delta", "text": result.answer}
            yield {"type": "done", "result": result.to_dict()}
            return

        lang, retrieval, context = await self._prepare(
            session,
            _retrieval_query(question, document_text),
            mode=mode,
            language=lang,
            act_types=act_types,
            act_ids=act_ids,
            top_k=None,
        )

        # Emit sources first so the UI can render citation chips while the answer
        # is still generating.
        yield {
            "type": "sources",
            "sources": [s.to_citation_dict() for s in context.sources],
            "retrieval_ms": retrieval.took_ms,
            "language": lang.value,
        }

        if context.is_empty:
            result = self._no_context_answer(lang, mode, retrieval.took_ms, started)
            yield {"type": "delta", "text": result.answer}
            yield {"type": "done", "result": result.to_dict()}
            return

        system = build_system_prompt(mode, lang)
        user_turn = context_message(
            context.text, _user_payload(question, document_text), mode=mode
        )
        messages = list(history or []) + [ChatMessage(role="user", content=user_turn)]

        collected: list[str] = []
        final_response = None
        async for event in llm_router.stream(
            system=system, messages=messages, provider=provider
        ):
            if event.type == "delta":
                collected.append(event.text)
                yield {"type": "delta", "text": event.text}
            elif event.type == "error":
                yield {"type": "error", "error": event.error}
                return
            elif event.type == "done":
                final_response = event.response

        raw = (final_response.text if final_response else "") or "".join(collected)
        result = self._finalise(
            question=question,
            raw_answer=raw,
            context=context,
            retrieval=retrieval,
            lang=lang,
            mode=mode,
            provider=final_response.provider if final_response else None,
            model=final_response.model if final_response else None,
            tokens_in=final_response.tokens_in if final_response else 0,
            tokens_out=final_response.tokens_out if final_response else 0,
            retrieval_ms=retrieval.took_ms,
            llm_ms=final_response.latency_ms if final_response else 0,
            started=started,
        )
        # The client replaces the streamed text with `result.answer`: validation
        # may have stripped fabricated citations from what was already shown.
        yield {"type": "done", "result": result.to_dict()}

    # ---------------------------------------------------------------- finalise
    def _finalise(
        self,
        *,
        question: str,
        raw_answer: str,
        context: BuiltContext,
        retrieval: RetrievalResult,
        lang: Language,
        mode: str,
        provider: str | None,
        model: str | None,
        tokens_in: int,
        tokens_out: int,
        retrieval_ms: int,
        llm_ms: int,
        started: float,
    ) -> LegalAnswer:
        validation = validator_mod.validate(raw_answer, context.sources)
        analysis = hierarchy_mod.resolve(
            [s.chunk for s in context.sources if s.chunk.via_crossref_from is None]
        )
        assessment = risk_mod.assess(
            question=question,
            answer=validation.text,
            chunks=[s.chunk for s in context.sources],
            conflict_count=len(analysis.conflicts),
            context_truncated=context.truncated,
        )
        warning = validator_mod.build_warning(validation, lang.value)

        if validation.rejected:
            log.warning(
                "answer rejected by validator",
                extra={"reason": validation.reason, "mode": mode},
            )
            return LegalAnswer(
                answer=NO_CONTEXT_MESSAGES[lang],
                disclaimer=DISCLAIMER_BY_LANG[lang],
                language=lang.value,
                mode=mode,
                provider=provider,
                model=model,
                risk=risk_mod.RiskAssessment(
                    level=risk_mod.RiskLevel.HIGH,
                    factors=["The generated answer could not be tied to retrieved sources."],
                ).to_dict(),
                validation=validation.to_dict(),
                retrieval_ms=retrieval_ms,
                llm_ms=llm_ms,
                total_ms=int((time.perf_counter() - started) * 1000),
                answered=False,
                refusal_reason=validation.reason,
            )

        # The model's own inline "Risk level: X" line can under-state what
        # assess() just decided (it takes the higher of the two), or be
        # missing outright — reconcile or synthesise it so the answer body
        # never contradicts (or omits) the risk badge shown next to it.
        answer_text = risk_mod.ensure_stated_risk(validation.text, assessment, lang)

        return LegalAnswer(
            answer=answer_text,
            citations=validator_mod.used_source_dicts(validation, context.sources),
            risk=assessment.to_dict(),
            hierarchy=analysis.to_dict(),
            validation=validation.to_dict(),
            warning=warning,
            disclaimer=DISCLAIMER_BY_LANG[lang],
            language=lang.value,
            mode=mode,
            provider=provider,
            model=model,
            retrieval_ms=retrieval_ms,
            llm_ms=llm_ms,
            total_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            retrieved_chunk_ids=[str(c.chunk_id) for c in retrieval.chunks],
        )


def _retrieval_query(question: str, document_text: str | None) -> str:
    """For document analysis, retrieval must see the document, not just the ask —
    but only a bounded prefix, or the query embedding drowns in boilerplate."""
    if not document_text:
        return question
    return f"{question}\n\n{document_text[:4000]}"


def _user_payload(question: str, document_text: str | None) -> str:
    if not document_text:
        return question
    return f"DOCUMENT\n--------\n{document_text}\n\nQUESTION\n--------\n{question}"


reasoning_engine = ReasoningEngine()
