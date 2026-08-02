"""Article-level deep links into lex.uz.

lex.uz gives every structural node a stable numeric id and exposes it in the
document's table of contents as ``scrollText('<id>')``. That handler runs
``history.pushState(null, '', '#' + hash)``, and the matching element in the
body carries ``id``/``name`` with the same value — so
``https://lex.uz/docs/6257288#6259020`` is a real, cold-loadable deep link to
article 80. Their ``window.onload`` reads ``location.hash`` and scrolls, which
is what makes a pasted link work in a fresh tab. No DOM-search fallback needed.

Two things make naive extraction wrong, both found by measurement:

1. **Sub-numbered articles.** lex.uz renders article 57¹ as the literal text
   ``Статья 57 1 .`` — a space-separated digit, not a superscript entity. Parsing
   only the leading number collapses 57, 57¹ and 57² into one key. These are
   legally distinct provisions, so we normalise them to ``57``, ``57-1``, ``57-2``.

2. **The corpus already collapsed them.** Existing ``chunks.article_number``
   values contain no separators at all, so articles 57, 57¹ and 57² are all
   stored as ``"57"``. Matching an anchor to a chunk on article number alone
   would therefore mis-link two of every three. :func:`match_anchor` disambiguates
   on the heading instead.

Measured: Labour Code (uz-Cyrl) 581/581 anchors resolve; Criminal Code (ru)
404/404 including 109 sub-numbered articles.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "ArticleAnchor",
    "extract_anchors",
    "verify_anchors",
    "match_anchor",
    "build_deep_link",
]


@dataclass(frozen=True, slots=True)
class ArticleAnchor:
    """One citable article and the fragment that scrolls to it."""

    article_number: str
    """Normalised: ``"57"``, ``"57-1"``. Sub-numbers use a hyphen."""

    anchor_id: str
    """lex.uz node id used as the URL fragment."""

    heading: str
    """Article title, used to disambiguate collapsed article numbers."""


#: TOC entries look like ``scrollText('6259020');">80-модда. Heading``.
_TOC_ENTRY = re.compile(
    r"scrollText\(\s*['\"](?P<anchor>\d+)['\"]\s*\)\s*;?\s*[\"']?\s*>\s*(?P<label>[^<]{0,200})",
    re.UNICODE,
)

#: Uzbek prints the number first (``80-модда``, ``57 1 -модда``); Russian prints
#: it after the marker (``Статья 80``, ``Статья 57 1 .``). The optional second
#: digit group is the superscript sub-number rendered as plain text.
_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?P<num>\d+)(?:\s+(?P<sub>\d+))?\s*[-–]\s*(?:модда|modda)\b\s*[.．]?",
        re.IGNORECASE | re.UNICODE,
    ),
    re.compile(
        r"^\s*статья\s+(?P<num>\d+)(?:\s+(?P<sub>\d+))?\s*[.．]",
        re.IGNORECASE | re.UNICODE,
    ),
)

_ELEMENT_IDS = re.compile(r"""\bid\s*=\s*["'](\d+)["']""", re.UNICODE)

#: Strips the ``57-modda.`` / ``Статья 57.`` prefix so headings compare cleanly.
_HEADING_PREFIX = re.compile(
    r"^\s*(?:\d+(?:\s+\d+)?\s*[-–]\s*(?:модда|modda)|статья\s+\d+(?:\s+\d+)?)\s*[.．]?\s*",
    re.IGNORECASE | re.UNICODE,
)


def _parse_label(label: str) -> tuple[str, str] | None:
    """Return ``(article_number, heading)`` if the label names an article.

    Chapter and section headings ("Глава I", "9-боб") return None — only
    articles are citable, so only articles get anchors.
    """
    for pattern in _LABEL_PATTERNS:
        m = pattern.match(label)
        if not m:
            continue
        number = m.group("num")
        if m.group("sub"):
            number = f"{number}-{m.group('sub')}"
        return number, label[m.end() :].strip()
    return None


def normalise_heading(text: str | None) -> str:
    """Casefold, strip any article prefix, and collapse whitespace.

    Used only for comparison, never for display.
    """
    if not text:
        return ""
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = _HEADING_PREFIX.sub("", cleaned)
    return " ".join(cleaned.split()).casefold()


def extract_anchors(html: str) -> list[ArticleAnchor]:
    """Extract every citable article anchor from a lex.uz document.

    Returns a list rather than a dict because article numbers are not unique
    once lex.uz's sub-numbering is taken into account, and because the heading
    is needed downstream to disambiguate.
    """
    out: list[ArticleAnchor] = []
    seen: set[tuple[str, str]] = set()
    for match in _TOC_ENTRY.finditer(html):
        parsed = _parse_label(match.group("label"))
        if parsed is None:
            continue
        number, heading = parsed
        key = (number, match.group("anchor"))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ArticleAnchor(article_number=number, anchor_id=match.group("anchor"), heading=heading)
        )
    return out


def verify_anchors(html: str, anchors: list[ArticleAnchor]) -> list[ArticleAnchor]:
    """Drop anchors with no matching element id in the document.

    lex.uz renumbers nodes when an act is amended, so an anchor from an older
    crawl can go stale. A link that scrolls nowhere is worse than a
    document-level link, so unverifiable anchors are discarded.
    """
    present = set(_ELEMENT_IDS.findall(html))
    return [a for a in anchors if a.anchor_id in present]


def match_anchor(
    anchors: list[ArticleAnchor],
    article_number: str | None,
    heading: str | None,
) -> ArticleAnchor | None:
    """Find the anchor for a stored chunk.

    Because the existing corpus collapsed sub-numbered articles (57, 57¹ and 57²
    all stored as ``"57"``), an article number can be ambiguous. When it is, the
    heading decides. If the heading cannot break the tie we return None rather
    than guess — a document-level link is acceptable, a link to the wrong
    article is not.
    """
    if not article_number:
        return None

    # Candidates are anchors whose base number matches, i.e. "57" also considers
    # "57-1" and "57-2", since the corpus cannot distinguish them by number.
    base = article_number.split("-")[0]
    candidates = [a for a in anchors if a.article_number.split("-")[0] == base]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    target = normalise_heading(heading)
    if not target:
        return None
    for candidate in candidates:
        if normalise_heading(candidate.heading) == target:
            return candidate

    # Fall back to prefix containment — chunk headings are sometimes truncated.
    partial = [
        c
        for c in candidates
        if target.startswith(normalise_heading(c.heading)[:40])
        or normalise_heading(c.heading).startswith(target[:40])
    ]
    return partial[0] if len(partial) == 1 else None


def build_deep_link(source_url: str | None, anchor_id: str | None) -> str | None:
    """Attach an article anchor to a lex.uz document URL.

    Degrades to the plain document URL when the anchor is unknown — a citation
    that opens the right law is still useful, it just costs the reader a scroll.
    """
    if not source_url:
        return None
    if not anchor_id:
        return source_url

    parts = urlsplit(source_url)
    if "lex.uz" not in parts.netloc:
        # Only lex.uz uses this anchor scheme; never rewrite a foreign URL.
        return source_url
    return urlunsplit(parts._replace(fragment=anchor_id))
