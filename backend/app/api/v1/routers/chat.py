"""Chat endpoints: streaming Q&A with conversation memory."""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_optional_user, rate_limit
from app.core.logging import get_logger, request_id_ctx
from app.db.models import Conversation, Language, Message, QueryLog, User
from app.db.session import SessionLocal, get_session
from app.schemas.common import ChatRequest, ChatResponse, ConversationOut, MessageOut
from app.services.lang.detect import detect_language
from app.services.llm import ChatMessage
from app.services.reasoning import reasoning_engine

log = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

#: How many prior turns to replay. Legal follow-ups ("and if the contract was
#: oral?") depend on recent context, but a long history dilutes retrieval and
#: costs tokens.
HISTORY_TURNS = 6


async def _load_or_create_conversation(
    session: AsyncSession,
    conversation_id: uuid.UUID | None,
    user: User | None,
    mode: str,
    language: Language,
    first_message: str,
) -> Conversation:
    if conversation_id:
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.id == conversation_id)
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        if conversation.user_id and (user is None or conversation.user_id != user.id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "not your conversation")
        return conversation

    conversation = Conversation(
        user_id=user.id if user else None,
        title=first_message[:120],
        mode=mode,
        language=language,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def _history(session: AsyncSession, conversation_id: uuid.UUID) -> list[ChatMessage]:
    rows = list(
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc())
                .limit(HISTORY_TURNS * 2)
            )
        ).scalars()
    )
    return [
        ChatMessage(role=m.role, content=m.content)
        for m in reversed(rows)
        if m.role in ("user", "assistant")
    ]


@router.post("", response_model=ChatResponse, dependencies=[Depends(rate_limit)])
async def chat(
    payload: ChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(get_optional_user),
):
    """Non-streaming answer. Use `/chat/stream` for the chat UI."""
    language = payload.language or detect_language(payload.message)
    conversation = await _load_or_create_conversation(
        session, payload.conversation_id, user, payload.mode, language, payload.message
    )
    history = await _history(session, conversation.id)

    session.add(
        Message(conversation_id=conversation.id, role="user", content=payload.message)
    )
    await session.flush()

    result = await reasoning_engine.answer(
        session,
        payload.message,
        mode=payload.mode,
        language=language,
        history=history,
        act_types=payload.act_types,
        act_ids=payload.act_ids,
        provider=payload.provider,
    )

    assistant = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=result.answer,
        citations=result.citations,
        risk_level=result.risk.get("level"),
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        latency_ms=result.total_ms,
    )
    session.add(assistant)
    session.add(
        QueryLog(
            request_id=request_id_ctx.get(),
            user_id=user.id if user else None,
            conversation_id=conversation.id,
            mode=payload.mode,
            query=payload.message,
            detected_language=result.language,
            retrieved_chunk_ids=result.retrieved_chunk_ids,
            citations=[c.get("citation") for c in result.citations],
            answered=result.answered,
            refusal_reason=result.refusal_reason,
            provider=result.provider,
            model=result.model,
            latency_ms=result.total_ms,
        )
    )
    await session.flush()

    return ChatResponse(
        conversation_id=conversation.id,
        message_id=assistant.id,
        **result.to_dict(),
    )


@router.post("/stream", dependencies=[Depends(rate_limit)])
async def chat_stream(
    payload: ChatRequest,
    user: User | None = Depends(get_optional_user),
):
    """Server-Sent Events stream.

    Event types: `meta`, `sources`, `delta`, `done`, `error`. The `done` event
    carries the *validated* answer — clients must replace the text they streamed
    with it, because validation can strip fabricated citations.

    This handler owns its own DB session: the request-scoped one would be closed
    by the time the generator runs.
    """
    request_id = request_id_ctx.get()

    async def event_stream() -> AsyncIterator[str]:
        async with SessionLocal() as session:
            try:
                language = payload.language or detect_language(payload.message)
                conversation = await _load_or_create_conversation(
                    session, payload.conversation_id, user, payload.mode, language, payload.message
                )
                history = await _history(session, conversation.id)
                session.add(
                    Message(
                        conversation_id=conversation.id, role="user", content=payload.message
                    )
                )
                await session.commit()

                yield _sse(
                    "meta",
                    {"conversation_id": str(conversation.id), "language": language.value},
                )

                final: dict | None = None
                async for event in reasoning_engine.stream(
                    session,
                    payload.message,
                    mode=payload.mode,
                    language=language,
                    history=history,
                    act_types=payload.act_types,
                    act_ids=payload.act_ids,
                    provider=payload.provider,
                ):
                    if event["type"] == "done":
                        final = event["result"]
                    yield _sse(event["type"], event)

                if final:
                    assistant = Message(
                        conversation_id=conversation.id,
                        role="assistant",
                        content=final["answer"],
                        citations=final["citations"],
                        risk_level=final["risk"].get("level"),
                        model=final.get("model"),
                        tokens_in=final["usage"]["tokens_in"],
                        tokens_out=final["usage"]["tokens_out"],
                        latency_ms=final["timings"]["total_ms"],
                    )
                    session.add(assistant)
                    session.add(
                        QueryLog(
                            request_id=request_id,
                            user_id=user.id if user else None,
                            conversation_id=conversation.id,
                            mode=payload.mode,
                            query=payload.message,
                            detected_language=final.get("language"),
                            citations=[c.get("citation") for c in final["citations"]],
                            answered=final.get("answered", True),
                            refusal_reason=final.get("refusal_reason"),
                            provider=final.get("provider"),
                            model=final.get("model"),
                            latency_ms=final["timings"]["total_ms"],
                        )
                    )
                    await session.commit()
                    yield _sse("saved", {"message_id": str(assistant.id)})
            except Exception as exc:
                log.exception("chat stream failed")
                await session.rollback()
                yield _sse("error", {"error": str(exc)})
            finally:
                yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx must not buffer SSE
        },
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(get_optional_user),
    limit: int = 50,
):
    if user is None:
        return []
    rows = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


@router.get("/conversations/{conversation_id}", response_model=list[MessageOut])
async def get_messages(
    conversation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(get_optional_user),
):
    conversation = (
        await session.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    if conversation.user_id and (user is None or conversation.user_id != user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your conversation")

    rows = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return list(rows.scalars())


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
