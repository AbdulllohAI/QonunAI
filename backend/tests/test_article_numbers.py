"""Sub-numbered article parsing.

Article 57¹ is a legally distinct provision from article 57 — different
subject, different penalties. lex.uz renders the superscript as a plain
space-separated digit (``Статья 57 1 .``), which the parser originally
dropped: every 57¹ and 57² collapsed onto ``"57"``, and the Uzbek form
(``57 1 -модда``) was not recognised as an article at all.

A regression here silently merges distinct provisions under one citation,
which is worse than failing loudly.
"""
from __future__ import annotations

import pytest

from app.db.models import NodeType
from app.services.ingestion.hierarchy_builder import classify, normalise_article_number


@pytest.mark.parametrize(
    "line,expected",
    [
        # Russian: superscript arrives as a space-separated digit.
        ("Статья 57. Назначение более мягкого наказания", "57"),
        ("Статья 57 1 . Назначение наказания при деятельном раскаянии", "57-1"),
        ("Статья 57 2 . Назначение наказания по преступлениям", "57-2"),
        ("Статья 18 1 . Ответственность лица", "18-1"),
        # Uzbek Cyrillic, number-first form.
        ("106-модда. Меҳнат шартномасининг шакли", "106"),
        ("57 1 -модда. Баъзи ҳоллар", "57-1"),
        ("57-1-модда. Boshqa holat", "57-1"),
        # Uzbek Latin.
        ("155-modda. Terrorchilik", "155"),
        ("155 2 -modda. Terrorchilik faoliyati", "155-2"),
        # English.
        ("Article 80. Collective agreements", "80"),
    ],
)
def test_article_numbers_parse(line: str, expected: str) -> None:
    node_type, number = classify(line)
    assert node_type is NodeType.MODDA, line
    assert normalise_article_number(number) == expected


def test_title_starting_with_a_number_is_not_swallowed() -> None:
    """"Статья 15 июня…" must yield 15, not 15-something.

    The space-separated sub-number only counts when a delimiter follows, which
    is what keeps ordinary prose out of the article number.
    """
    _, number = classify("Статья 15 июня установлен порядок")
    assert normalise_article_number(number) == "15"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("57", "57"),
        ("57-1", "57-1"),
        ("57 1", "57-1"),
        ("57 - 1", "57-1"),
        ("57–1", "57-1"),
        ("  57  ", "57"),
        ("", None),
        (None, None),
    ],
)
def test_normalise_collapses_every_separator(raw: str | None, expected: str | None) -> None:
    assert normalise_article_number(raw) == expected


def test_distinct_provisions_do_not_collide() -> None:
    """The whole point: 57, 57-1 and 57-2 must be three different keys."""
    numbers = {
        normalise_article_number(classify(line)[1])
        for line in (
            "Статья 57. Назначение более мягкого наказания",
            "Статья 57 1 . Назначение наказания при деятельном раскаянии",
            "Статья 57 2 . Назначение наказания по преступлениям",
        )
    }
    assert numbers == {"57", "57-1", "57-2"}
