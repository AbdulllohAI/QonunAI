"""Telling "not in this corpus" apart from "not a legal question".

Both end with retrieval returning nothing, and the right answers are opposite.
Getting this backwards is costly in one direction and merely rude in the other,
which is why the classifier leans towards treating things as legal: answering a
legal question without sources is the failure this system exists to prevent,
while being stiff with someone asking about lunch is recoverable.
"""
from __future__ import annotations

import pytest

from app.services.reasoning.prompts import GENERAL_SYSTEM
from app.services.reasoning.scope import looks_legal


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
