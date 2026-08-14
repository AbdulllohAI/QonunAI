"""Adaptive answer length.

Two behaviours carry the feature, and both are easy to get wrong in ways that
look like the retrieval being broken rather than the length handling:

A bare directive is not a question. "batafsil" sent to retrieval matches
nothing, so the user asking for more detail would receive "no relevant
provisions found" — a worse answer than the one they were trying to expand.

The preference has to persist. Someone who asked for detail once should not
have to append "batafsil" to every subsequent question.
"""
from __future__ import annotations

import pytest

from app.services.llm.base import ChatMessage
from app.services.reasoning.engine import _resolve_turn
from app.services.reasoning.verbosity import (
    Verbosity,
    detect_directive,
    is_bare_directive,
    remembered_from_history,
    resolve,
    style_instruction,
)


# ------------------------------------------------------------- detection

@pytest.mark.parametrize(
    "message",
    ["batafsil", "to'liq", "kengroq", "tushuntir", "davom et",
     "батафсил", "тўлиқ", "подробнее", "объясни подробнее",
     "detailed", "explain more", "elaborate", "longer"],
)
def test_requests_for_depth(message):
    assert detect_directive(message) is Verbosity.DETAILED


@pytest.mark.parametrize(
    "message",
    ["qisqa", "qisqaroq", "juda qisqa", "bir gapda",
     "қисқа", "кратко", "короче", "вкратце",
     "short", "brief", "summarize", "tl;dr", "in one sentence"],
)
def test_requests_for_brevity(message):
    assert detect_directive(message) is Verbosity.BRIEF


@pytest.mark.parametrize(
    "message",
    ["Mehnat shartnomasi qanday bekor qilinadi?",
     "Что признаётся сделкой?",
     "What penalty applies for kidnapping?"],
)
def test_ordinary_questions_carry_no_directive(message):
    assert detect_directive(message) is None


def test_both_directives_reads_as_depth():
    """"qisqacha tushuntir" is far more often a request for explanation than
    for compression, and an answer that is too long is the recoverable error."""
    assert detect_directive("qisqacha tushuntir") is Verbosity.DETAILED


# ------------------------------------------------------- bare vs embedded

@pytest.mark.parametrize(
    "message",
    ["batafsil", "qisqaroq", "iltimos batafsil", "bir gapda ayting",
     "скажи короче", "подробнее пожалуйста", "make it shorter", "keep it brief"],
)
def test_bare_directives(message):
    assert is_bare_directive(message) is True


@pytest.mark.parametrize(
    "message",
    ["qisqacha aytganda mehnat shartnomasi nima?",
     "короче, что признаётся сделкой?",
     "briefly, what is the penalty for theft?"],
)
def test_a_directive_inside_a_question_is_not_bare(message):
    """These carry a real question; answering the previous one would be wrong."""
    assert is_bare_directive(message) is False


def test_a_plain_question_is_not_a_directive():
    assert is_bare_directive("Mehnat shartnomasi qanday bekor qilinadi?") is False


# ------------------------------------------------------------- persistence

def test_preference_is_remembered_across_turns():
    history = ["Mehnat shartnomasi nima?", "batafsil", "Ta'til necha kun?"]
    assert remembered_from_history(history) is Verbosity.DETAILED


def test_the_latest_preference_wins():
    history = ["batafsil", "endi qisqaroq"]
    assert remembered_from_history(history) is Verbosity.BRIEF


def test_no_preference_yet():
    assert remembered_from_history(["Mehnat shartnomasi nima?"]) is None


def test_an_explicit_request_overrides_the_remembered_one():
    assert resolve("qisqaroq", Verbosity.DETAILED) is Verbosity.BRIEF


def test_default_is_normal():
    assert resolve("Mehnat shartnomasi nima?", None) is Verbosity.NORMAL


# --------------------------------------------------------- turn resolution

def _history(*turns: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=t) for t in turns]


def test_bare_directive_reanswers_the_previous_question():
    """The whole point: "batafsil" must retrieve on the previous question."""
    question, verbosity = _resolve_turn("batafsil", _history("Mehnat shartnomasi nima?"))
    assert question == "Mehnat shartnomasi nima?"
    assert verbosity is Verbosity.DETAILED


def test_bare_directive_with_no_history_is_left_alone():
    """Nothing to carry forward; better to answer it than to invent a question."""
    question, _ = _resolve_turn("batafsil", [])
    assert question == "batafsil"


def test_a_real_question_is_never_replaced():
    question, _ = _resolve_turn("Ta'til necha kun?", _history("Mehnat shartnomasi nima?"))
    assert question == "Ta'til necha kun?"


def test_preference_applies_to_later_unrelated_questions():
    question, verbosity = _resolve_turn(
        "Ta'til necha kun?", _history("Mehnat shartnomasi nima?", "batafsil")
    )
    assert question == "Ta'til necha kun?"
    assert verbosity is Verbosity.DETAILED


def test_assistant_turns_do_not_set_the_preference():
    """Only what the user asked for counts; the model's own text may well
    contain the word "batafsil" while explaining something."""
    history = [
        ChatMessage(role="user", content="Mehnat shartnomasi nima?"),
        ChatMessage(role="assistant", content="Batafsil javob: shartnoma bu..."),
    ]
    _, verbosity = _resolve_turn("Ta'til necha kun?", history)
    assert verbosity is Verbosity.NORMAL


# ------------------------------------------------------------------ prompt

def test_every_level_has_an_instruction():
    for level in Verbosity:
        assert "Length this turn" in style_instruction(level)


def test_brevity_never_licenses_dropping_citations():
    """The one thing a short answer must not shorten."""
    assert "citation" in style_instruction(Verbosity.BRIEF).lower()
