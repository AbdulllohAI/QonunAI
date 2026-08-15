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

## Why `looks_legal` alone must not decide

`looks_legal` was written when it ran *after* retrieval, as a tie-breaker on an
empty result — "a question that found sources is legal by demonstration" did
the heavy lifting, and this function only had to sort the leftovers.

Moving it in front of retrieval quietly turned it into the sole arbiter, and
under that load its default is backwards: absence of legal vocabulary became
proof of a non-legal question. Measured on ordinary phrasings, four of eleven
real legal questions fell through — *"Ishdan boʻshash tartibi qanday?"*,
*"Как разделить имущество при разводе?"* — because no word in them is an act
name, an article number, or a glossary term. Each one was then answered from
the model's own memory, with confident specifics and no citation: exactly the
failure this system exists to prevent, arrived at through the component meant
to prevent it.

Enumerating more legal words does not fix that. Law has no closed vocabulary —
any word can appear in a legal question — so every miss is silent and
dangerous. The non-legal side *is* enumerable in the only sense that matters
here: a miss there is harmless. An unrecognised cooking question gets a stiff
"the retrieved provisions don't cover this" instead of a warm answer, which is
a worse conversation and not a wrong one.

So the general path now requires **positive evidence** of an everyday topic
(`looks_non_legal`), not merely the absence of legal evidence. The two
functions are deliberately not each other's negation, and the gap between them
falls to the legal side.

Note that greetings, small talk and "what can you do" never reach here at all —
`classify_intent` answers those deterministically first.
"""
from __future__ import annotations

import re

from app.services.rag.keyword import extract_article_numbers, infer_act_types
from app.services.rag.query_prep import content_tokens
from app.services.rag.synonyms import SYNONYM_GROUPS

__all__ = ["looks_legal", "looks_non_legal"]


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


#: Everyday subjects that carry no legal reading. Unlike the legal markers
#: above, this list is allowed to be incomplete: what it misses is answered as
#: a legal question and gets an honest "not covered by the retrieved sources",
#: which is stiff rather than wrong. Nothing here may be a word that also does
#: legal work — "hujjat"/"документ" and anything medical are left out for that
#: reason, since both turn up constantly in genuine legal questions.
_NON_LEGAL_TOPICS = re.compile(
    r"("
    # cooking and food
    r"osh\b|palov|pishir\w*|retsept\w*|taom\w*|ovqat\w*|"
    r"пишир\w*|таом\w*|овқат\w*|"
    r"готов(?:ить|лю)\w*|рецепт\w*|блюд\w*|кухн\w*|"
    r"\bcook\w*|\brecipe\w*|\bdish(?:es)?\b|\bcuisine\b|"
    # weather
    r"ob-havo|обҳаво|об-ҳаво|погод\w*|температур\w*|"
    r"\bweather\b|\bforecast\b|\btemperature\b|"
    # sport
    r"futbol\w*|sport\w*|musobaqa\w*|футбол\w*|спорт\w*|матч\w*|"
    r"\bfootball\b|\bsports?\b|\bmatch(?:es)?\b|"
    # entertainment
    r"hazil\w*|latifa\w*|qo\w?shiq\w*|\bkino\b|\bfilm\w*|"
    r"ҳазил\w*|латифа\w*|қўшиқ\w*|"
    r"шутк\w*|анекдот\w*|песн\w*|музык\w*|\bфильм\w*|"
    r"\bjokes?\b|\bsongs?\b|\bmusic\b|\bmovies?\b|"
    # arithmetic, code and translation — asked of assistants constantly
    r"tarjima\w*|таржима\w*|перевед\w*|перевод\w*|\btranslat\w*|"
    r"\bpython\b|javascript|\bsql\b|dastur\w*|программир\w*|"
    r"\bprogramm\w*|\bcodes?\b\s+(?:for|in)\b"
    r")",
    re.IGNORECASE | re.UNICODE,
)


def looks_non_legal(question: str) -> bool:
    """Whether the question is positively about an everyday, non-legal topic.

    Not the negation of :func:`looks_legal`. Both can be false — that is the
    intended gap, and it falls to the legal side. See the module docstring.
    """
    if not question or not question.strip():
        return False
    return bool(_NON_LEGAL_TOPICS.search(question))


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
