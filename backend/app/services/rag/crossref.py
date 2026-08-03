"""Cross-reference extraction and context expansion.

Uzbek statutes lean heavily on internal references — an article on contract
liability will say "in the cases provided for by Article 333 of this Code"
without restating the rule. Answering from the retrieved article alone therefore
produces confidently incomplete advice. This module (a) mines those references
at ingest time and (b) pulls the referenced articles into context at query time.
"""
from __future__ import annotations

import re
import uuid
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import ActStatus, Chunk, CrossReference, Language, LegalAct, RefKind
from app.services.ingestion.anchors import build_deep_link
from app.services.rag.types import RetrievedChunk

log = get_logger(__name__)

# "Article 333 of this Code" / "ushbu Kodeksning 333-moddasi" /
# "статьей 333 настоящего Кодекса" — and the same forms naming another act.
_SELF_REF = [
    re.compile(r"ushbu\s+Kodeks\w*\s+(\d+(?:-\d+)?)[-–]?\s*modda", re.IGNORECASE),
    re.compile(r"(\d+(?:-\d+)?)[-–]\s*modda\w*\s+ushbu\s+Kodeks", re.IGNORECASE),
    # Uzbek Cyrillic — distinct from Russian, and lex.uz serves many acts in it.
    re.compile(r"ушбу\s+Кодекс\w*\s+(\d+(?:-\d+)?)[-–]?\s*модда", re.IGNORECASE),
    re.compile(r"(\d+(?:-\d+)?)[-–]\s*модда\w*\s+ушбу\s+Кодекс", re.IGNORECASE),
    re.compile(r"стать[её]?\w*\s+(\d+(?:-\d+)?)\s+настоящего\s+Кодекса", re.IGNORECASE),
    re.compile(r"настоящего\s+Кодекса\s+стать[её]?\w*\s+(\d+(?:-\d+)?)", re.IGNORECASE),
    re.compile(r"Article\s+(\d+(?:-\d+)?)\s+of\s+this\s+Code", re.IGNORECASE),
]

_EXTERNAL_REF = [
    # "Fuqarolik kodeksining 54-moddasi"
    re.compile(
        r"([A-ZА-ЯЎҚҒҲ][\w’'ʼ\s]{3,60}?(?:kodeks\w*|qonun\w*))\w*\s+(\d+(?:-\d+)?)[-–]?\s*modda",
        re.IGNORECASE,
    ),
    # "статья 54 Гражданского кодекса"
    re.compile(
        r"стать[её]?\w*\s+(\d+(?:-\d+)?)\s+([А-ЯЎҚҒҲ][\w\s]{3,60}?(?:кодекса|закона))",
        re.IGNORECASE,
    ),
    # "Article 54 of the Civil Code"
    re.compile(
        r"Article\s+(\d+(?:-\d+)?)\s+of\s+(?:the\s+)?([A-Z][\w\s]{3,60}?(?:Code|Law))",
        re.IGNORECASE,
    ),
]


def extract_references(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (self_article_numbers, [(act_name, article_number), ...])."""
    self_refs: list[str] = []
    for pattern in _SELF_REF:
        for match in pattern.finditer(text):
            num = match.group(1)
            if num not in self_refs:
                self_refs.append(num)

    external: list[tuple[str, str]] = []
    for pattern in _EXTERNAL_REF:
        for match in pattern.finditer(text):
            g1, g2 = match.group(1).strip(), match.group(2).strip()
            # Group order differs per pattern; the numeric one is the article.
            act_name, article = (g1, g2) if g2.replace("-", "").isdigit() else (g2, g1)
            pair = (re.sub(r"\s+", " ", act_name), article)
            if pair not in external and article.replace("-", "").isdigit():
                external.append(pair)

    return self_refs, external


async def persist_references(
    session: AsyncSession, act: LegalAct, article_number: str | None, body: str
) -> int:
    """Record edges found in `body`. External targets are stored unresolved when
    the referenced act is not yet ingested, so a later pass can resolve them."""
    self_refs, external = extract_references(body)
    written = 0

    for target in self_refs:
        session.add(
            CrossReference(
                kind=RefKind.CITES,
                source_act_id=act.id,
                source_article=article_number,
                target_act_id=act.id,
                target_article=target,
                confidence=0.95,
            )
        )
        written += 1

    for act_name, target_article in external:
        resolved = await _resolve_act(session, act_name)
        session.add(
            CrossReference(
                kind=RefKind.CITES,
                source_act_id=act.id,
                source_article=article_number,
                target_act_id=resolved.id if resolved else None,
                target_article=target_article,
                target_raw=f"{act_name} — {target_article}",
                confidence=0.8 if resolved else 0.4,
            )
        )
        written += 1

    return written


async def _resolve_act(session: AsyncSession, name: str) -> LegalAct | None:
    from app.services.rag.keyword import keyword_searcher

    matches = await keyword_searcher.find_acts_by_name(session, name, limit=1)
    return matches[0] if matches else None


async def expand_cross_references(
    session: AsyncSession,
    seeds: Sequence[RetrievedChunk],
    *,
    limit: int,
    languages: Sequence[Language],
    exclude: set[uuid.UUID] | None = None,
) -> list[RetrievedChunk]:
    """Fetch articles that the top hits point to, marked as supporting context."""
    if not seeds or limit <= 0:
        return []
    exclude = exclude or set()

    edge_stmt = select(CrossReference).where(
        CrossReference.source_act_id.in_({s.act_id for s in seeds}),
        CrossReference.source_article.in_({s.article_number for s in seeds if s.article_number}),
        CrossReference.target_act_id.isnot(None),
        CrossReference.target_article.isnot(None),
        CrossReference.confidence >= 0.5,
    )
    edges = list((await session.execute(edge_stmt)).scalars())
    if not edges:
        return []

    seed_by_key = {(s.act_id, s.article_number): s for s in seeds}
    wanted: dict[tuple[uuid.UUID, str], str] = {}
    for edge in edges[: limit * 3]:
        key = (edge.target_act_id, edge.target_article)
        if key not in wanted:
            origin = seed_by_key.get((edge.source_act_id, edge.source_article))
            wanted[key] = origin.citation if origin else "a retrieved article"

    if not wanted:
        return []

    # Prefer the language(s) already in play so the context stays coherent.
    lang_order = {lang: i for i, lang in enumerate(languages)}
    stmt = (
        select(Chunk, LegalAct)
        .join(LegalAct, LegalAct.id == Chunk.act_id)
        .where(
            Chunk.act_id.in_({a for a, _ in wanted}),
            Chunk.article_number.in_({n for _, n in wanted}),
            Chunk.language.in_(list(languages)),
            LegalAct.status.in_([ActStatus.IN_FORCE, ActStatus.AMENDED]),
        )
        .limit(limit * 4)
    )
    rows = (await session.execute(stmt)).all()

    seen: set[tuple[uuid.UUID, str | None]] = set()
    out: list[RetrievedChunk] = []
    for chunk, act in sorted(rows, key=lambda r: lang_order.get(r[0].language, 99)):
        key = (chunk.act_id, chunk.article_number)
        if key not in wanted or chunk.id in exclude or key in seen:
            continue
        seen.add(key)
        out.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                act_id=chunk.act_id,
                text=chunk.text,
                law_name=chunk.law_name,
                article_number=chunk.article_number,
                act_type=chunk.act_type,
                language=chunk.language,
                hierarchy_path=chunk.hierarchy_path,
                heading=chunk.heading,
                date_of_adoption=chunk.date_of_adoption,
                last_updated=chunk.last_updated,
                source_url=build_deep_link(chunk.source_url or act.source_url, chunk.lexuz_anchor_id),
                act_status=act.status.value if act.status else None,
                fused_score=0.3,
                via_crossref_from=wanted[key],
            )
        )
        if len(out) >= limit:
            break
    return out
