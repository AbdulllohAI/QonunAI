"""Parser contract: bytes/markup in, a flat list of structural blocks out.

Structure detection is deliberately kept out of the parsers — they only recover
*text with a hint of its role* (heading vs body). `hierarchy.py` turns that flat
stream into the Qism → Bo'lim → Bob → Modda → Band tree, so the tree-building
logic lives in exactly one place regardless of whether the act arrived as HTML,
PDF or DOCX.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

BlockRole = Literal["heading", "body", "table", "footnote"]


@dataclass(slots=True)
class ParsedBlock:
    text: str
    role: BlockRole = "body"
    level: int | None = None
    """Visual heading level when the format exposes one (h1..h6, DOCX styles)."""
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class ParsedDocument:
    blocks: list[ParsedBlock] = field(default_factory=list)
    title: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


class BaseParser(ABC):
    @abstractmethod
    def parse(self, data: bytes | str, **kwargs) -> ParsedDocument:
        ...
