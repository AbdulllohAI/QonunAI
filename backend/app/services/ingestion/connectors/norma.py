"""Norma.uz connector — commentary, practitioner analysis and consolidated texts.

Everything ingested from here is typed `COMMENTARY` by default: it is doctrinal
material, not a source of law, and the reasoning engine must never present it as
binding. Where Norma publishes a consolidated statutory text, pass
`act_type=ActType.CODE` explicitly — but prefer lex.uz for anything normative,
since it is the official database.

Norma.uz is a commercial publisher. Much of the content is behind a paid
subscription; scraping it without a licence is a contractual and copyright
problem, not merely a technical one. Set `NORMA_API_TOKEN` if you have a
subscription that grants API access, and confirm your licence covers derivative
indexing before enabling this connector in production.
"""
from __future__ import annotations

import re
from typing import AsyncIterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ActStatus, ActType, Language, SourceSystem
from app.services.ingestion.connectors.base import BaseConnector, RawAct, same_host
from app.services.ingestion.connectors.lexuz import _extract_date
from app.services.lang.translit import normalize

log = get_logger(__name__)

_SELECTORS = {
    "title": ["h1", ".article-title", ".news-title"],
    "body": [".article-body", ".news-text", "article", ".content-text"],
    "date": ["time", ".date", ".published"],
    "paywall": [".paywall", ".subscribe-block", ".premium-lock"],
}

_LANG_PATH = {
    Language.UZ_LATN: "uz",
    Language.RU: "ru",
    Language.EN: "en",
    Language.UZ_CYRL: "uz",
}


class NormaConnector(BaseConnector):
    name = "norma"
    source = SourceSystem.NORMA

    def __init__(self, api_token: str | None = None, **kwargs) -> None:
        self.base_url = settings.NORMA_BASE_URL.rstrip("/")
        super().__init__(**kwargs)
        self.api_token = api_token

    async def discover(
        self,
        *,
        sections: list[str] | None = None,
        max_pages: int = 3,
        language: Language = Language.RU,
    ) -> AsyncIterator[str]:
        lang = _LANG_PATH.get(language, "ru")
        for section in sections or ["publish/doc", "publish/article"]:
            for page in range(1, max_pages + 1):
                url = f"{self.base_url}/{lang}/{section}?page={page}"
                try:
                    resp = await self.fetch(url)
                except Exception as exc:
                    log.warning("norma discovery failed", extra={"url": url, "error": str(exc)})
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                links = [
                    urljoin(self.base_url, a["href"])
                    for a in soup.select("a[href]")
                    if a.get("href") and same_host(urljoin(self.base_url, a["href"]), self.base_url)
                ]
                article_links = [
                    link for link in dict.fromkeys(links) if re.search(r"/publish/doc/\w", link)
                ]
                if not article_links:
                    break
                for link in article_links:
                    yield link

    async def fetch_act(
        self,
        identifier: str,
        language: Language = Language.RU,
        *,
        act_type: ActType = ActType.COMMENTARY,
    ) -> RawAct | None:
        url = identifier if identifier.startswith("http") else urljoin(self.base_url, identifier)
        try:
            resp = await self.fetch(url)
        except Exception as exc:
            log.warning("norma fetch failed", extra={"url": url, "error": str(exc)})
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        # Paywalled stubs are worse than nothing: they index a teaser paragraph
        # as if it were the analysis.
        if any(soup.select_one(sel) for sel in _SELECTORS["paywall"]):
            log.info("skipping paywalled norma document", extra={"url": url})
            return None

        title = next(
            (
                normalize(el.get_text(" ", strip=True))
                for sel in _SELECTORS["title"]
                if (el := soup.select_one(sel))
            ),
            None,
        )
        body = next(
            (
                str(el)
                for sel in _SELECTORS["body"]
                if (el := soup.select_one(sel)) and len(el.get_text(strip=True)) > 300
            ),
            None,
        )
        if not body:
            return None

        published = next(
            (
                normalize(el.get_text(" ", strip=True))
                for sel in _SELECTORS["date"]
                if (el := soup.select_one(sel))
            ),
            "",
        )

        return RawAct(
            external_id=re.sub(r"[^\w\-]", "_", url.replace(self.base_url, "").strip("/"))[:120],
            source=self.source,
            source_url=url,
            content=body.encode("utf-8"),
            mime_type="text/html",
            language=language,
            act_type=act_type,
            status=ActStatus.IN_FORCE,
            title=title,
            date_of_adoption=_extract_date(published),
            last_updated=_extract_date(published),
            issuing_body="Norma (commercial publisher)",
            meta={"binding": False, "note": "Doctrinal commentary — not a source of law."},
        )
