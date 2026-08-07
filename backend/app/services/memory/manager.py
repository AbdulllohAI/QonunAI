"""Multi-layer memory system for QonunAI.

Layers
------
1. Short-term  — conversation history (HISTORY_TURNS in chat.py, already exists)
2. Long-term   — UserProfile: style + topic preferences per authenticated user
3. Semantic    — SemanticMemory: embedded past Q&A pairs, searched by cosine similarity

Only authenticated users get persistent memory. Anonymous sessions use only short-term
history that expires with the conversation.

Retrieval pipeline (called at query time)
------------------------------------------
1. Load UserProfile (single row read, cached by user_id)
2. Embed the incoming question with the running bge-m3 model
3. Top-3 cosine-nearest SemanticMemory rows whose weight > 0.1
4. Return a MemoryContext that the engine injects into the prompt

Recording pipeline (called after each answered query)
------------------------------------------------------
1. Update UserProfile: increment query_count, update style hint, merge topics
2. Decide whether to store a semantic memory (see _is_worth_storing)
3. Embed the question, store SemanticMemory row
4. Apply inline decay to very old memories (prune weight < 0.05)

Decay model
-----------
weight starts at 1.0 and decays 5% per week:
    weight_effective = weight * 0.95 ^ weeks_since_creation

Applied at read time — no background task needed. Rows below 0.1 are skipped.
Rows below 0.05 are deleted during record() to keep the table lean.
"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import SemanticMemory, UserProfile
from app.services.rag.embedder import embedder

log = get_logger(__name__)

_TOPIC_KEYWORDS = re.compile(
    r"\b(mehnat|labour|labor|труд|ishchi|работник|employee|employer"
    r"|shartnoma|договор|contract|agreement"
    r"|jinoyat|уголовн|criminal|crime"
    r"|fuqarolik|гражданск|civil"
    r"|soliq|налог|tax|fiscal"
    r"|oila|семья|family|nikoh|брак|marriage|divorce|talaq|развод"
    r"|mulk|собственн|property|ер|земля|land"
    r"|tadbirkor|предприним|entrepreneur|business|корхона|предприятие"
    r"|jazo|наказани|punishment|sentence|penalty"
    r"|sud|суд|court|judge|judge"
    r"|ijara|аренда|lease|rent"
    r"|iste|увольнен|dismissal|termination|fired"
    r"|ish beruvchi|работодатель|employer"
    r"|patent|intellekt|intellectual|copyright|авторск"
    r"|import|eksport|export|customs|bojxona|таможн"
    r"|bank|kredit|кредит|credit|loan"
    r"|pensiya|пенсия|pension|retirement"
    r"|bola|ребён|child|minor|несоверш|yosh)\b",
    re.IGNORECASE,
)

_MIN_WEIGHT = 0.1
_PRUNE_WEIGHT = 0.05
_MAX_SEMANTIC_MEMORIES = 200   # per user — prune oldest when exceeded
_TOPIC_CAP = 20                # max topics stored in profile
_MEMORY_THRESHOLD_CHARS = 200  # minimum answer length to be worth storing
_WEEKS_HALF_LIFE = 14          # weight halves every 14 weeks (~3 months)


@dataclass
class MemoryContext:
    profile: UserProfile | None = None
    memories: list[SemanticMemory] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.profile is None and not self.memories

    def to_prompt_block(self) -> str:
        """Format memory into a compact block for injection into the user turn."""
        if self.is_empty:
            return ""

        lines: list[str] = ["USER CONTEXT", "============"]

        if self.profile:
            if self.profile.last_language:
                lines.append(f"Preferred language: {self.profile.last_language}")
            if self.profile.preferred_style != "auto":
                style_label = "concise answers" if self.profile.preferred_style == "concise" else "detailed answers"
                lines.append(f"Answer style: {style_label}")
            if self.profile.topics:
                lines.append(f"Known topics: {', '.join(self.profile.topics[:8])}")
            if self.profile.query_count > 1:
                lines.append(f"Past queries: {self.profile.query_count}")

        if self.memories:
            lines.append("")
            lines.append("Relevant past context (do not repeat unless asked):")
            for i, m in enumerate(self.memories, 1):
                lines.append(f"  [{i}] Q: {m.question[:120]}")
                lines.append(f"      A: {m.answer_summary[:200]}")

        lines.append("")
        return "\n".join(lines)


def _effective_weight(memory: SemanticMemory) -> float:
    """Apply time-decay to the stored weight."""
    now = datetime.now(timezone.utc)
    created = memory.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    weeks = (now - created).total_seconds() / (7 * 24 * 3600)
    decay = math.exp(-math.log(2) * weeks / _WEEKS_HALF_LIFE)
    return memory.weight * decay


def _extract_topics(text: str) -> list[str]:
    """Pull legal topic keywords from answer text."""
    found = {m.group(0).lower() for m in _TOPIC_KEYWORDS.finditer(text)}
    return sorted(found)[:_TOPIC_CAP]


def _is_worth_storing(answer: str, answered: bool) -> bool:
    """Only store meaningful, answered interactions."""
    return answered and len(answer.strip()) >= _MEMORY_THRESHOLD_CHARS


def _answer_summary(answer: str) -> str:
    """First 350 chars of answer, stripped of markdown syntax."""
    clean = re.sub(r"[#*`\[\]_~>]", "", answer)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:350]


class MemoryManager:
    async def get_context(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        question: str,
    ) -> MemoryContext:
        """Retrieve profile + top-3 semantically similar memories."""
        profile_row = (
            await session.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
        ).scalar_one_or_none()

        memories: list[SemanticMemory] = []
        try:
            q_embedding = await embedder.embed_query(question)

            # pgvector cosine-distance ORDER BY — lower = more similar.
            rows = list(
                (
                    await session.execute(
                        select(SemanticMemory)
                        .where(SemanticMemory.user_id == user_id)
                        .where(SemanticMemory.weight > _MIN_WEIGHT)
                        .order_by(
                            SemanticMemory.embedding.cosine_distance(q_embedding)
                        )
                        .limit(5)
                    )
                ).scalars()
            )

            # Apply time-decay filter inline and pick top 3.
            for row in rows:
                if _effective_weight(row) >= _MIN_WEIGHT:
                    memories.append(row)
                if len(memories) == 3:
                    break

            # Update access metadata (fire-and-forget, don't block on flush).
            if memories:
                ids = [m.id for m in memories]
                await session.execute(
                    text(
                        "UPDATE semantic_memories "
                        "SET access_count = access_count + 1, last_accessed_at = now() "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": ids},
                )
        except Exception:
            log.exception("semantic memory retrieval failed — continuing without it")

        return MemoryContext(profile=profile_row, memories=memories)

    async def record(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        question: str,
        answer: str,
        language: str,
        compact: bool,
        answered: bool,
    ) -> None:
        """Update profile and optionally store a semantic memory."""
        try:
            await self._update_profile(session, user_id, answer, language, compact)

            if _is_worth_storing(answer, answered):
                await self._store_memory(session, user_id, question, answer, language)

            # Prune decayed memories to keep the table lean.
            await self._prune(session, user_id)
        except Exception:
            log.exception("memory recording failed — continuing without it")

    async def clear(self, session: AsyncSession, user_id: uuid.UUID) -> dict:
        """Delete all memory for a user (privacy control)."""
        deleted_mem = await session.execute(
            delete(SemanticMemory).where(SemanticMemory.user_id == user_id)
        )
        profile = (
            await session.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
        ).scalar_one_or_none()
        if profile:
            profile.topics = []
            profile.query_count = 0
            profile.preferred_style = "auto"
        await session.flush()
        return {
            "memories_deleted": deleted_mem.rowcount,
            "profile_reset": profile is not None,
        }

    # ---------------------------------------------------------------- private

    async def _update_profile(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        answer: str,
        language: str,
        compact: bool,
    ) -> None:
        profile = (
            await session.execute(
                select(UserProfile).where(UserProfile.user_id == user_id)
            )
        ).scalar_one_or_none()

        if profile is None:
            profile = UserProfile(user_id=user_id)
            session.add(profile)

        profile.query_count = (profile.query_count or 0) + 1
        profile.last_language = language

        # Style hint: compact requests signal a preference for concise answers.
        if compact and profile.preferred_style == "auto":
            profile.preferred_style = "concise"
        elif not compact and profile.preferred_style == "concise" and profile.query_count > 5:
            # If the user has used detailed mode frequently, reset to auto.
            profile.preferred_style = "auto"

        # Merge new topics into the profile list (capped at _TOPIC_CAP).
        new_topics = _extract_topics(answer)
        existing = set(profile.topics or [])
        merged = list(existing | set(new_topics))[:_TOPIC_CAP]
        profile.topics = merged

        await session.flush()

    async def _store_memory(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        question: str,
        answer: str,
        language: str,
    ) -> None:
        q_embedding = await embedder.embed_query(question)
        memory = SemanticMemory(
            user_id=user_id,
            question=question[:500],
            answer_summary=_answer_summary(answer),
            language=language,
            embedding=q_embedding,
        )
        session.add(memory)
        await session.flush()

        # Cap per-user memories at _MAX_SEMANTIC_MEMORIES (delete oldest).
        count_row = await session.execute(
            text("SELECT COUNT(*) FROM semantic_memories WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        count = count_row.scalar() or 0
        if count > _MAX_SEMANTIC_MEMORIES:
            excess = count - _MAX_SEMANTIC_MEMORIES
            oldest = list(
                (
                    await session.execute(
                        select(SemanticMemory.id)
                        .where(SemanticMemory.user_id == user_id)
                        .order_by(SemanticMemory.created_at)
                        .limit(excess)
                    )
                ).scalars()
            )
            if oldest:
                await session.execute(
                    delete(SemanticMemory).where(SemanticMemory.id.in_(oldest))
                )

    async def _prune(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        """Delete memories that have decayed beyond the prune threshold."""
        now = datetime.now(timezone.utc)
        # Calculate age cutoff: weight 1.0 * decay^weeks < PRUNE_WEIGHT
        # => decay^weeks < PRUNE_WEIGHT => weeks > log(PRUNE_WEIGHT) / log(decay)
        # With half-life of _WEEKS_HALF_LIFE: decay per week = 0.5^(1/HL)
        # Simplification: prune rows older than 2 years (covers < 0.05 easily).
        cutoff_weeks = -math.log(_PRUNE_WEIGHT / 1.0) / math.log(2) * _WEEKS_HALF_LIFE
        cutoff_seconds = cutoff_weeks * 7 * 24 * 3600
        await session.execute(
            text(
                "DELETE FROM semantic_memories "
                "WHERE user_id = :uid "
                "AND EXTRACT(EPOCH FROM (now() - created_at)) > :cutoff"
            ),
            {"uid": str(user_id), "cutoff": cutoff_seconds},
        )


memory_manager = MemoryManager()
