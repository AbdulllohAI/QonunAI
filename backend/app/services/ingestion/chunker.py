"""Article-aware chunking.

Generic fixed-window chunking is actively harmful for statutes: it splits an
article mid-sentence, so a retrieved chunk can state a rule without its
exception, and the citation metadata becomes ambiguous. The strategy here is:

* **One article = one chunk** whenever it fits. This is the natural retrieval and
  citation unit and keeps `article_number` unambiguous.
* **Long articles split on clause boundaries** (band / qismcha), never mid-clause,
  with the article heading repeated on every part so each chunk remains
  self-describing.
* **A single oversized clause** is split on sentence boundaries as a last resort,
  with overlap so a rule spanning the seam is still retrievable.
* **Tiny structural nodes** (a bare chapter heading) are merged forward rather
  than indexed alone — they retrieve well and answer nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.config import settings
from app.db.models import Language, NodeType
from app.services.ingestion.hierarchy_builder import BuiltNode
from app.services.lang.translit import normalize

# Uzbek/Russian legal prose averages ~3 chars/token under multilingual tokenisers.
_CHARS_PER_TOKEN = 3.0

_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+(?=[A-ZА-ЯЎҚҒҲ0-9«\"])")
_CLAUSE_SPLIT = re.compile(r"\n(?=\s*(?:\d+\s*[).]|[a-zа-я]\s*\)))")


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


@dataclass(slots=True)
class ChunkDraft:
    text: str
    node_id: object | None
    article_number: str | None
    heading: str | None
    hierarchy_path: str
    language: Language
    ordinal: int
    token_count: int = 0
    meta: dict = field(default_factory=dict)


class LegalChunker:
    def __init__(
        self,
        target_tokens: int | None = None,
        max_tokens: int | None = None,
        overlap_tokens: int | None = None,
    ) -> None:
        self.target = target_tokens or settings.CHUNK_TARGET_TOKENS
        self.max = max_tokens or settings.CHUNK_MAX_TOKENS
        self.overlap = overlap_tokens or settings.CHUNK_OVERLAP_TOKENS

    def chunk_nodes(self, nodes: list[BuiltNode]) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        ordinal = 0
        pending_heading: list[str] = []

        for node in nodes:
            body = normalize(node.body)
            heading = normalize(node.heading or "")

            # Structural nodes with no text of their own: carry their heading
            # forward so the next article inherits "Chapter I. Duties of..." as
            # context instead of losing it.
            if not body and node.node_type in (NodeType.QISM, NodeType.BOLIM, NodeType.BOB):
                if heading:
                    pending_heading.append(f"{_label(node)}{heading}")
                continue

            if not body and not heading:
                continue

            context_prefix = " / ".join(pending_heading[-3:])
            full_heading = _compose_heading(node, heading)

            # The context prefix and heading are prepended to every part, so
            # they must come out of the split budget — otherwise a long chapter
            # title silently pushes chunks over CHUNK_MAX_TOKENS.
            overhead = estimate_tokens(_assemble(context_prefix, full_heading, ""))

            for part in self._split_body(body, overhead=overhead):
                text = _assemble(context_prefix, full_heading, part)
                drafts.append(
                    ChunkDraft(
                        text=text,
                        node_id=node.id,
                        article_number=node.article_number,
                        heading=full_heading or None,
                        hierarchy_path=node.path,
                        language=node.language,
                        ordinal=ordinal,
                        token_count=estimate_tokens(text),
                        meta={"node_type": node.node_type.value},
                    )
                )
                ordinal += 1

            if node.node_type is NodeType.MODDA:
                # A new article ends the chapter-heading carry-over chain only
                # when the next structural node resets it, so leave it in place.
                pass

        return drafts

    # ------------------------------------------------------------- splitting
    def _split_body(self, body: str, *, overhead: int = 0) -> list[str]:
        """Split `body` so that each part plus `overhead` fits within `max`."""
        if not body:
            return []
        # Never let a huge heading starve the body budget entirely.
        budget_max = max(self.max - overhead, self.max // 4)
        budget_target = max(self.target - overhead, budget_max // 2)

        if estimate_tokens(body) <= budget_max:
            return [body]

        # 1. Clause boundaries.
        parts = [p.strip() for p in _CLAUSE_SPLIT.split(body) if p.strip()]
        if len(parts) > 1:
            return self._pack(parts, budget_target, budget_max)

        # 2. Paragraph boundaries.
        parts = [p.strip() for p in body.split("\n") if p.strip()]
        if len(parts) > 1:
            return self._pack(parts, budget_target, budget_max)

        # 3. Sentence boundaries, with overlap.
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(body) if s.strip()]
        if len(sentences) > 1:
            return self._pack(sentences, budget_target, budget_max, overlap=True)

        # 4. Hard character split — a single unpunctuated wall of text.
        return self._hard_split(body, budget_max)

    def _pack(
        self, parts: list[str], target: int, maximum: int, *, overlap: bool = False
    ) -> list[str]:
        """Greedily fill chunks up to `target`, never exceeding `maximum`."""
        out: list[str] = []
        buf: list[str] = []
        buf_tokens = 0

        for part in parts:
            part_tokens = estimate_tokens(part)

            if part_tokens > maximum:
                if buf:
                    out.append("\n".join(buf))
                    buf, buf_tokens = [], 0
                out.extend(self._hard_split(part, maximum))
                continue

            if buf and buf_tokens + part_tokens > target:
                out.append("\n".join(buf))
                if overlap and buf:
                    tail = buf[-1]
                    buf = [tail] if estimate_tokens(tail) <= self.overlap else []
                    buf_tokens = estimate_tokens(tail) if buf else 0
                else:
                    buf, buf_tokens = [], 0

            buf.append(part)
            buf_tokens += part_tokens

        if buf:
            out.append("\n".join(buf))
        return out

    def _hard_split(self, text: str, maximum: int | None = None) -> list[str]:
        window = int((maximum or self.max) * _CHARS_PER_TOKEN)
        step = window - int(self.overlap * _CHARS_PER_TOKEN)
        return [text[i : i + window] for i in range(0, len(text), max(step, 1))]


def _label(node: BuiltNode) -> str:
    if not node.number:
        return ""
    names = {
        NodeType.QISM: "Qism",
        NodeType.BOLIM: "Bo‘lim",
        NodeType.BOB: "Bob",
        NodeType.MODDA: "Modda",
    }
    return f"{names.get(node.node_type, '')} {node.number}. "


def _compose_heading(node: BuiltNode, heading: str) -> str:
    if node.node_type is NodeType.MODDA and node.number:
        return f"{node.number}-modda. {heading}".strip().rstrip(".")
    return heading


def _assemble(context_prefix: str, heading: str, body: str) -> str:
    """Every chunk repeats its structural context — chunks are retrieved in
    isolation, so a body fragment with no article label is unusable."""
    parts = [p for p in (context_prefix, heading, body) if p]
    return "\n".join(parts).strip()


legal_chunker = LegalChunker()
