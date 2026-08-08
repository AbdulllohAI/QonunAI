"""Query preparation for retrieval.

Two problems, both measured on the ``uzlegal-v1`` benchmark rather than assumed.

**Suffixes defeat prefix matching.** Uzbek has no Postgres stemmer, so the sparse
path uses ``token:*`` prefix queries. But the prefix is applied to the *query*
token, and Uzbek is agglutinative: the user types ``шаклда`` ("in the form"),
the article title says ``шакли`` ("its form"), and ``шаклда:*`` matches neither.
Verified directly::

    to_tsvector('simple','Меҳнат шартномасининг шакли') @@ to_tsquery('simple','шаклда:*')  → false
    to_tsvector('simple','Меҳнат шартномасининг шакли') @@ to_tsquery('simple','шакл:*')    → true

So a token is also emitted truncated, which restores the match.

**Interrogative scaffolding dilutes the query.** "Какое наказание предусмотрено
за похищение человека?" retrieves general *sentencing* articles rather than the
offence, because the framing words pull the embedding toward the sentencing
chapter. Stripping the scaffolding leaves the terms that actually discriminate.
The framing lists are deliberately conservative — they hold interrogatives and
copulas only, never substantive legal nouns, because dropping a real legal term
would trade one failure mode for a worse one.
"""
from __future__ import annotations

import re

__all__ = ["stem_variants", "strip_framing", "content_tokens"]

#: Interrogative and structural filler. Substantive legal nouns are deliberately
#: absent: "наказание" dominates a query, but it is still legal content, and
#: removing it would break questions where it is the actual subject.
_FRAMING: dict[str, frozenset[str]] = {
    "ru": frozenset({
        "какой", "какая", "какое", "какие", "каков", "какова", "каково", "каковы",
        "что", "как", "где", "когда", "почему", "зачем", "кто", "чем", "чему",
        "это", "такое", "является", "являются", "предусмотрено", "предусмотрена",
        "предусмотрены", "устанавливается", "установлен", "установлена",
        "происходит", "осуществляется", "признается", "признаётся", "грозит",
        "нужно", "надо", "можно", "ли", "же", "бы", "если", "или", "для", "при",
        "об", "обо", "про", "по", "из", "от", "до", "над", "под", "без", "за",
    }),
    "uz": frozenset({
        "qanday", "qanaqa", "qaysi", "qachon", "qayerda", "nima", "nimalar",
        "nega", "kim", "kimlar", "necha", "nechta", "qancha", "bo'ladi", "boladi",
        "boʻladi", "qilinadi", "tuziladi", "beriladi", "hisoblanadi", "deyiladi",
        "kerak", "mumkin", "uchun", "haqida", "bilan", "yoki", "va", "ham",
        "қандай", "қанақа", "қайси", "қачон", "қаерда", "нима", "нималар",
        "нега", "ким", "кимлар", "неча", "нечта", "қанча", "бўлади", "қилинади",
        "тузилади", "берилади", "ҳисобланади", "дейилади", "керак", "мумкин",
        "учун", "ҳақида", "билан", "ёки", "ва", "ҳам",
    }),
    "en": frozenset({
        "what", "which", "how", "when", "where", "why", "who", "whom", "whose",
        "is", "are", "was", "were", "be", "been", "does", "do", "did", "can",
        "could", "should", "would", "must", "the", "a", "an", "of", "for", "to",
        "in", "on", "at", "by", "with", "about", "under", "and", "or",
    }),
}

#: Every mark used for the Uzbek Latin ʻ in practice. U+02BB is the standard
#: form (oʻzbek), but users and sources freely substitute a straight quote or
#: either curly quote. To Postgres these are four different tokens, which
#: silently costs recall on Latin Uzbek, so they are all folded away.
_APOSTROPHES = ("ʻ", "ʼ", "‘", "’", "'", "´", "`")

_WORD = re.compile(r"[\wʻʼ‘’'`´]{2,}", re.UNICODE)

#: Below this length a token is already close to its stem; truncating would
#: start matching unrelated words.
_MIN_STEM_LEN = 4
_MIN_TRUNCATE_LEN = 6
#: How much to shave. Uzbek case/possessive endings are typically 2-4 chars.
_SUFFIX_SHAVE = 3


def _family(language_value: str) -> str:
    if language_value.startswith("uz"):
        return "uz"
    return language_value if language_value in _FRAMING else "en"


def normalise_token(raw: str) -> str:
    """Lowercase and fold the apostrophe variants Uzbek Latin uses.

    ``oʻ``, ``o'``, ``o‘`` and ``o’`` are the same letter to a reader and
    different tokens to Postgres, which silently halves recall on Latin Uzbek.
    """
    lowered = raw.lower()
    for mark in _APOSTROPHES:
        lowered = lowered.replace(mark, "")
    return lowered


# "по гражданскому праву", "нормы уголовного права" — these name the *branch
# of law* the question sits in, not its subject. Left in, they match every
# article title containing "гражданских прав" and bury the one that answers the
# question: for "Что признаётся сделкой по гражданскому праву?" the top hits
# were all "…гражданских прав" while art. 101 "Понятие сделок" never surfaced.
#
# Matched only in the oblique cases, where the phrase can only be the framing
# reading. "гражданские права" and "гражданских прав" (civil *rights*) are a
# genuine subject and use different adjective endings, so they are left alone.
_BRANCH_OF_LAW = re.compile(
    r"\b(?:гражданск|уголовн|трудов|налогов|семейн|администрат\w*)\w*(?:ому|ого|ом)"
    r"\s+прав(?:у|а|е)\b",
    re.IGNORECASE,
)


def strip_branch_of_law(query: str) -> str:
    """Drop branch-of-law qualifiers, keeping the question's actual subject."""
    return _BRANCH_OF_LAW.sub(" ", query)


def content_tokens(query: str, language_value: str) -> list[str]:
    """Query tokens with interrogative scaffolding removed.

    Falls back to the unfiltered tokens when filtering would empty the query —
    "Nima qilish kerak?" is all framing, and an empty query retrieves nothing.
    """
    fam = _family(language_value)
    framing = _FRAMING.get(fam, frozenset())
    raw = [normalise_token(t) for t in _WORD.findall(strip_branch_of_law(query))]
    raw = [t for t in raw if t and not t.isdigit()]
    kept = [t for t in raw if t not in framing]
    return kept or raw


def strip_framing(query: str, language_value: str) -> str:
    """Query text with framing words removed, for embedding.

    Returns the original string if stripping removed everything meaningful, so
    a purely conversational question still gets embedded as written.
    """
    kept = content_tokens(query, language_value)
    if not kept:
        return query
    stripped = " ".join(kept)
    # A near-total strip usually means the question was conversational rather
    # than terminological; embedding the fragment would lose more than it gains.
    return stripped if len(stripped) >= 0.4 * len(query.strip()) else query


def stem_variants(token: str) -> list[str]:
    """Prefix forms for a token: the token itself, plus a truncated stem.

    Only applied where Postgres has no stemmer for the language. The truncated
    form is what lets a query for ``шаклда`` reach a document containing
    ``шакли``.
    """
    token = normalise_token(token)
    if not token:
        return []
    out = [token]
    if len(token) >= _MIN_TRUNCATE_LEN:
        stem = token[: max(_MIN_STEM_LEN, len(token) - _SUFFIX_SHAVE)]
        if stem != token:
            out.append(stem)
    return out
