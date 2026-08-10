"""Title precision as a ranking tiebreaker.

RRF fuses ranks and so knows a candidate placed well in several branches, but
not why. That was enough to surface the governing article (Recall@10 = 0.965)
and not enough to put it first (Recall@1 stuck at 0.719 across three separate
improvements). These tests pin the ordering decisions that gap turns on.
"""
from __future__ import annotations

from app.services.rag.title_affinity import title_affinity


def test_the_article_that_governs_beats_the_one_about_its_consequences():
    """Both titles match the query's words; only one is the question."""
    q = "Когда сделка требует нотариального удостоверения?"
    governs = title_affinity(q, "110-modda. Нотариальное удостоверение сделки", "ru")
    consequence = title_affinity(
        q, "112-modda. Последствия несоблюдения нотариальной формы сделки", "ru"
    )
    assert governs > consequence


def test_definition_beats_a_related_procedure():
    q = "Что признаётся сделкой по гражданскому праву?"
    definition = title_affinity(q, "101-modda. Понятие сделок", "ru")
    procedure = title_affinity(q, "111-modda. Государственная регистрация сделок", "ru")
    assert definition > procedure


def test_unrelated_article_scores_zero():
    """A candidate that reached the shortlist on body text must not be lifted."""
    q = "Что признаётся сделкой по гражданскому праву?"
    assert title_affinity(q, "211-modda. Pora berish", "ru") == 0.0


def test_russian_fleeting_vowel_still_matches():
    """"сделкой" and "сделок" are the same word; a fixed-length stem would cut
    them to "сделк" and "сдело" and find nothing."""
    assert title_affinity("Что признаётся сделкой?", "101-modda. Понятие сделок", "ru") > 0


# ----------------------------------------------------------- cross-script

def test_latin_question_matches_cyrillic_title():
    """Most of the corpus is Cyrillic and Latin is what people type. Without
    folding, the two share no characters and the signal is dead exactly where
    it is needed."""
    q = "Xodim o'z tashabbusi bilan mehnat shartnomasini qanday bekor qiladi?"
    assert title_affinity(
        q, "160-modda. Меҳнат шартномасини ходимнинг ташаббусига кўра бекор қилиш", "uz-Latn"
    ) > 0


def test_cross_script_ordering_is_preserved():
    q = "Xodim o'z tashabbusi bilan mehnat shartnomasini qanday bekor qiladi?"
    gold = title_affinity(
        q, "160-modda. Меҳнат шартномасини ходимнинг ташаббусига кўра бекор қилиш", "uz-Latn"
    )
    other = title_affinity(
        q, "149-modda. Меҳнат шартномасини ўзгартиришни расмийлаштириш", "uz-Latn"
    )
    assert gold > other


# ------------------------------------------------------------------ shape

def test_missing_heading_is_zero():
    assert title_affinity("что такое сделка", None, "ru") == 0.0
    assert title_affinity("что такое сделка", "", "ru") == 0.0


def test_score_stays_in_range():
    q = "Что признаётся сделкой по гражданскому праву?"
    for heading in ("101-modda. Понятие сделок", "211-modda. Pora berish", "1-modda."):
        assert 0.0 <= title_affinity(q, heading, "ru") <= 1.0


def test_article_label_is_not_content():
    """Every title starts "N-modda."; matching on that would score everything
    equally and the signal would carry no information."""
    q = "Nima bu modda?"
    assert title_affinity(q, "211-modda. Pora berish", "uz-Latn") == 0.0


def test_long_title_containing_the_query_scores_below_an_exact_one():
    q = "давлат божи"
    exact = title_affinity(q, "128-modda. Давлат божи", "uz-Cyrl")
    padded = title_affinity(
        q, "131-modda. Даъвонинг баҳоси ўзгартирилганда давлат божи қайта ҳисобланади", "uz-Cyrl"
    )
    assert exact > padded
