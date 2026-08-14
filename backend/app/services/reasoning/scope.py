"""Telling "not in this corpus" apart from "not a legal question".

Both end with retrieval returning nothing, and they need opposite answers.

A question about the Land Code is a *legal* question this corpus cannot answer,
and the only honest reply is to say so. Inventing an answer there is the
failure this whole system is built to prevent.

"How do I cook osh?" is not a legal question at all. Replying "no relevant
legal provisions were found" to it is technically true and reads as a broken
product — the user asked something ordinary and got a bureaucratic non-answer.

The distinction is whether the question carries any legal signal: an act name,
an article reference, or a term of art. That is deliberately generous towards
refusing — anything that looks remotely legal is treated as legal, because
answering a legal question without sources is far worse than being stiff with
someone asking about lunch.
"""
from __future__ import annotations

import re

from app.services.rag.keyword import extract_article_numbers, infer_act_types
from app.services.rag.query_prep import content_tokens
from app.services.rag.synonyms import SYNONYM_GROUPS

__all__ = ["looks_legal"]


def _legal_vocabulary() -> frozenset[str]:
    """Every term of art the glossary knows, in both scripts.

    Reusing the retrieval glossary rather than keeping a second list means the
    words that make a question *findable* are exactly the words that make it
    count as legal — one place to add a term, not two.
    """
    from app.services.rag.synonyms import _TABLE  # normalised, both scripts

    return frozenset(_TABLE)


_VOCAB = _legal_vocabulary()

#: Words that name the legal domain without being terms of art the glossary
#: carries — enough on their own to treat a question as legal.
_LEGAL_MARKERS = re.compile(
    r"\b("
    r"qonun\w*|huquq\w*|kodeks\w*|modda\w*|jazo\w*|jarima\w*|sud\w*|advokat\w*|"
    r"shartnoma\w*|majburiyat\w*|javobgarlik\w*|nizom\w*|farmon\w*|qaror\w*|"
    r"қонун\w*|ҳуқуқ\w*|кодекс\w*|модда\w*|жазо\w*|жарима\w*|суд\w*|адвокат\w*|"
    r"шартнома\w*|мажburiyat\w*|жавобгарлик\w*|фармон\w*|қарор\w*|"
    r"закон\w*|прав\w*|кодекс\w*|стать\w*|наказан\w*|штраф\w*|суд\w*|адвокат\w*|"
    r"договор\w*|обязанност\w*|ответственност\w*|указ\w*|постановлен\w*|иск\w*|"
    r"legal|law|laws|article|code|court|penalt\w*|fine|contract|liab\w*|"
    r"attorney|lawyer|statut\w*|regulation\w*"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def looks_legal(question: str, language_value: str = "uz-Latn") -> bool:
    """Whether the question is asking about law at all.

    Only consulted when retrieval came back empty — a question that found
    sources is legal by demonstration.
    """
    if not question or not question.strip():
        return False

    if extract_article_numbers(question) or infer_act_types(question):
        return True
    if _LEGAL_MARKERS.search(question):
        return True

    tokens = content_tokens(question, language_value)
    return any(_matches_vocabulary(token) for token in tokens)


#: Below this, a shared prefix is coincidence rather than the same word.
_MIN_STEM = 4


def _matches_vocabulary(token: str) -> bool:
    """Whether a query token is a glossary term, allowing for inflection.

    Exact membership is not enough: the glossary holds "сделка" while the
    question says "сделкой", and Uzbek and Russian both inflect heavily. This
    mirrors how the retrieval branches match — on a shared prefix — so a
    question that *is* findable is also recognised as legal.
    """
    if len(token) < _MIN_STEM:
        return False
    if token in _VOCAB:
        return True
    return any(
        len(term) >= _MIN_STEM
        and (token.startswith(term[:_MIN_STEM]) and (term.startswith(token[:_MIN_STEM])))
        for term in _VOCAB
        if " " not in term
    )
