"""How much an article's title *is* the question, rather than merely related.

RRF fuses ranks, so it knows a candidate placed well in several branches but
nothing about why. That is enough to surface the governing article — Recall@10
is 0.965 — and not enough to put it first, which is where this system had been
stuck at Recall@1 = 0.719 across three separate improvements.

The signal that separates the two is title precision. Asked when a transaction
needs notarisation, the corpus offers:

    art. 110  "Нотариальное удостоверение сделки"                  ← governs it
    art. 112  "Последствия несоблюдения нотариальной формы сделки" ← a consequence

Both match the query's words. The difference is that art. 110's title contains
*nothing else*, while art. 112 introduces "последствия" and "несоблюдения",
concepts the question never raised. Measuring overlap in both directions
captures that: how much of the question the title covers, and how much of the
title is question. A title that is exactly the topic scores high on both.

Deliberately a *tiebreaker*, not a ranker. It is added to the fused score with a
small weight, so it reorders candidates RRF already considered comparable and
cannot pull an unrelated article to the top on a lucky word.
"""
from __future__ import annotations

from app.services.lang.translit import latin_to_cyrillic
from app.services.rag.query_prep import content_tokens

__all__ = ["title_affinity", "act_affinity"]

#: Shared leading characters required to treat two words as the same term.
#: Matches the threshold the tsquery builder already uses, and is what lets
#: "shartnoma" reach "shartnomasini".
#:
#: Truncating to a fixed length instead would miss Russian fleeting vowels:
#: "сделкой" cut to five characters is "сделк" and "сделок" is "сдело", which
#: are not equal even though they are the same word. Comparing prefixes finds
#: the shared "сдел".
_MIN_COMMON_PREFIX = 4


def _terms(text: str, language_value: str) -> list[str]:
    """Content words of a piece of text, with framing words removed.

    Folded to Cyrillic so the comparison survives script. Most of this corpus
    is Cyrillic while Latin is the script people type in, so without this the
    signal is zero for exactly the cross-script questions that need it most —
    a Latin question against a Cyrillic title shares no characters at all.
    Russian is already Cyrillic, and the mapping leaves it untouched.
    """
    return [
        latin_to_cyrillic(token)
        for token in content_tokens(text or "", language_value)
        if token and not token.isdigit()
    ]


def _same_term(a: str, b: str) -> bool:
    if a == b:
        return True
    limit = min(len(a), len(b))
    if limit < _MIN_COMMON_PREFIX:
        return False
    shared = 0
    while shared < limit and a[shared] == b[shared]:
        shared += 1
    return shared >= _MIN_COMMON_PREFIX


def title_affinity(query: str, heading: str | None, language_value: str) -> float:
    """0..1 — how closely an article title matches the question, both ways.

    The harmonic mean of the two coverages, so a title scores well only when it
    covers most of the question *and* introduces little the question did not
    ask about. A long title containing the query as a fragment scores poorly,
    which is the intent.
    """
    if not heading:
        return 0.0

    label = {"modda", "статья", "модда"}
    query_terms = [t for t in _terms(query, language_value) if t not in label]
    # Article titles arrive as "160-modda. …"; the numeric label is not content.
    title_terms = [t for t in _terms(heading, language_value) if t not in label]
    if not query_terms or not title_terms:
        return 0.0

    matched_query = sum(1 for q in query_terms if any(_same_term(q, t) for t in title_terms))
    matched_title = sum(1 for t in title_terms if any(_same_term(t, q) for q in query_terms))
    if not matched_query or not matched_title:
        return 0.0

    covers_query = matched_query / len(query_terms)
    covers_title = matched_title / len(title_terms)
    return 2 * covers_query * covers_title / (covers_query + covers_title)


#: Words that appear in nearly every act name and so carry no signal about
#: *which* act is meant. Without excluding them, "ответственность" in a question
#: matches the Code of Administrative Responsibility's own title and pulls every
#: liability question toward that code regardless of what was asked.
_ACT_NAME_NOISE = frozenset({
    "кодекс", "кодекса", "кодексе", "кодекси", "кодексининг",
    "республики", "республикаси", "республикасининг", "узбекистан",
    "ўзбекистон", "uzbekiston", "respublikasining", "kodeksi",
    "ответственности", "ответственность", "javobgarlik",
})


def act_affinity(query: str, law_name: str | None, language_value: str) -> float:
    """0..1 — how strongly the question names the act a candidate comes from.

    The Criminal Code and the Code of Administrative Responsibility both contain
    an article titled "Нарушение правил пожарной безопасности". Nothing in
    either article's text or title says which liability it imposes; the only
    thing that distinguishes them is the name of the code they sit in, and
    retrieval never looked there. Asked about *уголовная* liability for a fire
    safety breach, the system returned the administrative article first.

    Scored one-directionally, as coverage of the act name's distinctive words.
    The reverse would be meaningless: act names are mostly dates and boilerplate
    that no question would ever repeat.
    """
    if not law_name:
        return 0.0
    name_terms = [t for t in _terms(law_name, language_value) if t not in _ACT_NAME_NOISE]
    query_terms = _terms(query, language_value)
    if not name_terms or not query_terms:
        return 0.0

    matched = sum(1 for t in name_terms if any(_same_term(t, q) for q in query_terms))
    return matched / len(name_terms)
