"""Load pre-structured legal corpora from CSV.

This is the fastest path to a working system: it bypasses scraping entirely and
ingests already-cleaned article tables. Two known layouts are auto-detected, and
an explicit column mapping covers anything else.

Known layouts
-------------
`jinoyat`     Qism, Bo'lim, Bob raqami, Bob nomi, Modda raqami, Modda nomi, Modda matni
`konstitutsiya`  modda_raqami, bolim, bob_raqami, bob_nomi, matn

The Constitution file has one row per *clause*, with the article number repeated
across rows — so rows are grouped by article before chunking, or every clause
would become its own "article" and citations would be wrong.
"""
from __future__ import annotations

import csv
import hashlib
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import (
    ActStatus,
    ActType,
    Chunk,
    Language,
    LegalAct,
    LegalNode,
    NodeType,
    SourceSystem,
)
from app.services.ingestion.chunker import estimate_tokens, legal_chunker
from app.services.ingestion.hierarchy_builder import BuiltNode
from app.services.ingestion.pipeline import IngestStats, ingestion_pipeline
from app.services.lang.translit import normalize
from app.services.rag.crossref import persist_references
from app.services.rag.embedder import embedder

log = get_logger(__name__)


@dataclass(slots=True)
class ColumnMap:
    article_number: str
    body: str
    article_title: str | None = None
    chapter_number: str | None = None
    chapter_title: str | None = None
    section: str | None = None
    part: str | None = None


LAYOUTS: dict[str, ColumnMap] = {
    "jinoyat": ColumnMap(
        part="Qism",
        section="Bo'lim",
        chapter_number="Bob raqami",
        chapter_title="Bob nomi",
        article_number="Modda raqami",
        article_title="Modda nomi",
        body="Modda matni",
    ),
    "konstitutsiya": ColumnMap(
        section="bolim",
        chapter_number="bob_raqami",
        chapter_title="bob_nomi",
        article_number="modda_raqami",
        body="matn",
    ),
}


def detect_layout(fieldnames: Iterable[str]) -> ColumnMap | None:
    names = {n.strip().lstrip("﻿") for n in fieldnames if n}
    for layout in LAYOUTS.values():
        required = {layout.article_number, layout.body}
        if required <= names:
            return layout
    return None


class CsvSeedLoader:
    async def load(
        self,
        session: AsyncSession,
        csv_path: str | Path,
        *,
        short_name: str,
        act_type: ActType = ActType.CODE,
        language: Language = Language.UZ_LATN,
        title: str | None = None,
        date_of_adoption: date | None = None,
        source_url: str | None = None,
        column_map: ColumnMap | None = None,
        external_id: str | None = None,
        replace: bool = True,
    ) -> IngestStats:
        path = Path(csv_path)
        stats = IngestStats(acts_seen=1)
        if not path.exists():
            stats.errors.append(f"file not found: {path}")
            return stats

        # utf-8-sig strips the BOM these exports carry; without it the first
        # column name is "﻿Qism" and layout detection fails.
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            layout = column_map or detect_layout(reader.fieldnames or [])
            if layout is None:
                stats.errors.append(
                    f"could not detect layout for {path.name}; columns were "
                    f"{reader.fieldnames}. Pass an explicit ColumnMap."
                )
                return stats
            rows = [{(k or "").strip().lstrip("﻿"): v for k, v in row.items()} for row in reader]

        if not rows:
            stats.errors.append(f"{path.name}: no rows")
            return stats

        act = await self._upsert_act(
            session,
            short_name=short_name,
            act_type=act_type,
            language=language,
            title=title or short_name,
            date_of_adoption=date_of_adoption,
            source_url=source_url,
            external_id=external_id or f"csv:{path.stem}",
            content_hash=_hash_rows(rows),
            replace=replace,
        )
        if act is None:
            stats.acts_unchanged = 1
            return stats
        stats.acts_upserted = 1

        nodes = self._rows_to_nodes(rows, layout, language)
        if not nodes:
            stats.errors.append(f"{path.name}: no article rows recognised")
            return stats

        id_map = await ingestion_pipeline._write_nodes(session, act, nodes)
        stats.nodes_written = len(id_map)

        for node in nodes:
            if node.node_type is NodeType.MODDA and node.body:
                stats.crossrefs_written += await persist_references(
                    session, act, node.article_number, node.body
                )

        stats.chunks_written = await self._write_chunks(session, act, nodes, id_map, language)
        await session.commit()

        log.info("csv seed loaded", extra={"file": path.name, **stats.to_dict()})
        return stats

    # ------------------------------------------------------------------ parts
    async def _upsert_act(
        self,
        session: AsyncSession,
        *,
        short_name: str,
        act_type: ActType,
        language: Language,
        title: str,
        date_of_adoption: date | None,
        source_url: str | None,
        external_id: str,
        content_hash: str,
        replace: bool,
    ) -> LegalAct | None:
        existing = (
            await session.execute(
                select(LegalAct).where(
                    LegalAct.source == SourceSystem.SEED_CSV,
                    LegalAct.external_id == external_id,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            if existing.content_hash == content_hash and not replace:
                return None
            await session.execute(
                delete(Chunk).where(Chunk.act_id == existing.id, Chunk.language == language)
            )
            await session.execute(
                delete(LegalNode).where(
                    LegalNode.act_id == existing.id, LegalNode.language == language
                )
            )
            existing.content_hash = content_hash
            # Not touched: a bare CSV carries no real legislative amendment
            # date, and stamping today's re-ingestion date here reads to a
            # downstream LLM as "this law was amended today" — the exact
            # confusion a QA pass caught it presenting as fact. Leave
            # last_updated null for CSV-seeded acts; a real connector
            # (lexuz.py, norma.py) that actually parses the source page's own
            # amendment date is the only legitimate way to populate it.
            await session.flush()
            return existing

        act = LegalAct(
            act_type=act_type,
            status=ActStatus.IN_FORCE,
            short_name=short_name,
            title_uz=title if language in (Language.UZ_LATN, Language.UZ_CYRL) else None,
            title_ru=title if language is Language.RU else None,
            title_en=title if language is Language.EN else None,
            date_of_adoption=date_of_adoption,
            last_updated=None,
            source=SourceSystem.SEED_CSV,
            external_id=external_id,
            source_url=source_url,
            content_hash=content_hash,
            meta={"loader": "csv_seed"},
        )
        session.add(act)
        await session.flush()
        return act

    def _rows_to_nodes(
        self, rows: list[dict], layout: ColumnMap, language: Language
    ) -> list[BuiltNode]:
        """Group rows into a Part → Section → Chapter → Article tree."""
        nodes: list[BuiltNode] = []
        current: dict[str, BuiltNode] = {}
        ordinal = 0

        # Group consecutive rows sharing an article number (the Constitution
        # layout emits one row per clause).
        grouped: list[tuple[dict, list[str]]] = []
        for row in rows:
            article = normalize(str(row.get(layout.article_number, "") or ""))
            body = normalize(str(row.get(layout.body, "") or ""))
            if not article or not body:
                continue
            if grouped and normalize(
                str(grouped[-1][0].get(layout.article_number, "") or "")
            ) == article:
                grouped[-1][1].append(body)
            else:
                grouped.append((row, [body]))

        for row, bodies in grouped:
            for level_key, node_type, num_col, title_col in (
                ("part", NodeType.QISM, layout.part, layout.part),
                ("section", NodeType.BOLIM, layout.section, layout.section),
                ("chapter", NodeType.BOB, layout.chapter_number, layout.chapter_title),
            ):
                if not num_col:
                    continue
                label = normalize(str(row.get(num_col, "") or ""))
                if not label:
                    continue
                heading = normalize(str(row.get(title_col, "") or "")) if title_col else None
                if current.get(level_key) and current[level_key].meta.get("label") == label:
                    continue

                parent = _closest_parent(current, level_key)
                node = BuiltNode(
                    node_type=node_type,
                    number=label[:64],
                    heading=heading or None,
                    parent_id=parent.id if parent else None,
                    path=_path(parent, label),
                    ordinal=ordinal,
                    language=language,
                    meta={"label": label},
                )
                ordinal += 1
                nodes.append(node)
                current[level_key] = node
                # A new higher level invalidates the levels below it.
                for lower in _LOWER_LEVELS[level_key]:
                    current.pop(lower, None)

            article = normalize(str(row.get(layout.article_number, "") or ""))
            heading = (
                normalize(str(row.get(layout.article_title, "") or ""))
                if layout.article_title
                else None
            )
            parent = _closest_parent(current, "article")
            node = BuiltNode(
                node_type=NodeType.MODDA,
                number=article,
                article_number=article,
                heading=heading or None,
                parent_id=parent.id if parent else None,
                path=_path(parent, article),
                ordinal=ordinal,
                language=language,
            )
            node.body_parts.extend(bodies)
            ordinal += 1
            nodes.append(node)

        return nodes

    async def _write_chunks(
        self,
        session: AsyncSession,
        act: LegalAct,
        nodes: list[BuiltNode],
        id_map: dict[uuid.UUID, uuid.UUID],
        language: Language,
    ) -> int:
        drafts = legal_chunker.chunk_nodes(nodes)
        if not drafts:
            return 0

        vectors = await embedder.embed_documents([d.text for d in drafts])
        chunk_ids: list[uuid.UUID] = []

        for draft, vector in zip(drafts, vectors):
            # id is generated up front rather than read off `chunk.id` after
            # construction: the column's UUID default is populated by
            # SQLAlchemy only at flush time, so reading it beforehand yields
            # None for every chunk (and silently corrupts chunk_ids).
            chunk_id = uuid.uuid4()
            chunk = Chunk(
                id=chunk_id,
                act_id=act.id,
                node_id=id_map.get(draft.node_id) if draft.node_id else None,
                law_name=act.short_name or "",
                article_number=draft.article_number,
                jurisdiction="Uzbekistan",
                language=draft.language,
                date_of_adoption=act.date_of_adoption,
                last_updated=act.last_updated,
                act_type=act.act_type,
                hierarchy_path=draft.hierarchy_path,
                heading=draft.heading,
                text=draft.text,
                token_count=draft.token_count or estimate_tokens(draft.text),
                ordinal=draft.ordinal,
                source_url=act.source_url,
                embedding=vector,
                meta=draft.meta,
            )
            session.add(chunk)
            chunk_ids.append(chunk_id)

        await session.flush()
        await ingestion_pipeline._refresh_search_vectors(session, chunk_ids, language)
        return len(chunk_ids)


_LOWER_LEVELS = {
    "part": ("section", "chapter"),
    "section": ("chapter",),
    "chapter": (),
}
_PARENT_ORDER = {
    "part": [],
    "section": ["part"],
    "chapter": ["section", "part"],
    "article": ["chapter", "section", "part"],
}


def _closest_parent(current: dict[str, BuiltNode], level: str) -> BuiltNode | None:
    for candidate in _PARENT_ORDER[level]:
        node = current.get(candidate)
        if node is not None:
            return node
    return None


def _path(parent: BuiltNode | None, label: str) -> str:
    return f"{parent.path}/{label}" if parent else label


def _hash_rows(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(sorted(row.items())).encode("utf-8"))
    return digest.hexdigest()


csv_seed_loader = CsvSeedLoader()
