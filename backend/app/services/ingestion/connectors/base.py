"""Connector base: polite HTTP with rate limiting, retry/backoff and robots.txt.

Every connector shares this client. The politeness controls are not optional
niceties — lex.uz is state infrastructure serving the public, and an ingestion
job that hammers it is both a reliability risk and a legal-exposure risk for the
operator. Defaults are conservative (2 req/s, 4 concurrent) and configurable.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ActStatus, ActType, Language, SourceSystem

log = get_logger(__name__)


@dataclass(slots=True)
class RawAct:
    """What a connector yields: enough to build a LegalAct plus the raw payload."""

    external_id: str
    source: SourceSystem
    source_url: str
    content: bytes
    mime_type: str
    language: Language
    act_type: ActType = ActType.LAW
    status: ActStatus = ActStatus.IN_FORCE
    title: str | None = None
    doc_number: str | None = None
    date_of_adoption: date | None = None
    date_in_force: date | None = None
    last_updated: date | None = None
    issuing_body: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class RateLimiter:
    """Token-bucket-ish limiter: enforces a minimum interval between requests."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps if rps > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class BaseConnector(ABC):
    name: str
    source: SourceSystem
    base_url: str

    def __init__(
        self,
        *,
        rps: float | None = None,
        concurrency: int | None = None,
        respect_robots: bool = True,
    ) -> None:
        self._limiter = RateLimiter(rps or settings.INGEST_RATE_LIMIT_RPS)
        self._semaphore = asyncio.Semaphore(concurrency or settings.INGEST_CONCURRENCY)
        self._client: httpx.AsyncClient | None = None
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._respect_robots = respect_robots

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=settings.INGEST_TIMEOUT_S,
            follow_redirects=True,
            headers={
                "User-Agent": settings.INGEST_USER_AGENT,
                "Accept-Language": "uz,ru;q=0.9,en;q=0.8",
            },
        )
        if self._respect_robots:
            await self._load_robots()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _load_robots(self) -> None:
        parser = urllib.robotparser.RobotFileParser()
        url = urljoin(self.base_url, "/robots.txt")
        try:
            resp = await self._client.get(url)  # type: ignore[union-attr]
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
                self._robots = parser
                log.info("robots.txt loaded", extra={"connector": self.name})
        except Exception as exc:
            # No robots.txt is not permission to ignore rate limits, but it is
            # not a reason to abort either.
            log.warning("robots.txt unavailable", extra={"connector": self.name, "error": str(exc)})

    def allowed(self, url: str) -> bool:
        if not self._robots:
            return True
        return self._robots.can_fetch(settings.INGEST_USER_AGENT, url)

    # ----------------------------------------------------------------- fetch
    async def fetch(self, url: str, **kwargs) -> httpx.Response:
        """GET with rate limiting and exponential backoff + jitter."""
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        assert self._client is not None, "use `async with connector:`"

        last_exc: Exception | None = None
        for attempt in range(settings.INGEST_MAX_RETRIES):
            async with self._semaphore:
                await self._limiter.acquire()
                try:
                    resp = await self._client.get(url, **kwargs)
                except httpx.HTTPError as exc:
                    last_exc = exc
                else:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        retry_after = resp.headers.get("retry-after")
                        delay = (
                            float(retry_after)
                            if retry_after and retry_after.isdigit()
                            else _backoff(attempt)
                        )
                        log.warning(
                            "retrying",
                            extra={
                                "url": url,
                                "status": resp.status_code,
                                "attempt": attempt + 1,
                                "delay_s": round(delay, 2),
                            },
                        )
                        await asyncio.sleep(delay)
                        continue
                    resp.raise_for_status()
                    return resp

            await asyncio.sleep(_backoff(attempt))

        raise RuntimeError(f"fetch failed after {settings.INGEST_MAX_RETRIES} attempts: {url}") from last_exc

    # ---------------------------------------------------------------- contract
    @abstractmethod
    def discover(self, **kwargs) -> AsyncIterator[str]:
        """Yield document identifiers/URLs to ingest."""
        ...

    @abstractmethod
    async def fetch_act(self, identifier: str, language: Language) -> RawAct | None:
        ...

    async def health(self) -> bool:
        try:
            await self.fetch(self.base_url)
            return True
        except Exception:
            return False


def _backoff(attempt: int, base: float = 1.5, cap: float = 60.0) -> float:
    return min(cap, base * (2**attempt)) * (0.5 + random.random() / 2)


def same_host(url: str, base: str) -> bool:
    return urlparse(url).netloc.lower().lstrip("www.") == urlparse(base).netloc.lower().lstrip("www.")
