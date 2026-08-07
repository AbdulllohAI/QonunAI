"""Parsing exact legal references out of a question.

"Jinoyat kodeksi 155-moddasi 3-qismi" names one provision precisely, and a
system that answers it by embedding similarity is doing unnecessary and
error-prone work. Parsed references let retrieval fetch the article directly.

The parser is structured rather than number-scraping because the previous
approach had a real defect: scanning independently for "article N" and
"модда N" patterns meant ``155-модда 3-қисми`` matched twice — once correctly
as article 155, and once as *article 3*, because "модда ... 3" appears in the
text when the part number follows. Retrieval then pinned an unrelated article.

Here each reference is consumed as a whole (article, then optional part, then
optional clause) and its span is masked before the next pass, so a part number
can never be re-read as an article.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["LegalReference", "parse_references", "extract_article_numbers"]


@dataclass(frozen=True, slots=True)
class LegalReference:
    """One provision named in a question."""

    article: str
    """Normalised article number: ``"155"``, ``"155-1"``."""

    part: str | None = None
    """Qism / часть / part, when named."""

    clause: str | None = None
    """Band / пункт / clause, when named."""

    def citation_suffix(self) -> str:
        """Human-readable tail for a citation, e.g. ``" part 3, clause 2"``."""
        bits = []
        if self.part:
            bits.append(f"part {self.part}")
        if self.clause:
            bits.append(f"clause {self.clause}")
        return (", " + ", ".join(bits)) if bits else ""


#: Article number: 155, 155-1, and the space-separated superscript lex.uz emits.
_NUM = r"\d+(?:\s*[-–]\s*\d+)?"

_ARTICLE_WORD = r"(?:modda|модда|стать[яеиюйи]|ст\.?|article|art\.?)"
_PART_WORD = r"(?:qism|қисм|част[ьи]|part)"
_CLAUSE_WORD = r"(?:band|банд|пункт|clause|point|item)"

#: Uzbek suffixes the marker ("155-moddasi"); Russian and English prefix it
#: ("статья 155"). Both forms allow an optional trailing part and clause, and
#: crucially both are consumed in a single match so the part number cannot be
#: re-read as another article.
_SUFFIXED = re.compile(
    rf"(?P<article>{_NUM})\s*[-–]?\s*{_ARTICLE_WORD}\w*"
    rf"(?:\W{{0,4}}(?P<part>{_NUM})\s*[-–]?\s*{_PART_WORD}\w*)?"
    rf"(?:\W{{0,4}}(?P<clause>{_NUM})\s*[-–]?\s*{_CLAUSE_WORD}\w*)?",
    re.IGNORECASE | re.UNICODE,
)

_PREFIXED = re.compile(
    rf"{_ARTICLE_WORD}\s*[-–]?\s*(?P<article>{_NUM})"
    rf"(?:\W{{0,4}}{_PART_WORD}\w*\s*[-–]?\s*(?P<part>{_NUM}))?"
    rf"(?:\W{{0,4}}{_CLAUSE_WORD}\w*\s*[-–]?\s*(?P<clause>{_NUM}))?",
    re.IGNORECASE | re.UNICODE,
)


def _normalise(number: str | None) -> str | None:
    """Collapse ``"155 - 1"`` / ``"155–1"`` to ``"155-1"``."""
    if not number:
        return None
    return re.sub(r"\s*[-–]\s*", "-", number.strip())


def parse_references(query: str) -> list[LegalReference]:
    """Structured references, in the order they appear.

    Document order matters: when a question names several provisions, the first
    is usually the subject and the rest are context.
    """
    masked = query
    found: list[tuple[int, LegalReference]] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    # Suffixed form first. It is the Uzbek convention and the more specific
    # match, so consuming it before the prefixed pass avoids the prefixed
    # pattern claiming half of it.
    for pattern in (_SUFFIXED, _PREFIXED):
        for match in pattern.finditer(masked):
            article = _normalise(match.group("article"))
            if not article:
                continue
            ref = LegalReference(
                article=article,
                part=_normalise(match.group("part")),
                clause=_normalise(match.group("clause")),
            )
            key = (ref.article, ref.part, ref.clause)
            if key not in seen:
                seen.add(key)
                found.append((match.start(), ref))
        # Blank out everything matched so the next pattern cannot re-read a
        # part or clause number as an article.
        masked = pattern.sub(lambda m: " " * len(m.group(0)), masked)

    return [ref for _, ref in sorted(found, key=lambda pair: pair[0])]


def extract_article_numbers(query: str) -> list[str]:
    """Article numbers only, preserved for callers that don't need structure."""
    return [ref.article for ref in parse_references(query)]
