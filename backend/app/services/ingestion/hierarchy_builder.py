"""Build the Qism → Bo'lim → Bob → Modda → Band tree from a flat block stream.

This is the single place that understands Uzbek legislative structure. It runs a
small state machine over the parsed blocks: each block is classified by regex
into a structural level, and the current open-node stack is popped to that level
before the new node is pushed. Body text attaches to whatever node is on top.

Robustness matters more than elegance here — real acts have inconsistent
numbering, missing levels (a Law has articles but no chapters), and mixed
scripts. Anything unclassifiable becomes body text on the current node rather
than being dropped.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from app.db.models import Language, NodeType
from app.services.ingestion.parsers.base import ParsedBlock
from app.services.lang.translit import normalize

# Level ordering — a lower index is structurally higher.
_LEVEL_ORDER: list[NodeType] = [
    NodeType.QISM,
    NodeType.BOLIM,
    NodeType.BOB,
    NodeType.MODDA,
    NodeType.BAND,
    NodeType.QISMCHA,
]
_LEVEL_INDEX = {t: i for i, t in enumerate(_LEVEL_ORDER)}


@dataclass(slots=True)
class _Rule:
    node_type: NodeType
    pattern: re.Pattern[str]
    number_group: int = 1


# Order matters: the most specific patterns are tried first.
# Uzbek is written in BOTH Latin and Cyrillic, and the Cyrillic terms are
# distinct words from the Russian ones (модда ≠ статья, боб ≠ глава). Matching
# only Latin Uzbek + Russian silently yields ZERO articles for every Uzbek
# Cyrillic act — the text still indexes, but nothing is citable by article
# number and exact-article pinning can never hit it.
_RULES: list[_Rule] = [
    # --- QISM (Part) -----------------------------------------------------
    _Rule(NodeType.QISM, re.compile(r"^\s*(UMUMIY|MAXSUS|MAHSUS)\s+QISM\b", re.I), 1),
    _Rule(NodeType.QISM, re.compile(r"^\s*(УМУМИЙ|МАХСУС|МАҲСУС)\s+ҚИСМ\b", re.I), 1),
    _Rule(NodeType.QISM, re.compile(r"^\s*(ОБЩАЯ|ОСОБЕННАЯ)\s+ЧАСТЬ\b", re.I), 1),
    _Rule(NodeType.QISM, re.compile(r"^\s*(GENERAL|SPECIAL)\s+PART\b", re.I), 1),
    _Rule(NodeType.QISM, re.compile(r"^\s*([IVXLC]+|\d+)[-–\s.]*QISM\b", re.I), 1),
    _Rule(NodeType.QISM, re.compile(r"^\s*([IVXLC]+|\d+)[-–\s.]*ҚИСМ\b", re.I), 1),
    # --- BO'LIM (Section) ------------------------------------------------
    _Rule(NodeType.BOLIM, re.compile(r"^\s*([IVXLC]+|\d+)[-–\s.]*BO[’'ʻ‘]?LIM\b", re.I), 1),
    _Rule(NodeType.BOLIM, re.compile(r"^\s*([IVXLC]+|\d+)[-–\s.]*БЎЛИМ\b", re.I), 1),
    _Rule(
        NodeType.BOLIM,
        re.compile(r"^\s*(BIRINCHI|IKKINCHI|UCHINCHI|TO[’'ʻ‘]?RTINCHI|BESHINCHI|OLTINCHI|"
                   r"YETTINCHI|SAKKIZINCHI|TO[’'ʻ‘]?QQIZINCHI|O[’'ʻ‘]?NINCHI)\s+BO[’'ʻ‘]?LIM\b", re.I),
        1,
    ),
    _Rule(
        NodeType.BOLIM,
        re.compile(r"^\s*(БИРИНЧИ|ИККИНЧИ|УЧИНЧИ|ТЎРТИНЧИ|БЕШИНЧИ|ОЛТИНЧИ|"
                   r"ЕТТИНЧИ|САККИЗИНЧИ|ТЎҚҚИЗИНЧИ|ЎНИНЧИ)\s+БЎЛИМ\b", re.I),
        1,
    ),
    _Rule(NodeType.BOLIM, re.compile(r"^\s*РАЗДЕЛ\s+([IVXLC]+|\d+)", re.I), 1),
    _Rule(NodeType.BOLIM, re.compile(r"^\s*SECTION\s+([IVXLC]+|\d+)", re.I), 1),
    # --- BOB (Chapter) ---------------------------------------------------
    _Rule(NodeType.BOB, re.compile(r"^\s*([IVXLC]+|\d+)[-–\s.]*BOB\b", re.I), 1),
    _Rule(NodeType.BOB, re.compile(r"^\s*([IVXLC]+|\d+)[-–\s.]*БОБ\b", re.I), 1),
    _Rule(NodeType.BOB, re.compile(r"^\s*BOB\s*([IVXLC]+|\d+)", re.I), 1),
    _Rule(NodeType.BOB, re.compile(r"^\s*БОБ\s*([IVXLC]+|\d+)", re.I), 1),
    _Rule(NodeType.BOB, re.compile(r"^\s*ГЛАВА\s+([IVXLC]+|\d+)", re.I), 1),
    _Rule(NodeType.BOB, re.compile(r"^\s*CHAPTER\s+([IVXLC]+|\d+)", re.I), 1),
    # --- MODDA (Article) — the citable unit -------------------------------
    _Rule(NodeType.MODDA, re.compile(r"^\s*(\d+(?:[-–]\d+)?)\s*[-–]?\s*MODDA\b", re.I), 1),
    _Rule(NodeType.MODDA, re.compile(r"^\s*(\d+(?:[-–]\d+)?)\s*[-–]?\s*МОДДА\b", re.I), 1),
    _Rule(NodeType.MODDA, re.compile(r"^\s*MODDA\s*[-–]?\s*(\d+(?:[-–]\d+)?)", re.I), 1),
    _Rule(NodeType.MODDA, re.compile(r"^\s*МОДДА\s*[-–]?\s*(\d+(?:[-–]\d+)?)", re.I), 1),
    _Rule(NodeType.MODDA, re.compile(r"^\s*СТАТЬЯ\s*(\d+(?:[-–]\d+)?)", re.I), 1),
    _Rule(NodeType.MODDA, re.compile(r"^\s*ARTICLE\s*(\d+(?:[-–]\d+)?)", re.I), 1),
    # --- BAND (Clause) ----------------------------------------------------
    _Rule(NodeType.BAND, re.compile(r"^\s*(\d+)\s*\)\s+"), 1),
    _Rule(NodeType.BAND, re.compile(r"^\s*(\d+)\.\s+(?=[A-ZА-ЯЎҚҒҲ«\"])"), 1),
    # --- QISMCHA (Sub-clause) --------------------------------------------
    _Rule(NodeType.QISMCHA, re.compile(r"^\s*([a-zа-я])\s*\)\s+"), 1),
]

_ANNEX_RE = re.compile(r"^\s*(ILOVA|ИЛОВА|ПРИЛОЖЕНИЕ|ANNEX|APPENDIX)\b", re.I)
_PREAMBLE_RE = re.compile(r"^\s*(MUQADDIMA|МУҚАДДИМА|ПРЕАМБУЛА|PREAMBLE)\b", re.I)


@dataclass
class BuiltNode:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    node_type: NodeType = NodeType.BOB
    number: str | None = None
    heading: str | None = None
    body_parts: list[str] = field(default_factory=list)
    parent_id: uuid.UUID | None = None
    article_number: str | None = None
    path: str = ""
    ordinal: int = 0
    language: Language = Language.UZ_LATN
    meta: dict = field(default_factory=dict)

    @property
    def body(self) -> str:
        return "\n".join(p for p in self.body_parts if p).strip()

    @property
    def level(self) -> int:
        return _LEVEL_INDEX.get(self.node_type, len(_LEVEL_ORDER))


def classify(text: str) -> tuple[NodeType, str] | None:
    """Return (node_type, number) if `text` opens a structural node."""
    stripped = normalize(text)
    if not stripped:
        return None
    if _ANNEX_RE.match(stripped):
        return NodeType.ANNEX, ""
    if _PREAMBLE_RE.match(stripped):
        return NodeType.PREAMBLE, ""
    for rule in _RULES:
        match = rule.pattern.match(stripped)
        if match:
            number = (match.group(rule.number_group) or "").strip().replace("–", "-")
            return rule.node_type, number
    return None


def _split_heading(text: str, node_type: NodeType) -> str | None:
    """Articles print as '54-modda. Shartnoma tushunchasi' — return the title part."""
    parts = re.split(r"[.:]\s+", normalize(text), maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    # Chapters often put the title on the same line after the number, no period.
    if node_type in (NodeType.BOB, NodeType.BOLIM, NodeType.QISM):
        cleaned = re.sub(
            r"^\s*(?:[IVXLC]+|\d+)[-–\s.]*(?:QISM|BO[’'ʻ‘]?LIM|BOB|ГЛАВА|РАЗДЕЛ|CHAPTER|SECTION)\b[.\s—–-]*",
            "",
            normalize(text),
            flags=re.I,
        )
        return cleaned.strip() or None
    return None


class HierarchyBuilder:
    def build(
        self, blocks: list[ParsedBlock], *, language: Language
    ) -> list[BuiltNode]:
        nodes: list[BuiltNode] = []
        stack: list[BuiltNode] = []
        ordinal_by_parent: dict[uuid.UUID | None, int] = {}

        for block in blocks:
            text = normalize(block.text)
            if not text:
                continue

            classified = classify(text) if block.role in ("heading", "body") else None

            if classified is None:
                # Body text belongs to the deepest open node.
                if stack:
                    stack[-1].body_parts.append(text)
                else:
                    # Text before any structural marker — a preamble in practice.
                    preamble = BuiltNode(
                        node_type=NodeType.PREAMBLE, language=language, path="preamble"
                    )
                    preamble.body_parts.append(text)
                    nodes.append(preamble)
                    stack.append(preamble)
                continue

            node_type, number = classified
            node = BuiltNode(
                node_type=node_type,
                number=number or None,
                heading=_split_heading(text, node_type),
                language=language,
            )

            # Pop the stack to this node's level.
            level = _LEVEL_INDEX.get(node_type, len(_LEVEL_ORDER))
            while stack and stack[-1].level >= level:
                stack.pop()

            parent = stack[-1] if stack else None
            node.parent_id = parent.id if parent else None
            node.ordinal = ordinal_by_parent.get(node.parent_id, 0)
            ordinal_by_parent[node.parent_id] = node.ordinal + 1

            # Denormalise the governing article number down the subtree.
            if node_type is NodeType.MODDA:
                node.article_number = number or None
            elif parent is not None:
                node.article_number = parent.article_number

            node.path = _join_path(parent.path if parent else "", node)

            # For articles, the heading line's trailing text is the title, not body.
            # For everything else, keep any residual text as body.
            if node_type not in (NodeType.MODDA, NodeType.BOB, NodeType.BOLIM, NodeType.QISM):
                residual = _strip_marker(text, node_type)
                if residual:
                    node.body_parts.append(residual)

            nodes.append(node)
            stack.append(node)

        return [n for n in nodes if n.body or n.heading or n.node_type is NodeType.MODDA]


def _join_path(parent_path: str, node: BuiltNode) -> str:
    label = node.number or node.node_type.value
    return f"{parent_path}/{label}" if parent_path else label


def _strip_marker(text: str, node_type: NodeType) -> str:
    """Remove the '1)' / 'a)' marker, keeping the clause text."""
    if node_type in (NodeType.BAND, NodeType.QISMCHA):
        return re.sub(r"^\s*[\da-zа-я]+\s*[).]\s*", "", normalize(text)).strip()
    return normalize(text)


hierarchy_builder = HierarchyBuilder()
