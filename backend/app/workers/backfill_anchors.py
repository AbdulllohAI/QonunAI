"""Backfill lex.uz article anchors for the indexed corpus.

Fetches each act's page from lex.uz, extracts the article -> node-id map from
its table of contents, and writes the anchor onto every matching chunk so
citations can deep-link to the provision rather than the top of the document.

Politeness: lex.uz publishes ``Crawl-delay: 20`` and this script honours it with
a single serialised loop — 13 acts is about 4.5 minutes. Do not parallelise it.

Usage
-----
    python -m app.workers.backfill_anchors            # all acts
    python -m app.workers.backfill_anchors --dry-run  # report, write nothing
    python -m app.workers.backfill_anchors --act-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update

from app.core.logging import configure_logging, get_logger
from app.db.models import Chunk, LegalAct, LegalNode
from app.db.session import SessionLocal
from app.services.ingestion.anchors import extract_anchors, match_anchor, verify_anchors

log = get_logger(__name__)

CRAWL_DELAY_SECONDS = 20.0
USER_AGENT = "QonunAI/1.0 (+legal-research; contact: you@example.uz)"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)


async def _fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        log.warning("anchor_fetch_failed", extra={"url": url, "error": str(exc)})
        return None


async def backfill_act(
    session,
    client: httpx.AsyncClient,
    act: LegalAct,
    *,
    dry_run: bool,
    fix_numbers: bool = False,
) -> dict[str, int]:
    stats = {"chunks": 0, "matched": 0, "nodes": 0, "renumbered": 0}
    if not act.source_url:
        log.warning("anchor_skip_no_source_url", extra={"act": act.short_name})
        return stats

    html = await _fetch(client, act.source_url)
    if html is None:
        return stats

    anchors = verify_anchors(html, extract_anchors(html))
    if not anchors:
        # Not necessarily an error: seed-imported acts point at a lex.uz page
        # whose markup we may not recognise. Worth surfacing, not worth failing.
        log.warning("anchor_none_extracted", extra={"act": act.short_name})
        return stats

    chunks = list(
        (await session.execute(select(Chunk).where(Chunk.act_id == act.id))).scalars()
    )
    stats["chunks"] = len(chunks)

    for chunk in chunks:
        hit = match_anchor(anchors, chunk.article_number, chunk.heading)
        if hit is None:
            continue
        stats["matched"] += 1
        # The TOC carries the sub-number the ingester dropped: articles 57, 57¹
        # and 57² all landed as "57". `match_anchor` disambiguated them on the
        # heading, so its article number is the corrected one.
        if hit.article_number != chunk.article_number:
            stats["renumbered"] += 1
        if not dry_run:
            chunk.lexuz_anchor_id = hit.anchor_id
            if fix_numbers:
                chunk.article_number = hit.article_number

    if not dry_run:
        # Mirror onto the structural nodes, which are the source of truth.
        now = datetime.now(timezone.utc)
        nodes = list(
            (await session.execute(select(LegalNode).where(LegalNode.act_id == act.id))).scalars()
        )
        for node in nodes:
            hit = match_anchor(anchors, node.article_number, node.heading)
            if hit is None:
                continue
            node.lexuz_anchor_id = hit.anchor_id
            node.anchor_verified = True
            node.anchor_checked_at = now
            if fix_numbers:
                node.article_number = hit.article_number
            stats["nodes"] += 1
        await session.commit()

    coverage = 100.0 * stats["matched"] / stats["chunks"] if stats["chunks"] else 0.0
    log.info(
        "anchor_backfill_act",
        extra={
            "act": act.short_name,
            "anchors": len(anchors),
            "chunks": stats["chunks"],
            "matched": stats["matched"],
            "renumbered": stats["renumbered"],
            "coverage_pct": round(coverage, 1),
        },
    )
    return stats


async def main(act_id: str | None, dry_run: bool, fix_numbers: bool = False) -> None:
    configure_logging("INFO")
    totals = {"chunks": 0, "matched": 0, "nodes": 0, "renumbered": 0}

    async with SessionLocal() as session:
        stmt = select(LegalAct).order_by(LegalAct.short_name)
        if act_id:
            stmt = stmt.where(LegalAct.id == act_id)
        acts = list((await session.execute(stmt)).scalars())

        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            for index, act in enumerate(acts):
                if index:
                    # Serialised, one request per CRAWL_DELAY_SECONDS.
                    await asyncio.sleep(CRAWL_DELAY_SECONDS)
                stats = await backfill_act(
                    session, client, act, dry_run=dry_run, fix_numbers=fix_numbers
                )
                for key in totals:
                    totals[key] += stats[key]

    coverage = 100.0 * totals["matched"] / totals["chunks"] if totals["chunks"] else 0.0
    log.info(
        "anchor_backfill_done",
        extra={
            "acts": len(acts),
            "chunks": totals["chunks"],
            "matched": totals["matched"],
            "renumbered": totals["renumbered"],
            "coverage_pct": round(coverage, 1),
            "dry_run": dry_run,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--act-id", default=None, help="Backfill a single act.")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    parser.add_argument(
        "--fix-numbers",
        action="store_true",
        help="Also correct article_number from the TOC, restoring dropped sub-numbers (57 -> 57-1).",
    )
    args = parser.parse_args()
    asyncio.run(main(args.act_id, args.dry_run, args.fix_numbers))
