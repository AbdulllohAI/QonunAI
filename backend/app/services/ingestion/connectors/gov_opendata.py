"""Government open-data connector (data.egov.uz and gov.uz portals).

data.egov.uz is the Republic's open-data portal and *does* have a documented
partner API with a token. It carries registries and datasets rather than statute
text — useful for enriching the corpus (registers of licences, lists of
regulatory bodies) and for discovering newly published acts, but it is not a
substitute for lex.uz.

This connector is written against the documented `apiPartner` shape:
    GET {base}/{dataset}/{version}?limit=&offset=
    Header: X-API-KEY: <token>
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, AsyncIterator

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ActStatus, ActType, Language, SourceSystem
from app.services.ingestion.connectors.base import BaseConnector, RawAct

log = get_logger(__name__)


class GovOpenDataConnector(BaseConnector):
    name = "gov_opendata"
    source = SourceSystem.GOV_OPENDATA

    def __init__(self, token: str | None = None, **kwargs) -> None:
        self.base_url = settings.GOV_OPENDATA_BASE.rstrip("/")
        # Open-data APIs publish rate limits; robots.txt does not apply to an API.
        kwargs.setdefault("respect_robots", False)
        super().__init__(**kwargs)
        self.token = token or settings.GOV_OPENDATA_TOKEN

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.token} if self.token else {}

    async def discover(
        self, *, datasets: list[str] | None = None, **kwargs
    ) -> AsyncIterator[str]:
        for dataset in datasets or []:
            yield dataset

    async def fetch_dataset(
        self, dataset: str, *, version: str = "1", limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        if not self.token:
            log.warning("GOV_OPENDATA_TOKEN not set — dataset fetch skipped")
            return []
        url = f"{self.base_url}/{dataset}/{version}?limit={limit}&offset={offset}"
        try:
            resp = await self.fetch(url, headers=self._headers())
            payload = resp.json()
        except Exception as exc:
            log.warning("open-data fetch failed", extra={"dataset": dataset, "error": str(exc)})
            return []
        # The portal wraps rows under `result.data` on most datasets.
        result = payload.get("result", payload)
        rows = result.get("data", result) if isinstance(result, dict) else result
        return rows if isinstance(rows, list) else []

    async def iter_dataset(
        self, dataset: str, *, version: str = "1", page_size: int = 200, max_rows: int = 10_000
    ) -> AsyncIterator[dict[str, Any]]:
        offset = 0
        while offset < max_rows:
            rows = await self.fetch_dataset(
                dataset, version=version, limit=page_size, offset=offset
            )
            if not rows:
                return
            for row in rows:
                yield row
            if len(rows) < page_size:
                return
            offset += page_size

    async def fetch_act(
        self, identifier: str, language: Language = Language.UZ_LATN
    ) -> RawAct | None:
        """Materialise a dataset as a single reference document.

        Registry data is not statute, so it is stored as `COMMENTARY` — the
        reasoning engine will never cite it as binding law.
        """
        rows = [row async for row in self.iter_dataset(identifier)]
        if not rows:
            return None

        body = json.dumps(rows, ensure_ascii=False, indent=2)
        return RawAct(
            external_id=f"dataset:{identifier}",
            source=self.source,
            source_url=f"{self.base_url}/{identifier}",
            content=_render_dataset_html(identifier, rows).encode("utf-8"),
            mime_type="text/html",
            language=language,
            act_type=ActType.COMMENTARY,
            status=ActStatus.IN_FORCE,
            title=f"Open data registry: {identifier}",
            last_updated=date.today(),
            issuing_body="data.egov.uz",
            meta={"binding": False, "row_count": len(rows), "raw_json_bytes": len(body)},
        )


def _render_dataset_html(dataset: str, rows: list[dict]) -> str:
    """Flatten rows to HTML so the standard parser pipeline can consume them."""
    if not rows:
        return ""
    columns = list(rows[0].keys())
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(str(row.get(c, '')))}</td>" for c in columns) + "</tr>"
        for row in rows[:5000]
    )
    return (
        f"<div id='doc_content'><h1>{_esc(dataset)}</h1>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _esc(text: str) -> str:
    return (
        re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
