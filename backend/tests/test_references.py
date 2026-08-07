"""Exact legal reference parsing.

The defect these guard against was live: scanning independently for "article N"
and "модда N" meant ``155-модда 3-қисми`` matched twice — correctly as article
155, and again as *article 3*, because the part number trails the article word.
Retrieval then pinned an unrelated article alongside the right one.
"""
from __future__ import annotations

import pytest

from app.services.rag.references import (
    LegalReference,
    extract_article_numbers,
    parse_references,
)


@pytest.mark.parametrize(
    "query,article,part,clause",
    [
        # The forms named in the product spec.
        ("Jinoyat kodeksi 155-moddasi 3-qismi", "155", "3", None),
        ("Mehnat kodeksi 106-modda", "106", None, None),
        ("Fuqarolik kodeksi 985-modda", "985", None, None),
        # Sub-numbered articles are legally distinct provisions.
        ("Jinoyat kodeksi 155-1-moddasi", "155-1", None, None),
        # Russian prefixes the marker.
        ("Уголовный кодекс статья 137 часть 2", "137", "2", None),
        ("статья 137", "137", None, None),
        # Uzbek Cyrillic, the case that previously produced a phantom article.
        ("ЖК 155-модда 3-қисми", "155", "3", None),
        # English.
        ("Criminal Code article 137 part 2", "137", "2", None),
        # Full depth.
        ("JK 155-modda 3-qism 2-band", "155", "3", "2"),
    ],
)
def test_structured_reference_parsing(query, article, part, clause):
    refs = parse_references(query)
    assert refs, f"nothing parsed from {query!r}"
    assert refs[0] == LegalReference(article=article, part=part, clause=clause)


def test_part_number_is_not_read_as_a_second_article():
    """The regression this module exists for."""
    articles = extract_article_numbers("ЖК 155-модда 3-қисми")
    assert articles == ["155"], f"phantom article parsed: {articles}"


def test_clause_number_is_not_read_as_an_article():
    articles = extract_article_numbers("JK 155-modda 3-qism 2-band")
    assert articles == ["155"]


@pytest.mark.parametrize(
    "query,expected",
    [
        # Pre-existing behaviour that must not regress.
        ("What does Article 54 of the Civil Code say?", ["54"]),
        ("Jinoyat kodeksining 105-moddasi", ["105"]),
        ("Что говорит статья 208?", ["208"]),
        ("ст. 12 и статья 15", ["12", "15"]),
        ("no article here", []),
        ("Жиноят кодексининг 105-моддаси", ["105"]),
        ("модда 54", ["54"]),
    ],
)
def test_no_regression_against_previous_extractor(query, expected):
    assert extract_article_numbers(query) == expected


def test_references_keep_document_order():
    """The first-named provision is usually the subject of the question."""
    assert extract_article_numbers("ст. 12 и статья 15") == ["12", "15"]


def test_plain_question_yields_no_reference():
    assert parse_references("Mehnat shartnomasi qanday shaklda tuziladi?") == []


def test_citation_suffix_renders_part_and_clause():
    assert LegalReference("155").citation_suffix() == ""
    assert LegalReference("155", part="3").citation_suffix() == ", part 3"
    assert (
        LegalReference("155", part="3", clause="2").citation_suffix()
        == ", part 3, clause 2"
    )


def test_spaced_subnumber_is_normalised():
    """lex.uz renders 155¹ as a space-separated digit run."""
    assert extract_article_numbers("155 - 1-modda") == ["155-1"]


@pytest.mark.xfail(
    reason="Coordinated references ('106 va 107-moddalari') parse only the "
    "article adjacent to the marker. Rare enough to document rather than "
    "over-fit the grammar for.",
    strict=True,
)
def test_coordinated_articles_not_yet_supported():
    assert extract_article_numbers("Mehnat kodeksi 106 va 107-moddalari") == ["106", "107"]
