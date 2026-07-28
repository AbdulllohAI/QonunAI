"""HTML parser for lex.uz / norma.uz act pages."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

from app.services.ingestion.parsers.base import BaseParser, ParsedBlock, ParsedDocument
from app.services.lang.translit import normalize

_DROP_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "form", "svg")
_DROP_SELECTORS = (
    ".breadcrumb", ".navbar", ".sidebar", ".menu", ".pagination",
    "#header", "#footer", ".no-print", ".doc-tools", ".share",
    # --- lex.uz specific chrome ---
    # The table of contents and side navigation repeat every article heading;
    # left in, they double the corpus with contentless duplicates.
    "#dvToc", ".docNavbar", ".docBody__sidebar", "#docBody__sidebar",
    # Editorial annotations, not enacted text: "see previous version" links,
    # amendment provenance, and lex.uz's own commentary. Indexing these as if
    # they were statute is how a RAG system ends up citing a footnote as law.
    ".COMMENT", ".COMMENTLEXUZ", ".CHANGES_ORIGINS", ".INDEXES_ON_REF",
    ".lx_no_select", ".lx_date_link",
)

# lex.uz wraps act bodies in one of these; fall back to <body> if none match.
# Verified against live lex.uz: #divCont is the real container (~1 MB of text);
# the rest are other layouts and legacy fallbacks.
_CONTENT_SELECTORS = (
    "#divCont", "#divBody", "#mD", ".main-column.document",
    "#main_container1", ".docBody__container",
    "#doc_content", ".document-body", ".doc-text", "#content_text",
    ".act-content", "article", "main", ".content",
)

# lex.uz injects a per-element toolbar as an unclassed <span> inside every
# content block, so it cannot be removed by selector. These are the exact
# button labels; matched case-insensitively against a block's whole text.
# Dropping by known literal is safer than dropping the wrapper element, which
# would risk taking real statutory text with it.
_CHROME_PHRASES = (
    "hujjatga taklif yuborish",
    "audioni tinglash",
    "hujjat elementidan havola olish",
    "предложить изменение",
    "прослушать аудио",
    "получить ссылку на элемент",
)


def _is_chrome(text: str) -> bool:
    """True when a block is nothing but lex.uz UI labels."""
    stripped = text.strip().lower()
    if not stripped:
        return True
    for phrase in _CHROME_PHRASES:
        stripped = stripped.replace(phrase, "")
    # Only chrome (plus separators) remained.
    return len(re.sub(r"[\s|/·—–-]+", "", stripped)) == 0


# Covers Uzbek Latin, Uzbek Cyrillic, Russian and English. The Uzbek Cyrillic
# forms (модда/боб/бўлим/қисм) are distinct words from the Russian ones and
# must be listed separately — omitting them leaves Cyrillic Uzbek acts with no
# detected headings at all, so nothing downstream can cite them by article.
_HEADING_RE = re.compile(
    r"^\s*("
    r"\d+(?:-\d+)?\s*[-–]?\s*modda"      # 54-modda
    r"|modda\s*\d+"
    r"|\d+(?:-\d+)?\s*[-–]?\s*модда"     # 54-модда (Uzbek Cyrillic)
    r"|модда\s*\d+"
    r"|стать[яеий]\s*\d+"
    r"|article\s+\d+"
    r"|[IVXLC]+\s*[-–.]?\s*bob"           # I bob
    r"|\d+\s*[-–]?\s*bob"
    r"|[IVXLC]+\s*[-–.]?\s*боб"           # I боб
    r"|\d+\s*[-–]?\s*боб"
    r"|глава\s+[IVXLC\d]+"
    r"|chapter\s+[IVXLC\d]+"
    r"|[IVXLC]+\s*[-–]?\s*bo[’'ʻ‘]?lim"
    r"|[IVXLC]+\s*[-–]?\s*бўлим"
    r"|раздел\s+[IVXLC\d]+"
    r"|[А-ЯA-ZЎҚҒҲ\s]{6,}QISM"
    r"|[А-ЯЎҚҒҲ\s]{6,}ҚИСМ"
    r"|УМУМИЙ\s+ҚИСМ|МАХСУС\s+ҚИСМ"
    r"|ОБЩАЯ\s+ЧАСТЬ|ОСОБЕННАЯ\s+ЧАСТЬ"
    r")",
    re.IGNORECASE,
)


class HtmlParser(BaseParser):
    def parse(self, data: bytes | str, **kwargs) -> ParsedDocument:
        markup = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        soup = BeautifulSoup(markup, "lxml")

        for tag in soup(_DROP_TAGS):
            tag.decompose()
        for selector in _DROP_SELECTORS:
            for tag in soup.select(selector):
                tag.decompose()

        root: Tag | None = None
        for selector in _CONTENT_SELECTORS:
            root = soup.select_one(selector)
            if root and len(root.get_text(strip=True)) > 200:
                break
            root = None
        root = root or soup.body or soup

        title_tag = soup.find(["h1", "title"])
        title = normalize(title_tag.get_text(" ", strip=True)) if title_tag else None

        blocks: list[ParsedBlock] = []
        for element in root.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "li", "td", "pre"]
        ):
            # Skip containers whose text belongs to their children — otherwise
            # every paragraph is emitted once per ancestor.
            if element.name == "div" and element.find(
                ["p", "div", "li", "h1", "h2", "h3", "h4"]
            ):
                continue

            text = normalize(_inline_text(element))
            if len(text) < 2 or _is_chrome(text):
                continue

            # Strip the toolbar labels that lex.uz interleaves with real text
            # inside the same block, then re-check the remainder is substantive.
            for phrase in _CHROME_PHRASES:
                text = re.sub(re.escape(phrase), "", text, flags=re.IGNORECASE)
            text = normalize(text)
            if len(text) < 2:
                continue

            if element.name.startswith("h") and element.name[1:].isdigit():
                blocks.append(
                    ParsedBlock(text=text, role="heading", level=int(element.name[1]))
                )
            elif _HEADING_RE.match(text) and len(text) < 400:
                blocks.append(ParsedBlock(text=text, role="heading", level=4))
            elif element.name == "td":
                blocks.append(ParsedBlock(text=text, role="table"))
            else:
                blocks.append(ParsedBlock(text=text, role="body"))

        return ParsedDocument(blocks=_dedupe(blocks), title=title, meta={"format": "html"})


def _inline_text(element: Tag) -> str:
    parts: list[str] = []
    for child in element.descendants:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag) and child.name == "br":
            parts.append("\n")
    return re.sub(r"\n{3,}", "\n\n", " ".join(parts))


def _dedupe(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
    """Drop consecutive duplicates — common when markup nests the same text."""
    out: list[ParsedBlock] = []
    for block in blocks:
        if out and out[-1].text == block.text:
            continue
        out.append(block)
    return out
