"""PDF parser with layout-aware heading detection and an OCR fallback.

Many older Uzbek acts circulate only as scanned PDFs. When a page yields no
extractable text, OCR runs with the `uzb`/`rus` Tesseract models — without them,
scanned acts silently ingest as empty documents, which is worse than failing.
"""
from __future__ import annotations

import io
import re
import statistics

from app.core.logging import get_logger
from app.services.ingestion.parsers.base import BaseParser, ParsedBlock, ParsedDocument
from app.services.ingestion.parsers.html_parser import _HEADING_RE
from app.services.lang.translit import normalize

log = get_logger(__name__)

_PAGE_NOISE = re.compile(
    r"^\s*(\d+\s*/\s*\d+|[-–—]\s*\d+\s*[-–—]|стр\.?\s*\d+|page\s+\d+|\d+)\s*$",
    re.IGNORECASE,
)


class PdfParser(BaseParser):
    def __init__(self, ocr_languages: str = "uzb+rus+eng") -> None:
        self.ocr_languages = ocr_languages

    def parse(self, data: bytes | str, *, allow_ocr: bool = True, **kwargs) -> ParsedDocument:
        raw = data.encode() if isinstance(data, str) else data
        import pdfplumber

        blocks: list[ParsedBlock] = []
        title: str | None = None
        ocr_pages = 0

        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            meta_title = (pdf.metadata or {}).get("Title")
            if meta_title:
                title = normalize(meta_title)

            # Modal font size across the document ≈ body text; anything larger
            # and short is very likely a heading.
            sizes = [
                round(ch["size"], 1)
                for page in pdf.pages[:20]
                for ch in (page.chars or [])
            ]
            body_size = statistics.mode(sizes) if sizes else 10.0

            for page_no, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                if len(text.strip()) < 30 and allow_ocr:
                    ocr_text = self._ocr_page(page)
                    if ocr_text:
                        text = ocr_text
                        ocr_pages += 1

                page_sizes = _line_sizes(page)
                for line in text.splitlines():
                    clean = normalize(line)
                    if not clean or _PAGE_NOISE.match(clean):
                        continue
                    size = page_sizes.get(clean[:40], body_size)
                    is_heading = (
                        bool(_HEADING_RE.match(clean))
                        or (size > body_size * 1.15 and len(clean) < 200)
                        or (clean.isupper() and 6 < len(clean) < 200)
                    )
                    blocks.append(
                        ParsedBlock(
                            text=clean,
                            role="heading" if is_heading else "body",
                            level=4 if is_heading else None,
                            meta={"page": page_no},
                        )
                    )

        merged = _merge_wrapped_lines(blocks)
        if not title and merged:
            title = merged[0].text[:300]

        return ParsedDocument(
            blocks=merged,
            title=title,
            meta={"format": "pdf", "ocr_pages": ocr_pages},
        )

    def _ocr_page(self, page) -> str:
        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            log.warning("OCR requested but pytesseract/Pillow not installed")
            return ""
        try:
            image = page.to_image(resolution=300).original
            if not isinstance(image, Image.Image):
                image = Image.frombytes("RGB", image.size, image.tobytes())
            return pytesseract.image_to_string(image, lang=self.ocr_languages)
        except Exception as exc:
            log.warning("OCR failed", extra={"error": str(exc)})
            return ""


def _line_sizes(page) -> dict[str, float]:
    """Approximate font size per line, keyed by a text prefix."""
    sizes: dict[str, float] = {}
    try:
        for line in page.extract_text_lines() or []:
            key = normalize(line.get("text", ""))[:40]
            chars = line.get("chars") or []
            if key and chars:
                sizes[key] = statistics.mean(c["size"] for c in chars)
    except Exception:
        pass
    return sizes


def _merge_wrapped_lines(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
    """Rejoin body lines split by PDF line wrapping.

    A line that does not end in sentence punctuation and is followed by one
    starting lowercase is a continuation, not a new paragraph.
    """
    out: list[ParsedBlock] = []
    for block in blocks:
        if (
            out
            and block.role == "body"
            and out[-1].role == "body"
            and not re.search(r"[.;:!?»)]\s*$", out[-1].text)
            and block.text[:1].islower()
        ):
            out[-1].text = f"{out[-1].text} {block.text}"
            continue
        out.append(block)
    return out
