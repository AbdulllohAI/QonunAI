"""Turn retrieved chunks into a citation-anchored prompt context.

Two design rules drive everything here:

1. **Every passage carries a stable `[S<n>]` source tag.** The model is told to
   cite only those tags, and `app.services.reasoning.validator` rejects any
   answer citing a tag that was not supplied. That closed loop is what makes
   "only answer from retrieved sources" enforceable rather than aspirational.
2. **Passages are grouped by legal force.** The reasoning engine must resolve
   conflicts Constitution → Code → Law → Decree, and it does that far more
   reliably when the context is already laid out in that order and labelled.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.core.config import settings
from app.db.models import ActType, Language
from app.services.rag.types import RetrievedChunk

# Chars-per-token varies sharply by script: Latin legal text runs ~3.5-4
# chars/token in Llama's BPE vocabulary, but Cyrillic — most of this corpus —
# tokenizes far denser, closer to 1.6-1.8. A flat 3.0 ratio underestimates
# Cyrillic-heavy context by ~1.7x, which is exactly what was blowing past
# Groq's 12k TPM cap (see RAG_MAX_CONTEXT_TOKENS) while still reporting
# "6000 tokens used" in this budget.
_CHARS_PER_TOKEN_LATIN = 3.5
_CHARS_PER_TOKEN_CYRILLIC = 1.7
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")

_TIER_LABELS = {
    ActType.CONSTITUTION: "CONSTITUTION (highest legal force)",
    ActType.CONSTITUTIONAL_LAW: "CONSTITUTIONAL LAW",
    ActType.CODE: "CODE",
    ActType.LAW: "LAW",
    ActType.PRESIDENTIAL_DECREE: "PRESIDENTIAL DECREE (Farmon)",
    ActType.PRESIDENTIAL_RESOLUTION: "PRESIDENTIAL RESOLUTION (Qaror)",
    ActType.CABINET_RESOLUTION: "CABINET OF MINISTERS RESOLUTION",
    ActType.MINISTERIAL_ACT: "MINISTERIAL / AGENCY ACT",
    ActType.LOCAL_ACT: "LOCAL ACT",
    ActType.COURT_DECISION: "COURT DECISION (persuasive, not a source of law)",
    ActType.COMMENTARY: "LEGAL COMMENTARY (doctrinal, not binding)",
}


@dataclass(slots=True)
class BuiltContext:
    text: str
    sources: list["SourceRef"]
    truncated: bool = False

    @property
    def valid_tags(self) -> set[str]:
        return {s.tag for s in self.sources}

    @property
    def is_empty(self) -> bool:
        return not self.sources


@dataclass(slots=True)
class SourceRef:
    tag: str
    chunk: RetrievedChunk

    def to_citation_dict(self) -> dict:
        c = self.chunk
        return {
            "tag": self.tag,
            "chunk_id": str(c.chunk_id),
            "act_id": str(c.act_id),
            "law_name": c.law_name,
            "article_number": c.article_number,
            "act_type": c.act_type.value,
            "citation": c.citation,
            "language": c.language.value,
            "hierarchy_path": c.hierarchy_path,
            "date_of_adoption": c.date_of_adoption.isoformat() if c.date_of_adoption else None,
            "last_updated": c.last_updated.isoformat() if c.last_updated else None,
            "source_url": c.source_url,
            "score": round(c.score, 4),
            "supporting": c.via_crossref_from is not None,
        }


class ContextBuilder:
    def __init__(self, max_context_tokens: int | None = None) -> None:
        # Defaults from settings rather than a hardcoded literal: a fixed
        # budget with no regard for the configured model's actual per-minute
        # token limit works fine on a generous tier and silently 413s on a
        # constrained one (e.g. Groq's free "on_demand" tier caps at 12k TPM
        # total, and the system prompt + question eat into that budget too).
        self.max_context_tokens = max_context_tokens or settings.RAG_MAX_CONTEXT_TOKENS

    def build(
        self, chunks: Sequence[RetrievedChunk], *, answer_language: Language
    ) -> BuiltContext:
        if not chunks:
            return BuiltContext(text="", sources=[])

        primary = [c for c in chunks if c.via_crossref_from is None]
        supporting = [c for c in chunks if c.via_crossref_from is not None]

        sources: list[SourceRef] = []
        blocks: list[str] = []
        budget = self.max_context_tokens
        truncated = False

        # --- primary passages, grouped by legal force ------------------------
        for act_type in sorted(
            {c.act_type for c in primary}, key=lambda t: t.precedence, reverse=True
        ):
            tier = [c for c in primary if c.act_type is act_type]
            header = f"\n### {_TIER_LABELS.get(act_type, act_type.value.upper())}\n"
            if budget - self._tokens(header) <= 0:
                truncated = True
                break
            blocks.append(header)
            budget -= self._tokens(header)

            for chunk in tier:
                tag = f"S{len(sources) + 1}"
                block = self._render(tag, chunk)
                cost = self._tokens(block)
                if cost > budget:
                    truncated = True
                    break
                blocks.append(block)
                budget -= cost
                sources.append(SourceRef(tag=tag, chunk=chunk))

        # --- cross-referenced supporting passages ----------------------------
        if supporting and budget > 500:
            header = "\n### CROSS-REFERENCED PROVISIONS (cited by the passages above)\n"
            blocks.append(header)
            budget -= self._tokens(header)
            for chunk in supporting:
                tag = f"S{len(sources) + 1}"
                block = self._render(tag, chunk)
                cost = self._tokens(block)
                if cost > budget:
                    truncated = True
                    break
                blocks.append(block)
                budget -= cost
                sources.append(SourceRef(tag=tag, chunk=chunk))

        return BuiltContext(text="".join(blocks).strip(), sources=sources, truncated=truncated)

    @staticmethod
    def _render(tag: str, chunk: RetrievedChunk) -> str:
        lines = [f"\n[{tag}] {chunk.citation}"]
        if chunk.heading:
            lines.append(f"Heading: {chunk.heading}")
        if chunk.hierarchy_path:
            lines.append(f"Location: {chunk.hierarchy_path}")
        meta: list[str] = [f"Language: {chunk.language.value}"]
        if chunk.date_of_adoption:
            meta.append(f"Adopted: {chunk.date_of_adoption.isoformat()}")
        if chunk.last_updated:
            meta.append(f"Last updated: {chunk.last_updated.isoformat()}")
        lines.append(" | ".join(meta))
        if chunk.via_crossref_from:
            lines.append(f"(Included because {chunk.via_crossref_from} refers to it.)")
        lines.append(f"Text: {chunk.text.strip()}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _tokens(text: str) -> int:
        if not text:
            return 1
        cyrillic_chars = len(_CYRILLIC_RE.findall(text))
        cyrillic_ratio = cyrillic_chars / len(text)
        # Blend the two ratios by how much of the text is actually Cyrillic,
        # rather than a hard threshold — mixed uz-Cyrl/citation text is common.
        chars_per_token = (
            cyrillic_ratio * _CHARS_PER_TOKEN_CYRILLIC
            + (1 - cyrillic_ratio) * _CHARS_PER_TOKEN_LATIN
        )
        return int(len(text) / chars_per_token) + 1


context_builder = ContextBuilder()
