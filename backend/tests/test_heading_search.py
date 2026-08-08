"""Regressions for the two ways the heading branch lost the governing article.

Both were found by measurement, not reading: the benchmark reported the misses,
and the causes only became visible by running the ranking function in Postgres
against the real corpus.
"""
from __future__ import annotations

from app.db.models import Language
from app.services.rag.keyword import build_tsquery


def _terms(query: str, language: Language) -> set[str]:
    _, tsquery = build_tsquery(query, language)
    return {t.strip().removesuffix(":*") for t in tsquery.split("|")}


# ------------------------------------------------- Russian fleeting vowels

def test_russian_query_emits_a_form_short_enough_to_reach_fleeting_vowels():
    """`сделкой` stems to 'сделк'; the corpus has 'сделок'. Neither prefixes
    the other, so without a shorter variant the article titled "Понятие
    сделок" is unreachable from any question about сделки."""
    terms = _terms("Что признаётся сделкой по гражданскому праву?", Language.RU)
    assert any(
        "сделок".startswith(t) and "сделк".startswith(t) for t in terms
    ), f"no term prefixes both fleeting-vowel forms: {sorted(terms)}"


def test_russian_still_emits_the_original_token():
    """The truncated form is an addition, not a replacement."""
    assert "сделкой" in _terms("Что признаётся сделкой?", Language.RU)


def test_english_is_left_to_postgres():
    """English has no comparable alternation; extra variants would be noise."""
    terms = _terms("What is recognised as a transaction?", Language.EN)
    assert "transaction" in terms
    assert "transac" not in terms


# ------------------------------------------------------ Uzbek unchanged

def test_uzbek_still_gets_truncated_variants():
    terms = _terms("mehnat shartnomasi", Language.UZ_LATN)
    assert "shartnomasi" in terms
    assert any(t.startswith("shartnom") and len(t) < len("shartnomasi") for t in terms)


def test_uzbek_query_reaches_cyrillic_corpus():
    """Labour Code art. 160 exists only in Cyrillic; a Latin query must reach it."""
    terms = _terms("Xodim o'z tashabbusi bilan mehnat shartnomasini bekor qiladi", Language.UZ_LATN)
    assert any("ходим".startswith(t) for t in terms), sorted(terms)
    assert any("ташаббусига".startswith(t) for t in terms), sorted(terms)


# ------------------------------------------------- branch-of-law framing

def test_branch_of_law_qualifier_is_dropped():
    """"по гражданскому праву" names the branch, not the subject. Left in, it
    matched every "…гражданских прав" title and buried art. 101 "Понятие
    сделок"."""
    from app.services.rag.query_prep import content_tokens

    assert content_tokens("Что признаётся сделкой по гражданскому праву?", "ru") == ["сделкой"]


def test_civil_rights_are_a_subject_not_framing():
    """Different adjective endings: "гражданских прав" is what the question is
    about, and stripping it would break every rights question."""
    from app.services.rag.query_prep import content_tokens

    kept = content_tokens("Какие способы защиты гражданских прав?", "ru")
    assert "гражданских" in kept and "прав" in kept


def test_nominative_civil_rights_kept():
    from app.services.rag.query_prep import content_tokens

    assert "гражданские" in content_tokens("гражданские права человека", "ru")


def test_criminal_law_qualifier_also_dropped():
    from app.services.rag.query_prep import content_tokens

    assert content_tokens("Что признаётся кражей по уголовному праву?", "ru") == ["кражей"]


def test_uzbek_queries_are_untouched():
    from app.services.rag.query_prep import content_tokens

    assert content_tokens("Mehnat shartnomasi qanday bekor qilinadi", "uz-Latn") == [
        "mehnat", "shartnomasi", "bekor",
    ]
