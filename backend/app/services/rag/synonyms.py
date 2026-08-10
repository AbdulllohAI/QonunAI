"""Legal synonym expansion for the lexical retrieval branches.

People do not ask questions in statutory vocabulary. The Labour Code says
*xodim*; someone describing their own situation says *ishchi*. Both mean
"employee", but nothing lexical connects them, and the multilingual embedding
does not reliably bridge them either — measured: the same article (Labour Code
160) ranks 1st when asked with *xodim* and does not appear in the top 20 when
asked with *ishchi*.

**Deliberately conservative.** Every group here is a true equivalence in legal
usage, not a near-relation. Conflating terms that a lawyer distinguishes is a
correctness bug in a tool whose whole claim is that it cites the governing
provision: *shartnoma* (contract) and *bitim* (transaction) overlap in ordinary
speech and are distinct in the Civil Code, so they are not grouped here. When
in doubt, leave it out — a miss is recoverable by rephrasing, a confidently
wrong citation is not.

Applied only to the sparse and article-title branches, which match on tokens.
The dense branch embeds the question as asked; padding that text with synonyms
would move the query vector away from what the user actually wrote.
"""
from __future__ import annotations

from app.services.lang.translit import latin_to_cyrillic

__all__ = ["expand_tokens", "SYNONYM_GROUPS"]


#: Groups of interchangeable legal terms. Uzbek entries are written in Latin;
#: the Cyrillic forms are generated, so each term is listed once.
#:
#: Terms are stored as prefixes where the language is agglutinative, because
#: the tsquery emits `term:*` anyway — "ishdan bo'shash" appears as
#: "ishdan bo'shashi", "ishdan bo'shatish" and so on.
SYNONYM_GROUPS: tuple[frozenset[str], ...] = (
    # --- Uzbek: employment ------------------------------------------------
    # The Labour Code's own word is "xodim"; "ishchi" is what people say.
    frozenset({"xodim", "ishchi", "ishlovchi"}),
    frozenset({"ish beruvchi", "ishberuvchi"}),
    # Leaving a job: the statute frames it as terminating the contract.
    frozenset({"ishdan bo'shash", "ishdan bo'shatish", "ishdan ketish",
               "ishdan chiqish", "mehnat shartnomasini bekor qilish"}),
    frozenset({"ish haqi", "oylik", "maosh"}),
    frozenset({"mehnat ta'tili", "ta'til"}),
    # --- Uzbek: general ---------------------------------------------------
    frozenset({"jazo", "jazolash"}),
    frozenset({"jarima", "pul jarimasi"}),
    frozenset({"javobgarlik", "mas'uliyat"}),
    # --- Russian: employment ----------------------------------------------
    frozenset({"работник", "сотрудник", "трудящийся"}),
    frozenset({"увольнение", "расторжение трудового договора",
               "прекращение трудового договора"}),
    frozenset({"заработная плата", "зарплата", "оплата труда"}),
    frozenset({"отпуск", "трудовой отпуск"}),
    # --- Russian: general -------------------------------------------------
    frozenset({"наказание", "мера наказания"}),
    frozenset({"штраф", "денежное взыскание"}),
    frozenset({"ответственность", "юридическая ответственность"}),
    frozenset({"жилье", "жилище", "жилое помещение"}),
)


def _index() -> dict[str, frozenset[str]]:
    """Term -> the other members of its group, in both Uzbek scripts."""
    table: dict[str, frozenset[str]] = {}
    for group in SYNONYM_GROUPS:
        expanded: set[str] = set()
        for term in group:
            expanded.add(term)
            cyrillic = latin_to_cyrillic(term)
            if cyrillic != term:
                expanded.add(cyrillic)
        for term in expanded:
            table[term] = frozenset(expanded - {term})
    return table


_TABLE = _index()

#: Longest group entry in words, so callers know how many tokens to join when
#: looking for multi-word terms.
_MAX_PHRASE_WORDS = max(len(t.split()) for t in _TABLE)


def expand_tokens(tokens: list[str]) -> list[str]:
    """Extra query terms implied by the ones the user typed.

    Matches single tokens and multi-word phrases, and returns only the
    additions — the caller keeps the original tokens, so expansion can add
    recall but never removes what was actually asked.
    """
    if not tokens:
        return []

    extra: list[str] = []
    seen = set(tokens)

    for size in range(1, _MAX_PHRASE_WORDS + 1):
        for start in range(len(tokens) - size + 1):
            phrase = " ".join(tokens[start : start + size])
            for synonym in _TABLE.get(phrase, ()):
                if synonym not in seen:
                    seen.add(synonym)
                    extra.append(synonym)
    return extra
