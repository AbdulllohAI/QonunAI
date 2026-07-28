"""Format dispatch for ingestion parsers."""
from __future__ import annotations

from app.services.ingestion.parsers.base import BaseParser, ParsedBlock, ParsedDocument
from app.services.ingestion.parsers.docx_parser import DocxParser
from app.services.ingestion.parsers.html_parser import HtmlParser
from app.services.ingestion.parsers.pdf_parser import PdfParser

html_parser = HtmlParser()
pdf_parser = PdfParser()
docx_parser = DocxParser()

_BY_MIME = {
    "text/html": html_parser,
    "application/xhtml+xml": html_parser,
    "application/pdf": pdf_parser,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": docx_parser,
    "application/msword": docx_parser,
}

_BY_EXT = {
    ".html": html_parser,
    ".htm": html_parser,
    ".pdf": pdf_parser,
    ".docx": docx_parser,
    ".doc": docx_parser,
}


def get_parser(*, mime_type: str | None = None, filename: str | None = None) -> BaseParser:
    if mime_type:
        parser = _BY_MIME.get(mime_type.split(";")[0].strip().lower())
        if parser:
            return parser
    if filename:
        for ext, parser in _BY_EXT.items():
            if filename.lower().endswith(ext):
                return parser
    raise ValueError(f"unsupported document format (mime={mime_type}, file={filename})")


def parse_document(
    data: bytes, *, mime_type: str | None = None, filename: str | None = None
) -> ParsedDocument:
    return get_parser(mime_type=mime_type, filename=filename).parse(data)


__all__ = [
    "BaseParser",
    "ParsedBlock",
    "ParsedDocument",
    "HtmlParser",
    "PdfParser",
    "DocxParser",
    "get_parser",
    "parse_document",
    "html_parser",
    "pdf_parser",
    "docx_parser",
]
