"""Telling "not in this corpus" apart from "not a legal question".

Both end with retrieval returning nothing, and the right answers are opposite.
Getting this backwards is costly in one direction and merely rude in the other,
which is why the classifier leans towards treating things as legal: answering a
legal question without sources is the failure this system exists to prevent,
while being stiff with someone asking about lunch is recoverable.
"""
from __future__ import annotations

import pytest

from app.services.reasoning.engine import _is_everyday
from app.services.reasoning.prompts import GENERAL_SYSTEM
from app.db.models import Language
from app.services.reasoning.scope import looks_legal, looks_non_legal


@pytest.mark.parametrize(
    "question",
    [
        # Real legal questions this corpus happens not to cover — these must
        # still refuse rather than be answered from the model's memory.
        "Ер кодексида ер участкаси қандай ажратилади?",
        "Какие требования Жилищного кодекса предъявляются к перепланировке?",
        "What does the Customs Code say about declaring goods?",
        "Jinoyat kodeksining 9999-moddasida nima deyilgan?",
        # Ordinary legal questions.
        "Mehnat shartnomasi qanday bekor qilinadi?",
        "Что признаётся сделкой?",
    ],
)
def test_legal_questions_are_recognised(question):
    assert looks_legal(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "Osh qanday pishiriladi?",
        "What is the weather in Tashkent today?",
        "Write me a short poem about the mountains.",
        "Кто выиграл чемпионат мира по футболу в 2022 году?",
        "Toshkentda eng yaxshi restoran qaysi?",
        "Python o'rganishni qayerdan boshlay?",
    ],
)
def test_ordinary_questions_are_not_legal(question):
    assert looks_legal(question) is False


def test_empty_input_is_not_legal():
    assert looks_legal("") is False
    assert looks_legal("   ") is False


def test_an_article_reference_alone_is_enough():
    """Even without a legal noun, naming an article is unmistakably legal."""
    assert looks_legal("137-modda nima deydi?") is True


# ------------------------------------------------------- the routing gate

# `looks_legal` is a signal, not the decision. What actually routes a question
# away from the corpus is `_is_everyday`, and these pin the asymmetry that
# makes it safe.


@pytest.mark.parametrize(
    "question,lang",
    [
        # Every one of these was misrouted in production: ordinary phrasings
        # containing no act name, article number or glossary term, answered
        # from the model's memory with confident specifics and no citation.
        ("Ishdan bo'shash tartibi qanday?", Language.UZ_LATN),
        ("Ишдан бўшаш тартиби қандай?", Language.UZ_CYRL),
        ("Как разделить имущество при разводе?", Language.RU),
        ("How many days of annual leave am I entitled to?", Language.EN),
        # And the ones that already worked, which must not regress.
        ("Mehnat shartnomasi qanday shaklda tuziladi?", Language.UZ_LATN),
        ("Меня уволили без предупреждения, что делать?", Language.RU),
    ],
)
def test_legal_questions_never_take_the_conversational_path(question, lang):
    """The expensive direction. A miss here is ungrounded legal advice."""
    assert _is_everyday(question, lang) is False


@pytest.mark.parametrize(
    "question,lang",
    [
        ("Osh qanday pishiriladi?", Language.UZ_LATN),
        ("Как приготовить плов?", Language.RU),
        ("What is the weather in Tashkent today?", Language.EN),
        ("Кто выиграл чемпионат мира по футболу в 2022 году?", Language.RU),
        ("Python o'rganishni qayerdan boshlay?", Language.UZ_LATN),
    ],
)
def test_everyday_questions_are_answered_conversationally(question, lang):
    assert _is_everyday(question, lang) is True


def test_the_two_predicates_are_not_each_others_negation():
    """The gap between them is deliberate, and it falls to the legal side.

    "Toshkentda eng yaxshi restoran qaysi?" is neither legal nor a topic the
    everyday list names, so it gets the legal path and an honest "not covered"
    — stiff, and safe. Collapsing the two functions into one would turn that
    gap into ungrounded answers.
    """
    question = "Toshkentda eng yaxshi restoran qaysi?"
    assert looks_legal(question) is False
    assert looks_non_legal(question) is False
    assert _is_everyday(question, Language.UZ_LATN) is False


def test_a_legal_question_mentioning_an_everyday_word_stays_legal():
    """Positive everyday evidence is necessary but not sufficient — a real
    legal signal in the same sentence wins."""
    assert looks_non_legal("Restoran ochish uchun qanday litsenziya kerak?") is False
    assert _is_everyday(
        "Oshxona ochish uchun qanday ruxsatnoma va shartnoma kerak?", Language.UZ_LATN
    ) is False


def test_medical_and_document_words_are_not_treated_as_everyday():
    """Both appear constantly in genuine legal questions, so neither may be a
    non-legal marker."""
    for question in (
        "Shifokor xatosi uchun javobgarlik qanday?",
        "Какие документы нужны для регистрации?",
    ):
        assert looks_non_legal(question) is False


# ------------------------------------------------------------------ prompt

def test_the_general_prompt_forbids_deflecting():
    """Telling someone what you will not do is worse than useless when they
    asked something ordinary."""
    assert "not a legal question" in GENERAL_SYSTEM
    assert "Never say" in GENERAL_SYSTEM


def test_the_general_prompt_forbids_citations():
    """No sources are supplied, so any citation would be fabricated."""
    assert "no citations" in GENERAL_SYSTEM.lower()


def test_the_general_prompt_refuses_to_invent_live_data():
    assert "live" in GENERAL_SYSTEM and "weather" in GENERAL_SYSTEM


def test_the_general_prompt_keeps_the_users_language():
    assert "same language and script" in GENERAL_SYSTEM


def test_a_legal_question_in_disguise_is_still_refused():
    """The prompt's own backstop for anything the classifier lets through."""
    assert "legal one in disguise" in GENERAL_SYSTEM
