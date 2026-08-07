"""Intent routing tests.

The failure this guards against is specific and was live in production: a
conversational message that fell through to retrieval came back as "no legal
sources found", which reads as a broken product. The opposite failure — a real
legal question captured by a conversational rule — is worse still, so the
misrouting tests below matter more than the happy path.
"""
from __future__ import annotations

import pytest

from app.db.models import Language
from app.services.reasoning.intent import (
    Intent,
    classify_intent,
    conversational_reply,
)


@pytest.mark.parametrize(
    "message",
    [
        "Salom",
        "salom",
        "Assalomu alaykum",
        "Assalomu alaykum!",
        "Ассалому алайкум",
        "Xayrli kun",
        "Привет",
        "Здравствуйте",
        "Hello",
        "Good morning",
    ],
)
def test_greetings_route_to_greeting(message):
    assert classify_intent(message) is Intent.GREETING


@pytest.mark.parametrize(
    "message",
    [
        # Every one of these previously fell through to retrieval.
        "Qalaysiz?",
        "Nima gap?",
        "Yaxshimisiz?",
        "Ishlar qalay",
        "Salom, qalaysiz?",
        "Қалайсиз?",
        "Как дела?",
        "How are you?",
        "Rahmat",
        "Спасибо",
        "Thanks",
    ],
)
def test_smalltalk_routes_to_smalltalk(message):
    assert classify_intent(message) is Intent.SMALLTALK


@pytest.mark.parametrize(
    "message",
    [
        "Sen kimsan?",
        "Siz kimsiz?",
        "Nima qila olasan?",
        "Кто ты?",
        "Что ты умеешь?",
        "Who are you?",
        "What can you do?",
        "What is QonunAI?",
    ],
)
def test_capability_questions_route_to_capability(message):
    assert classify_intent(message) is Intent.CAPABILITY


@pytest.mark.parametrize(
    "message",
    [
        "Mehnat shartnomasi qanday shaklda tuziladi?",
        "Jinoyat kodeksi 155-modda",
        "Какое наказание за похищение человека?",
        "What form must a labour contract take?",
        # A greeting prefix must not swallow a real question.
        "Salom, mehnat shartnomasi haqida savolim bor",
        "Assalomu alaykum, meros masalasida yordam kerak",
        "Здравствуйте, у меня вопрос по трудовому договору",
        # Superficially similar to small talk but substantive.
        "Ishdan boshqasiga qanday oʻtish mumkin?",
    ],
)
def test_legal_questions_are_never_short_circuited(message):
    assert classify_intent(message) is Intent.LEGAL


def test_long_message_is_legal_even_if_it_opens_conversationally():
    """A greeting prefix on a real question must reach retrieval."""
    msg = "Salom qalaysiz men mehnat shartnomasi haqida soʻramoqchi edim"
    assert classify_intent(msg) is Intent.LEGAL


def test_empty_and_whitespace_are_legal():
    """Never answer an empty message with canned small talk."""
    assert classify_intent("") is Intent.LEGAL
    assert classify_intent("   ") is Intent.LEGAL


@pytest.mark.parametrize("apostrophe", ["ʻ", "ʼ", "'", "’", "‘"])
def test_apostrophe_variants_do_not_break_matching(apostrophe):
    assert classify_intent(f"nima gap{apostrophe}") is Intent.SMALLTALK


@pytest.mark.parametrize("lang", list(Language))
def test_every_language_has_a_reply_for_every_conversational_intent(lang):
    for intent in (Intent.GREETING, Intent.SMALLTALK, Intent.CAPABILITY):
        reply = conversational_reply(intent, lang)
        assert reply and reply.strip(), f"{lang} / {intent} has no reply"


@pytest.mark.parametrize("lang", list(Language))
def test_legal_intent_has_no_canned_reply(lang):
    assert conversational_reply(Intent.LEGAL, lang) is None


@pytest.mark.parametrize("lang", list(Language))
def test_smalltalk_and_capability_replies_include_an_example(lang):
    """Show the user what a good question looks like, don't just deflect."""
    for intent in (Intent.SMALLTALK, Intent.CAPABILITY):
        assert "«" in conversational_reply(intent, lang)
