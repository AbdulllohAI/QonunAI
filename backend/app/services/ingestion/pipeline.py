"""Ingestion orchestration: fetch → parse → structure → version → chunk → embed → index.

Idempotency is the design centre. An act is keyed by (source, external_id), and
its content hash decides whether anything downstream runs at all. Re-running the
pipeline over an unchanged corpus is cheap and produces no duplicates; re-running
over a changed act supersedes the old article versions and re-embeds only what
moved.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import delete, select, text as sql_text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import (
    ActStatus,
    Chunk,
    IngestionRun,
    Language,
    LegalAct,
    LegalActVersion,
    LegalAlert,
    LegalNode,
    NodeType,
)
from app.services.ingestion.chunker import legal_chunker
from app.services.ingestion.connectors.base import RawAct
from app.services.ingestion.hierarchy_builder import BuiltNode, hierarchy_builder
from app.services.ingestion.parsers import parse_document
from app.services.rag.crossref import persist_references
from app.services.rag.embedder import embedder

log = get_logger(__name__)


@dataclass
class IngestStats:
    acts_seen: int = 0
    acts_upserted: int = 0
    acts_unchanged: int = 0
    nodes_written: int = 0
    chunks_written: int = 0
    versions_written: int = 0
    crossrefs_written: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "IngestStats") -> None:
        self.acts_seen += other.acts_seen
        self.acts_upserted += other.acts_upserted
        self.acts_unchanged += other.acts_unchanged
        self.nodes_written += other.nodes_written
        self.chunks_written += other.chunks_written
        self.versions_written += other.versions_written
        self.crossrefs_written += other.crossrefs_written
        self.errors.extend(other.errors)

    def to_dict(self) -> dict:
        return {
            "acts_seen": self.acts_seen,
            "acts_upserted": self.acts_upserted,
            "acts_unchanged": self.acts_unchanged,
            "nodes_written": self.nodes_written,
            "chunks_written": self.chunks_written,
            "versions_written": self.versions_written,
            "crossrefs_written": self.crossrefs_written,
            "error_count": len(self.errors),
            "errors": self.errors[:20],
        }


class IngestionPipeline:
    async def ingest_raw_act(
        self, session: AsyncSession, raw: RawAct, *, force: bool = False
    ) -> IngestStats:
        stats = IngestStats(acts_seen=1)
        try:
            act, changed = await self._upsert_act(session, raw, force=force)
            if not changed:
                stats.acts_unchanged = 1
                return stats
            stats.acts_upserted = 1

            parsed = parse_document(
                raw.content, mime_type=raw.mime_type, filename=raw.source_url
            )
            if not parsed.blocks:
                stats.errors.append(f"{raw.external_id}: parser produced no blocks")
                return stats

            nodes = hierarchy_builder.build(parsed.blocks, language=raw.language)
            if not nodes:
                stats.errors.append(f"{raw.external_id}: no structural nodes recognised")
                return stats

            # Replace this act+language slice wholesale — simpler and safer than
            # diffing a restructured document node by node.
            #
            # On a forced re-ingest, purge EVERY language for the act, not just
            # the incoming one. Language is detected from the content, so a
            # detector fix can change an act's language between runs; purging
            # only the new language would strand the old slice as invisible,
            # uncitable duplicates that still surface in retrieval.
            await self._purge_language_slice(
                session, act.id, raw.language, all_languages=force
            )

            id_map = await self._write_nodes(session, act, nodes)
            stats.nodes_written = len(id_map)

            stats.versions_written = await self._write_versions(session, act, nodes, raw)
            stats.crossrefs_written = await self._write_crossrefs(session, act, nodes)
            stats.chunks_written = await self._write_chunks(session, act, nodes, raw, id_map)

            await session.flush()
        except Exception as exc:
            log.exception("ingest failed", extra={"external_id": raw.external_id})
            stats.errors.append(f"{raw.external_id}: {exc}")
        return stats

    # ------------------------------------------------------------------- act
    async def _upsert_act(
        self, session: AsyncSession, raw: RawAct, *, force: bool
    ) -> tuple[LegalAct, bool]:
        existing = (
            await session.execute(
                select(LegalAct).where(
                    LegalAct.source == raw.source,
                    LegalAct.external_id == raw.external_id,
                )
            )
        ).scalar_one_or_none()

        content_hash = raw.content_hash

        if existing is None:
            act = LegalAct(
                act_type=raw.act_type,
                status=raw.status,
                short_name=(raw.title or "")[:255] or None,
                doc_number=raw.doc_number,
                date_of_adoption=raw.date_of_adoption,
                date_in_force=raw.date_in_force,
                # Never fall back to today's date here: that would present
                # our own ingestion run as if it were the law's actual last
                # amendment date, indistinguishable from a real one to any
                # downstream reader (LLM or human). When the connector
                # couldn't parse a real amendment date off the source page,
                # we genuinely don't know one — leave it null.
                last_updated=raw.last_updated,
                issuing_body=raw.issuing_body,
                source=raw.source,
                external_id=raw.external_id,
                source_url=raw.source_url,
                content_hash=content_hash,
                meta=raw.meta,
            )
            _set_title(act, raw)
            session.add(act)
            await session.flush()
            session.add(
                LegalAlert(
                    act_id=act.id,
                    kind="new",
                    summary=f"New act ingested: {raw.title or raw.external_id}",
                    payload={"source": raw.source.value, "url": raw.source_url},
                )
            )
            return act, True

        if existing.content_hash == content_hash and not force:
            return existing, False

        previous_hash = existing.content_hash
        existing.act_type = raw.act_type
        existing.status = raw.status
        existing.doc_number = raw.doc_number or existing.doc_number
        existing.date_of_adoption = raw.date_of_adoption or existing.date_of_adoption
        existing.date_in_force = raw.date_in_force or existing.date_in_force
        # Same reasoning as the create path above: only overwrite with a
        # date the connector actually parsed off the source, never today's
        # date as a stand-in for "we don't know."
        if raw.last_updated:
            existing.last_updated = raw.last_updated
        existing.issuing_body = raw.issuing_body or existing.issuing_body
        existing.source_url = raw.source_url
        existing.content_hash = content_hash
        _set_title(existing, raw)

        if previous_hash:
            session.add(
                LegalAlert(
                    act_id=existing.id,
                    kind="repealed" if raw.status is ActStatus.REPEALED else "amended",
                    summary=f"Act text changed: {raw.title or raw.external_id}",
                    payload={
                        "previous_hash": previous_hash,
                        "new_hash": content_hash,
                        "url": raw.source_url,
                    },
                )
            )
        await session.flush()
        return existing, True

    async def _purge_language_slice(
        self,
        session: AsyncSession,
        act_id: uuid.UUID,
        language: Language,
        *,
        all_languages: bool = False,
    ) -> None:
        """Delete an act's chunks and nodes before re-writing them.

        `all_languages=True` clears every language for the act — use it on a
        forced re-ingest, where the detected language may differ from the run
        that wrote the existing rows.
        """
        chunk_q = delete(Chunk).where(Chunk.act_id == act_id)
        node_q = delete(LegalNode).where(LegalNode.act_id == act_id)
        if not all_languages:
            chunk_q = chunk_q.where(Chunk.language == language)
            node_q = node_q.where(LegalNode.language == language)

        await session.execute(chunk_q)
        await session.execute(node_q)
        await session.flush()

    # ----------------------------------------------------------------- nodes
    async def _write_nodes(
        self, session: AsyncSession, act: LegalAct, nodes: list[BuiltNode]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Insert in tree order so parents exist before children."""
        id_map: dict[uuid.UUID, uuid.UUID] = {}
        for built in nodes:
            db_node = LegalNode(
                act_id=act.id,
                parent_id=id_map.get(built.parent_id) if built.parent_id else None,
                node_type=built.node_type,
                ordinal=built.ordinal,
                number=built.number,
                article_number=built.article_number,
                path=built.path,
                heading=built.heading,
                body=built.body or None,
                language=built.language,
                meta=built.meta,
            )
            session.add(db_node)
            await session.flush()
            id_map[built.id] = db_node.id
        return id_map

    async def _write_versions(
        self, session: AsyncSession, act: LegalAct, nodes: list[BuiltNode], raw: RawAct
    ) -> int:
        """Snapshot article texts, closing any superseded version.

        Only MODDA nodes are versioned — that is the citable unit and the one a
        "what changed?" timeline is meaningful for.
        """
        written = 0
        effective = raw.last_updated or raw.date_of_adoption or date.today()

        for built in nodes:
            if built.node_type is not NodeType.MODDA or not built.body:
                continue
            body_hash = hashlib.sha256(built.body.encode("utf-8")).hexdigest()

            latest = (
                await session.execute(
                    select(LegalActVersion)
                    .where(
                        LegalActVersion.act_id == act.id,
                        LegalActVersion.article_number == built.article_number,
                        LegalActVersion.language == built.language,
                        LegalActVersion.valid_to.is_(None),
                    )
                    .order_by(LegalActVersion.captured_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if latest is not None:
                if latest.body_hash == body_hash:
                    continue  # unchanged
                latest.valid_to = effective

            session.add(
                LegalActVersion(
                    act_id=act.id,
                    article_number=built.article_number,
                    language=built.language,
                    body=built.body,
                    body_hash=body_hash,
                    valid_from=effective,
                    change_note="Amended text detected during ingestion"
                    if latest is not None
                    else "Initial capture",
                )
            )
            written += 1
        return written

    async def _write_crossrefs(
        self, session: AsyncSession, act: LegalAct, nodes: list[BuiltNode]
    ) -> int:
        written = 0
        for built in nodes:
            if built.node_type is NodeType.MODDA and built.body:
                written += await persist_references(
                    session, act, built.article_number, built.body
                )
        return written

    # ---------------------------------------------------------------- chunks
    async def _write_chunks(
        self,
        session: AsyncSession,
        act: LegalAct,
        nodes: list[BuiltNode],
        raw: RawAct,
        id_map: dict[uuid.UUID, uuid.UUID],
    ) -> int:
        drafts = legal_chunker.chunk_nodes(nodes)
        if not drafts:
            return 0

        law_name = act.short_name or act.display_title(raw.language)
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
                law_name=law_name,
                article_number=draft.article_number,
                jurisdiction=act.jurisdiction,
                language=draft.language,
                date_of_adoption=act.date_of_adoption,
                last_updated=act.last_updated,
                act_type=act.act_type,
                hierarchy_path=draft.hierarchy_path,
                heading=draft.heading,
                text=draft.text,
                token_count=draft.token_count,
                ordinal=draft.ordinal,
                source_url=act.source_url,
                embedding=vector,
                meta=draft.meta,
            )
            session.add(chunk)
            chunk_ids.append(chunk_id)

        await session.flush()
        await self._refresh_search_vectors(session, chunk_ids, raw.language)
        return len(chunk_ids)

    async def _refresh_search_vectors(
        self, session: AsyncSession, chunk_ids: list[uuid.UUID], language: Language
    ) -> None:
        """Populate the tsvector with the language-appropriate dictionary.

        Done in SQL rather than Python so the text config matches exactly what
        `to_tsquery` will use at query time — a mismatch here silently returns
        zero keyword hits.
        """
        if not chunk_ids:
            return
        await session.execute(
            sql_text(
                """
                UPDATE chunks SET search_vector =
                    setweight(to_tsvector(CAST(:config AS regconfig), coalesce(law_name, '')), 'A') ||
                    setweight(to_tsvector(CAST(:config AS regconfig), coalesce(heading, '')), 'B') ||
                    setweight(to_tsvector(CAST(:config AS regconfig), coalesce(text, '')), 'C')
                WHERE id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"config": language.pg_text_config, "ids": [str(i) for i in chunk_ids]},
        )

    # ------------------------------------------------------------------- runs
    async def run_connector(
        self,
        session: AsyncSession,
        connector,
        *,
        identifiers: list[str] | None = None,
        languages: list[Language] | None = None,
        force: bool = False,
        limit: int | None = None,
        **discover_kwargs,
    ) -> IngestStats:
        run = IngestionRun(connector=connector.name)
        session.add(run)
        await session.flush()

        stats = IngestStats()
        languages = languages or [Language.UZ_LATN, Language.RU]

        async with connector:
            ids: list[str] = list(identifiers or [])
            if not ids:
                async for identifier in connector.discover(**discover_kwargs):
                    ids.append(identifier)
                    if limit and len(ids) >= limit:
                        break

            for identifier in ids:
                # De-duplicate by content hash across the requested languages.
                # Some sources (lex.uz) key a document id to a single language
                # edition and serve identical bytes for every language prefix,
                # so looping languages naively doubles the request volume
                # against a rate-limited public service for no added content.
                seen_hashes: set[str] = set()
                for language in languages:
                    try:
                        raw = await connector.fetch_act(identifier, language)
                    except Exception as exc:
                        stats.errors.append(f"{identifier}[{language.value}]: {exc}")
                        continue
                    if raw is None:
                        continue
                    if raw.content_hash in seen_hashes:
                        log.debug(
                            "identical content for another language prefix, skipping",
                            extra={"identifier": identifier, "language": language.value},
                        )
                        continue
                    seen_hashes.add(raw.content_hash)
                    stats.merge(await self.ingest_raw_act(session, raw, force=force))
                await session.commit()

        run.status = "completed" if not stats.errors else "completed_with_errors"
        run.acts_seen = stats.acts_seen
        run.acts_upserted = stats.acts_upserted
        run.chunks_written = stats.chunks_written
        run.errors = stats.errors[:50]
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()

        log.info("ingestion run finished", extra={"connector": connector.name, **stats.to_dict()})
        return stats


def _set_title(act: LegalAct, raw: RawAct) -> None:
    if not raw.title:
        return
    match raw.language:
        case Language.RU:
            act.title_ru = raw.title
        case Language.EN:
            act.title_en = raw.title
        case _:
            act.title_uz = raw.title
    if not act.short_name:
        act.short_name = raw.title[:255]


ingestion_pipeline = IngestionPipeline()
