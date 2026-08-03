"""Lexical retrieval.

Two complementary signals:

1. **Full-text search** over a per-language `tsvector`. Postgres `ts_rank_cd` is a
   cover-density score, not textbook BM25, but it is the right production choice
   here: it is index-backed, incremental, and multilingual. (`rank_bm25` is
   available as an in-memory fallback for the FAISS/offline profile — it needs
   the whole corpus resident, which a national legal corpus is not.)
2. **Direct article lookup.** Legal queries very often name their target
   ("Article 54 of the Civil Code", "ЖК 105-модда"). A regex extracts those and
   fetches the article outright — dense retrieval alone is unreliable at
   pinpointing an exact article number.
"""
from __future__ import annotations

import re
import uuid
from typing import Sequence

from sqlalchemy import Select, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import ActStatus, ActType, Chunk, LegalAct, Language
from app.services.lang.translit import cyrillic_to_latin, normalize, script_variants
from app.services.rag.query_prep import content_tokens, stem_variants

log = get_logger(__name__)

# "Article 54", "54-modda", "modda 54", "статья 54", "ст. 54", "54-moddasi"
_ARTICLE_PATTERNS = [
    re.compile(r"\barticle\s+(\d+(?:[-–]\d+)?)", re.IGNORECASE),
    re.compile(r"\b(\d+(?:[-–]\d+)?)\s*[-–]?\s*modda", re.IGNORECASE),
    re.compile(r"\bmodda\s*[-–]?\s*(\d+(?:[-–]\d+)?)", re.IGNORECASE),
    re.compile(r"\bстать[яеиюй]\s*(\d+(?:[-–]\d+)?)", re.IGNORECASE),
    re.compile(r"\bст\.?\s*(\d+(?:[-–]\d+)?)", re.IGNORECASE),
    # Uzbek Cyrillic: "105-модда" and "модда 105".
    re.compile(r"\b(\d+(?:[-–]\d+)?)\s*[-–]?\s*модда", re.IGNORECASE),
    re.compile(r"\bмодда\s*[-–]?\s*(\d+(?:[-–]\d+)?)", re.IGNORECASE),
]

# Maps free-text act mentions onto ActType. Latin, Cyrillic and English forms.
_ACT_HINTS: list[tuple[re.Pattern[str], ActType]] = [
    (re.compile(r"konstitutsiya|конституц|constitution", re.I), ActType.CONSTITUTION),
    (re.compile(r"fuqarolik\s+kodeks|граждан\w*\s+кодекс|civil\s+code", re.I), ActType.CODE),
    (re.compile(r"jinoyat\s+kodeks|уголовн\w*\s+кодекс|criminal\s+code", re.I), ActType.CODE),
    (re.compile(r"mehnat\s+kodeks|трудов\w*\s+кодекс|labou?r\s+code", re.I), ActType.CODE),
    (re.compile(r"soliq\s+kodeks|налогов\w*\s+кодекс|tax\s+code", re.I), ActType.CODE),
    (re.compile(r"ma[’'ʼ]?muriy\s+javobgarlik|админист\w*\s+кодекс|administrative\s+code", re.I), ActType.CODE),
    (re.compile(r"\bkodeks|\bкодекс|\bcode\b", re.I), ActType.CODE),
    (re.compile(r"\bfarmon|\bуказ|\bdecree", re.I), ActType.PRESIDENTIAL_DECREE),
    (re.compile(r"vazirlar\s+mahkamasi|кабинет\s+министров|cabinet", re.I), ActType.CABINET_RESOLUTION),
    # Deliberately no generic "qonun"/"закон"/"law" hint: unlike the specific
    # act names above, that's the ordinary word people use to mean
    # "legislation" in general — it appears constantly in normal legal
    # phrasing ("against the law of...", "qonun asosida...") and, read as a
    # request for ActType.LAW specifically, silently zeroed every retrieval
    # whose query happened to contain it (verified: this corpus has zero
    # ActType.LAW acts, so the filter matched nothing, every time).
]


def extract_article_numbers(query: str) -> list[str]:
    """Article numbers in the order they appear in the query.

    Document order matters: when a question names several articles, the first is
    usually the primary subject and the rest are context. Collecting per-pattern
    would instead order by which regex happened to match first.
    """
    hits: list[tuple[int, str]] = []
    for pattern in _ARTICLE_PATTERNS:
        for match in pattern.finditer(query):
            hits.append((match.start(), match.group(1).replace("–", "-")))

    found: list[str] = []
    for _, num in sorted(hits, key=lambda h: h[0]):
        if num not in found:
            found.append(num)
    return found


def infer_act_types(query: str) -> list[ActType]:
    hits: list[ActType] = []
    for pattern, act_type in _ACT_HINTS:
        if pattern.search(query) and act_type not in hits:
            hits.append(act_type)
    return hits


def build_tsquery(query: str, language: Language) -> tuple[str, str]:
    """Return (pg_config, tsquery_string) using OR-of-prefix terms.

    Prefix matching (`term:*`) matters for Uzbek and Russian: both are heavily
    agglutinative/inflected and the 'simple' dictionary does no stemming, so an
    exact-token query misses "shartnomani" when the user typed "shartnoma".
    """
    config = language.pg_text_config
    # Postgres stems Russian and English itself; for those, adding our own
    # truncated variants would only add noise. Uzbek uses 'simple' (no stemmer),
    # which is where the suffix problem lives.
    needs_stemming = config == "simple"

    tokens: list[str] = []
    for variant in script_variants(query):
        for raw in content_tokens(variant, language.value):
            forms = stem_variants(raw) if needs_stemming else [raw]
            for token in forms:
                if token and token not in tokens:
                    tokens.append(token)
    if not tokens:
        tokens = [normalize(query).lower() or "x"]
    return config, " | ".join(f"{t}:*" for t in tokens[:60])


class KeywordSearcher:
    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        top_k: int,
        language: Language,
        languages: Sequence[Language] | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        in_force_only: bool = True,
    ) -> list[tuple[Chunk, LegalAct, float]]:
        config, tsquery = build_tsquery(query, language)
        ts = func.to_tsquery(config, tsquery)
        rank = func.ts_rank_cd(Chunk.search_vector, ts, 32).label("rank")

        stmt: Select = (
            select(Chunk, LegalAct, rank)
            .join(LegalAct, LegalAct.id == Chunk.act_id)
            .where(Chunk.search_vector.op("@@")(ts))
        )
        stmt = self._filters(stmt, languages, act_types, act_ids, in_force_only)
        stmt = stmt.order_by(rank.desc()).limit(top_k)

        try:
            rows = (await session.execute(stmt)).all()
        except Exception as exc:
            # A malformed tsquery must not take the whole request down; dense
            # retrieval can still answer.
            log.warning("keyword search failed", extra={"error": str(exc), "query": query[:120]})
            return []
        return [(chunk, act, float(r)) for chunk, act, r in rows]

    async def by_heading(
        self,
        session: AsyncSession,
        query: str,
        *,
        top_k: int,
        language: Language,
        languages: Sequence[Language] | None = None,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        in_force_only: bool = True,
    ) -> list[tuple[Chunk, LegalAct, float]]:
        """Match the query against article titles only.

        Benchmarking showed that in essentially every retrieval failure the
        answer was sitting in the article's own title — "Меҳнат шартномасининг
        шакли" for a question about the form of an employment contract — while
        the full-text search buried it under hundreds of articles that merely
        mention employment contracts.

        Searching titles in isolation fixes that for two reasons: the title has
        no body text to dilute it, and it is short, so ts_rank_cd's length
        normalisation (flag 2 | 8) rewards a title that is *mostly* query terms
        over a long one that happens to contain them.
        """
        config, tsquery = build_tsquery(query, language)
        ts = func.to_tsquery(config, tsquery)
        heading_tsv = func.to_tsvector(config, func.coalesce(Chunk.heading, ""))
        # 2 = divide by document length, 8 = divide by unique word count.
        rank = func.ts_rank_cd(heading_tsv, ts, 2 | 8).label("rank")

        stmt: Select = (
            select(Chunk, LegalAct, rank)
            .join(LegalAct, LegalAct.id == Chunk.act_id)
            .where(Chunk.heading.isnot(None))
            .where(heading_tsv.op("@@")(ts))
        )
        stmt = self._filters(stmt, languages, act_types, act_ids, in_force_only)
        stmt = stmt.order_by(rank.desc()).limit(top_k)

        try:
            rows = (await session.execute(stmt)).all()
        except Exception as exc:
            log.warning("heading search failed", extra={"error": str(exc), "query": query[:120]})
            return []
        return [(chunk, act, float(r)) for chunk, act, r in rows]

    async def by_article(
        self,
        session: AsyncSession,
        article_numbers: Sequence[str],
        *,
        act_types: Sequence[ActType] | None = None,
        act_ids: Sequence[uuid.UUID] | None = None,
        languages: Sequence[Language] | None = None,
        limit: int = 12,
    ) -> list[tuple[Chunk, LegalAct, float]]:
        """Exact article fetch. Scored 1.0 — an explicit citation is not a guess."""
        if not article_numbers:
            return []
        stmt: Select = (
            select(Chunk, LegalAct, literal(1.0))
            .join(LegalAct, LegalAct.id == Chunk.act_id)
            .where(Chunk.article_number.in_(list(article_numbers)))
        )
        stmt = self._filters(stmt, languages, act_types, act_ids, in_force_only=True)
        stmt = stmt.order_by(Chunk.ordinal).limit(limit)
        rows = (await session.execute(stmt)).all()
        return [(c, a, float(s)) for c, a, s in rows]

    @staticmethod
    def _filters(
        stmt: Select,
        languages: Sequence[Language] | None,
        act_types: Sequence[ActType] | None,
        act_ids: Sequence[uuid.UUID] | None,
        in_force_only: bool,
    ) -> Select:
        if languages:
            stmt = stmt.where(Chunk.language.in_(list(languages)))
        if act_types:
            stmt = stmt.where(Chunk.act_type.in_(list(act_types)))
        if act_ids:
            stmt = stmt.where(Chunk.act_id.in_(list(act_ids)))
        if in_force_only:
            stmt = stmt.where(LegalAct.status.in_([ActStatus.IN_FORCE, ActStatus.AMENDED]))
        return stmt

    async def find_acts_by_name(
        self, session: AsyncSession, name: str, limit: int = 5
    ) -> list[LegalAct]:
        """Resolve 'Civil Code' / 'Fuqarolik kodeksi' / 'Гражданский кодекс' to acts."""
        needle = f"%{normalize(name).lower()}%"
        latin = f"%{cyrillic_to_latin(normalize(name)).lower()}%"
        stmt = (
            select(LegalAct)
            .where(
                or_(
                    func.lower(LegalAct.short_name).like(needle),
                    func.lower(LegalAct.title_uz).like(needle),
                    func.lower(LegalAct.title_ru).like(needle),
                    func.lower(LegalAct.title_en).like(needle),
                    func.lower(LegalAct.title_uz).like(latin),
                )
            )
            .where(LegalAct.status.in_([ActStatus.IN_FORCE, ActStatus.AMENDED]))
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars())


SEARCH_VECTOR_SQL = text(
    """
    UPDATE chunks SET search_vector =
        setweight(to_tsvector(:config, coalesce(law_name, '')), 'A') ||
        setweight(to_tsvector(:config, coalesce(heading, '')), 'B') ||
        setweight(to_tsvector(:config, coalesce(text, '')), 'C')
    WHERE id = ANY(:ids)
    """
)

keyword_searcher = KeywordSearcher()
