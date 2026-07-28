"""LexUZ (lex.uz) connector — the National Database of Legislation.

**Read this before running against production.**

lex.uz does not publish a documented, openly-contracted public API. This
connector therefore does two things:

1. **Prefers a JSON endpoint** (`LEXUZ_API_BASE`) if one is available to you.
   If your organisation has an access agreement with the Ministry of Justice,
   set `LEXUZ_API_BASE` and `LEXUZ_API_TOKEN` and this path is used — it is
   cheaper, more stable, and the correct way to consume the corpus at scale.
2. **Falls back to polite HTML scraping** of the public document pages, which is
   what most integrators actually have available.

The HTML selectors below reflect lex.uz's document-page structure at the time of
writing. Site markup changes; `_SELECTORS` is isolated at the top of the file
precisely so a layout change is a one-line fix rather than a rewrite, and
`validate_selectors()` gives you a fast signal when they break.

Operational note: confirm the terms of use and, for sustained crawling, seek
written permission. `respect_robots=True` and the shared rate limiter are on by
default; do not disable them to go faster.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import AsyncIterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ActStatus, ActType, Language, SourceSystem
from app.services.ingestion.connectors.base import BaseConnector, RawAct
from app.services.lang.translit import normalize

log = get_logger(__name__)

# --- markup contract (update here when lex.uz changes layout) --------------
#
# Verified against live lex.uz (doc 111457, Criminal Code). The document body
# is server-rendered — ~1.05 MB of text under #divCont — so plain HTTP + HTML
# parsing is sufficient and no headless browser is needed. Individual statutory
# paragraphs carry class .ACT_TEXT; .lx_elem* are generic layout wrappers.
# Ordered most- to least-specific; _first_html takes the first that matches.
_SELECTORS = {
    "title": ["h1.doc-title", ".document-title", "h1", "title"],
    "body": [
        "#divCont",
        "#divBody",
        "#mD",
        ".main-column.document",
        "#main_container1",
        ".docBody__container",
        # Legacy guesses kept last as a fallback if the layout changes again.
        "#doc_content",
        ".document-body",
    ],
    "meta_block": [".docNavbar", "#dvToc", ".doc-info", ".document-meta"],
    "search_result": ["a.doc-link", ".search-result a", "table a[href*='/docs/']"],
}

_DOC_ID_RE = re.compile(r"/docs?/[-]?(\d+)")

# lex.uz language path segments.
#
# Verified against the live site: only /uz/, /ru/ and /en/ resolve — every
# Cyrillic variant (/uz-cyrl/, /uz-cy/, /uzc/, /oz/, /cyr/) returns 404, so
# UZ_CYRL maps to /uz/.
#
# IMPORTANT: the segment selects the *site chrome* language, not the document
# language. Requesting /uz/, /ru/ and /en/ for the same doc id returns byte-
# identical content. lex.uz publishes each language edition of an act as a
# SEPARATE document with its own id, so the language of a document is a
# property of the id, not of the URL prefix — which is why the connector
# detects language from the fetched text instead of trusting the request.
_LANG_PATH = {
    Language.UZ_LATN: "uz",
    Language.UZ_CYRL: "uz",
    Language.RU: "ru",
    Language.EN: "en",
}

# Well-known act IDs. These are the corpus backbone — everything else is
# discovered by crawling. Verify each against lex.uz before a production run;
# document IDs are stable but the set of codes in force is not.
SEED_ACT_IDS: dict[str, dict] = {
    "constitution": {"id": "6445145", "type": ActType.CONSTITUTION, "short": "Konstitutsiya"},
    "civil_code_1": {"id": "111181", "type": ActType.CODE, "short": "Fuqarolik kodeksi (I qism)"},
    "civil_code_2": {"id": "180552", "type": ActType.CODE, "short": "Fuqarolik kodeksi (II qism)"},
    "criminal_code": {"id": "111457", "type": ActType.CODE, "short": "Jinoyat kodeksi"},
    "labour_code": {"id": "6257288", "type": ActType.CODE, "short": "Mehnat kodeksi"},
    "tax_code": {"id": "4674902", "type": ActType.CODE, "short": "Soliq kodeksi"},
    "admin_liability_code": {
        "id": "97661", "type": ActType.CODE, "short": "Ma'muriy javobgarlik to'g'risidagi kodeks",
    },
    "criminal_procedure_code": {
        "id": "111463", "type": ActType.CODE, "short": "Jinoyat-protsessual kodeksi",
    },
    "civil_procedure_code": {
        "id": "3517337", "type": ActType.CODE, "short": "Fuqarolik protsessual kodeksi",
    },
    "family_code": {"id": "104720", "type": ActType.CODE, "short": "Oila kodeksi"},
    "land_code": {"id": "144870", "type": ActType.CODE, "short": "Yer kodeksi"},
    "customs_code": {"id": "2213194", "type": ActType.CODE, "short": "Bojxona kodeksi"},
    "budget_code": {"id": "2304140", "type": ActType.CODE, "short": "Byudjet kodeksi"},
    # FIXME: "175bulk" is not a valid lex.uz document id (they are numeric).
    # Left in deliberately so the warning in discover() surfaces it; replace
    # with the real id from lex.uz before relying on the Housing Code.
    "housing_code": {"id": "175bulk", "type": ActType.CODE, "short": "Uy-joy kodeksi"},
    "urban_code": {"id": "1088243", "type": ActType.CODE, "short": "Shaharsozlik kodeksi"},
}

_STATUS_HINTS = [
    (re.compile(r"kuchini\s+yo[’'ʻ‘]?qotgan|утратил\s+силу|repealed", re.I), ActStatus.REPEALED),
    (re.compile(r"o[’'ʻ‘]?zgartirish\s+kiritilgan|с\s+изменениями|amended", re.I), ActStatus.AMENDED),
    (re.compile(r"kuchga\s+kirmagan|не\s+вступил", re.I), ActStatus.NOT_YET_IN_FORCE),
]

# Order matters: first match wins, so the most specific patterns come first.
# "kodeks"/"кодекс" is checked before the constitution pattern because a title
# like "Конституционный суд ... кодекс" is a code, not the Constitution; only a
# title that is *anchored* on Konstitutsiya should type as CONSTITUTION.
_TYPE_HINTS = [
    (re.compile(r"kodeks|кодекс|\bcode\b", re.I), ActType.CODE),
    (re.compile(r"konstitutsiya|конституц|constitution", re.I), ActType.CONSTITUTION),
    (re.compile(r"farmon|указ\s+президента", re.I), ActType.PRESIDENTIAL_DECREE),
    (re.compile(r"prezident\w*\s+qaror|постановление\s+президента", re.I), ActType.PRESIDENTIAL_RESOLUTION),
    (re.compile(r"vazirlar\s+mahkamasi|кабинета\s+министров", re.I), ActType.CABINET_RESOLUTION),
    (re.compile(r"\bqonun\b|\bзакон\b", re.I), ActType.LAW),
    (re.compile(r"buyruq|приказ|nizom|положение", re.I), ActType.MINISTERIAL_ACT),
]

_DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DOC_NUMBER_RE = re.compile(
    r"(?:№|N|son|сон|raqam)\s*[:\-]?\s*([A-ZА-Я0-9][A-ZА-Я0-9\-/]{1,24})", re.I
)


class LexUzConnector(BaseConnector):
    name = "lexuz"
    source = SourceSystem.LEXUZ

    def __init__(self, api_token: str | None = None, **kwargs) -> None:
        self.base_url = settings.LEXUZ_BASE_URL.rstrip("/")
        super().__init__(**kwargs)
        self.api_base = settings.LEXUZ_API_BASE.rstrip("/") if settings.LEXUZ_API_BASE else None
        self.api_token = api_token

    # ------------------------------------------------------------- discovery
    async def discover(
        self,
        *,
        seeds: bool = True,
        search_terms: list[str] | None = None,
        max_pages: int = 5,
        language: Language = Language.UZ_LATN,
    ) -> AsyncIterator[str]:
        """Yield lex.uz document IDs."""
        emitted: set[str] = set()

        if seeds:
            for name, entry in SEED_ACT_IDS.items():
                doc_id = entry["id"]
                if not doc_id.isdigit():
                    # Loudly, not silently: a malformed seed ID means that act
                    # is simply absent from the corpus, and a silent skip makes
                    # that look like a retrieval failure much later on.
                    log.warning(
                        "skipping seed act with non-numeric lex.uz id — verify it on lex.uz",
                        extra={"seed": name, "doc_id": doc_id, "short": entry.get("short")},
                    )
                    continue
                if doc_id not in emitted:
                    emitted.add(doc_id)
                    yield doc_id

        for term in search_terms or []:
            async for doc_id in self._search(term, max_pages, language):
                if doc_id not in emitted:
                    emitted.add(doc_id)
                    yield doc_id

    async def _search(
        self, term: str, max_pages: int, language: Language
    ) -> AsyncIterator[str]:
        lang = _LANG_PATH[language]
        for page in range(1, max_pages + 1):
            url = f"{self.base_url}/{lang}/search/nat?text={term}&page={page}"
            try:
                resp = await self.fetch(url)
            except Exception as exc:
                log.warning("search failed", extra={"term": term, "page": page, "error": str(exc)})
                return

            soup = BeautifulSoup(resp.text, "lxml")
            found = False
            for selector in _SELECTORS["search_result"]:
                for anchor in soup.select(selector):
                    href = anchor.get("href", "")
                    match = _DOC_ID_RE.search(href)
                    if match:
                        found = True
                        yield match.group(1)
                if found:
                    break
            if not found:
                return

    # ------------------------------------------------------------------ fetch
    async def fetch_act(
        self, identifier: str, language: Language = Language.UZ_LATN
    ) -> RawAct | None:
        if self.api_base and self.api_token:
            act = await self._fetch_via_api(identifier, language)
            if act:
                return act
        return await self._fetch_via_html(identifier, language)

    async def _fetch_via_api(self, doc_id: str, language: Language) -> RawAct | None:
        url = f"{self.api_base}/documents/{doc_id}?lang={_LANG_PATH[language]}"
        try:
            resp = await self.fetch(url, headers={"Authorization": f"Bearer {self.api_token}"})
            data = resp.json()
        except Exception as exc:
            log.info("lexuz API path unavailable, falling back to HTML",
                     extra={"doc_id": doc_id, "error": str(exc)})
            return None

        body = data.get("content") or data.get("text") or ""
        if not body:
            return None

        return RawAct(
            external_id=str(doc_id),
            source=self.source,
            source_url=f"{self.base_url}/{_LANG_PATH[language]}/docs/{doc_id}",
            content=body.encode("utf-8"),
            mime_type="text/html",
            language=language,
            act_type=_infer_type(data.get("title", "")),
            status=_infer_status(data.get("status", "")),
            title=data.get("title"),
            doc_number=data.get("number"),
            date_of_adoption=_parse_date(data.get("adopted_at")),
            date_in_force=_parse_date(data.get("in_force_at")),
            last_updated=_parse_date(data.get("updated_at")),
            issuing_body=data.get("issuer"),
            meta={"via": "api"},
        )

    async def _fetch_via_html(self, doc_id: str, language: Language) -> RawAct | None:
        url = f"{self.base_url}/{_LANG_PATH[language]}/docs/{doc_id}"
        try:
            resp = await self.fetch(url)
        except Exception as exc:
            log.warning("fetch_act failed", extra={"doc_id": doc_id, "error": str(exc)})
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        title = _first_text(soup, _SELECTORS["title"])
        body_html = _first_html(soup, _SELECTORS["body"])

        if not body_html or len(body_html) < 500:
            log.warning(
                "document body missing or too short — check _SELECTORS against current markup",
                extra={"doc_id": doc_id, "length": len(body_html or "")},
            )
            if not body_html:
                return None

        meta_text = " ".join(
            normalize(el.get_text(" ", strip=True))
            for selector in _SELECTORS["meta_block"]
            for el in soup.select(selector)
        )
        haystack = f"{title or ''} {meta_text}"

        # Trust the content, not the URL. lex.uz does not reliably honour the
        # language path segment — /uz/docs/111457 serves Russian text — and
        # mislabelling it as uz-Latn would silently break language filtering at
        # retrieval time (queries would match against the wrong FTS dictionary
        # and the wrong script variants).
        actual_language = _detect_content_language(
            BeautifulSoup(body_html, "lxml").get_text(" ", strip=True), language
        )
        if actual_language is not language:
            log.info(
                "lex.uz served a different language than requested",
                extra={
                    "doc_id": doc_id,
                    "requested": language.value,
                    "detected": actual_language.value,
                },
            )

        # act_type is inferred from the TITLE ONLY, never the meta/ToC block.
        # An act's own title states what it is; the table of contents is full of
        # references to *other* acts. Including it made the Criminal Code parse
        # as a constitution (its ToC cites "Конституция" 26 times), which would
        # have handed it the highest precedence in conflict resolution.
        return RawAct(
            external_id=str(doc_id),
            source=self.source,
            source_url=url,
            content=body_html.encode("utf-8"),
            mime_type="text/html",
            language=actual_language,
            act_type=_infer_type(title or ""),
            status=_infer_status(haystack),
            title=title,
            doc_number=_extract_doc_number(haystack),
            date_of_adoption=_extract_date(haystack),
            last_updated=_extract_date(meta_text, last=True),
            issuing_body=_extract_issuer(haystack),
            meta={"via": "html"},
        )

    async def fetch_all_languages(self, doc_id: str) -> list[RawAct]:
        """Fetch the distinct language editions reachable from one document id.

        On lex.uz a document id identifies ONE language edition — /uz/, /ru/
        and /en/ of the same id return byte-identical content. So this walks the
        URL prefixes but de-duplicates on content hash, and in practice returns
        a single act. It is kept for the case where lex.uz does vary content by
        prefix for some documents, and so callers have one obvious entry point.

        To genuinely index an act in several languages you need its sibling
        document ids (one per edition), not this method.
        """
        acts: list[RawAct] = []
        seen_hashes: set[str] = set()
        for language in (Language.UZ_LATN, Language.RU, Language.EN):
            act = await self.fetch_act(doc_id, language)
            if act is None:
                continue
            if act.content_hash in seen_hashes:
                continue
            seen_hashes.add(act.content_hash)
            acts.append(act)
        return acts

    # --------------------------------------------------------------- health
    async def validate_selectors(self, probe_doc_id: str = "111181") -> dict:
        """Fast check that the markup contract still holds. Run this in CI or a
        daily job — a silent selector break degrades to an empty corpus."""
        result: dict = {"doc_id": probe_doc_id, "ok": False, "found": {}}
        try:
            resp = await self.fetch(f"{self.base_url}/uz/docs/{probe_doc_id}")
        except Exception as exc:
            result["error"] = str(exc)
            return result

        soup = BeautifulSoup(resp.text, "lxml")
        for key in ("title", "body"):
            hit = next(
                (s for s in _SELECTORS[key] if soup.select_one(s)),
                None,
            )
            result["found"][key] = hit
        body = _first_html(soup, _SELECTORS["body"])
        result["body_length"] = len(body or "")
        result["ok"] = bool(result["found"].get("body")) and result["body_length"] > 1000
        return result


# ------------------------------------------------------------------ helpers


def _detect_content_language(text: str, requested: Language) -> Language:
    """Identify the language actually served, falling back to what we asked for.

    Uses the shared detector, which already distinguishes Uzbek Cyrillic from
    Russian by the ў/қ/ғ/ҳ letters that exist only in Uzbek.

    Samples from the MIDDLE of the document, not the head: lex.uz prefixes every
    page with Latin-script UI chrome and a long amendment-date list, which makes
    the opening of a Cyrillic act look script-"mixed" and pushes the detector
    down the wrong branch — so the answer would otherwise depend on which
    language happened to be requested rather than on the text itself.
    """
    if not text or len(text) < 200:
        return requested
    try:
        from app.services.lang.detect import detect_language

        # Take a window from the body, skipping the leading chrome.
        start = min(len(text) // 4, 5000)
        sample = text[start : start + 20000] or text
        return detect_language(sample, default=requested)
    except Exception:
        return requested


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        el = soup.select_one(selector)
        if el:
            text = normalize(el.get_text(" ", strip=True))
            if text:
                return text
    return None


def _first_html(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        el = soup.select_one(selector)
        if el and len(el.get_text(strip=True)) > 100:
            return str(el)
    return None


def _infer_type(text: str) -> ActType:
    for pattern, act_type in _TYPE_HINTS:
        if pattern.search(text or ""):
            return act_type
    return ActType.LAW


def _infer_status(text: str) -> ActStatus:
    for pattern, status in _STATUS_HINTS:
        if pattern.search(text or ""):
            return status
    return ActStatus.IN_FORCE


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value)
    iso = _ISO_DATE_RE.search(text)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    return _extract_date(text)


def _extract_date(text: str, *, last: bool = False) -> date | None:
    matches = _DATE_RE.findall(text or "")
    if not matches:
        return None
    day, month, year = matches[-1] if last else matches[0]
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _extract_doc_number(text: str) -> str | None:
    match = _DOC_NUMBER_RE.search(text or "")
    return match.group(1) if match else None


def _extract_issuer(text: str) -> str | None:
    issuers = [
        ("Oliy Majlis", r"oliy\s+majlis"),
        ("President of the Republic of Uzbekistan", r"prezident|президент"),
        ("Cabinet of Ministers", r"vazirlar\s+mahkamasi|кабинет\s+министров"),
        ("Constitutional Court", r"konstitutsiyaviy\s+sud"),
        ("Supreme Court", r"oliy\s+sud|верховн\w+\s+суд"),
    ]
    for label, pattern in issuers:
        if re.search(pattern, text or "", re.I):
            return label
    return None
