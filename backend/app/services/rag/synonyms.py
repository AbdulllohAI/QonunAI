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
from app.services.rag.query_prep import normalise_token

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
    frozenset({"xodim", "ishchi", "ishlovchi", "работник", "сотрудник",
               "трудящийся"}),
    frozenset({"ish beruvchi", "ishberuvchi", "работодатель"}),
    # Leaving a job: the statute frames it as terminating the contract.
    frozenset({"ishdan bo'shash", "ishdan bo'shatish", "ishdan ketish",
               "ishdan chiqish", "mehnat shartnomasini bekor qilish"}),
    # Voluntary resignation. The Labour Code frames it as termination "at the
    # employee's initiative"; people say "I want to leave myself". This is the
    # distinction between art. 160 and art. 166 (employer-initiated), so the
    # "own initiative" sense is signal, not scaffolding — which is why the
    # reflexive pronoun is expanded rather than stripped as framing.
    frozenset({"o'z tashabbusi", "o'z xohishi", "o'z arizasi", "o'zi",
               "xodimning tashabbusi", "ixtiyoriy"}),
    frozenset({"ish haqi", "oylik", "maosh", "заработная плата", "зарплата",
               "оплата труда"}),
    frozenset({"mehnat ta'tili", "ta'til", "отпуск", "трудовой отпуск"}),
    # --- Uzbek: general ---------------------------------------------------
    frozenset({"jazo", "jazolash", "наказание", "мера наказания"}),
    frozenset({"jarima", "pul jarimasi", "штраф", "денежное взыскание"}),
    # --- terms of art and their lay phrasing ------------------------------
    # People describe the situation; the statute names the doctrine. These are
    # the same relation as xodim/ishchi, not benchmark-specific patches: the
    # Criminal Code defines "невменяемость" as being unable to understand the
    # significance of one's actions, which is exactly how a non-lawyer puts it.
    frozenset({"невменяемость", "психическое расстройство",
               "понимал своих действий", "понимать значение своих действий"}),
    # Admissibility: the code says "мақбуллик", people say "қабул қилинади".
    frozenset({"maqbul", "maqbullik", "qabul"}),
    frozenset({"javobgarlik", "mas'uliyat", "ответственность",
               "юридическая ответственность"}),
    # --- Russian: employment ----------------------------------------------
    frozenset({"увольнение", "расторжение трудового договора",
               "прекращение трудового договора"}),
    # Listed without the preposition too: "по" is stripped as framing before
    # expansion runs, so a group keyed only on the full phrase never matches.
    frozenset({"собственному желанию", "собственное желание",
               "инициативе работника", "своей инициативе"}),
    # --- Russian: general -------------------------------------------------
    frozenset({"жилье", "жилище", "жилое помещение"}),
    # --- Uzbek <-> Russian legal glossary ---------------------------------
    # Nothing lexical connects the two languages, so a question asked in Uzbek
    # cannot reach a Russian-only act (43% of this corpus) through the keyword
    # branches at all. That leaves dense retrieval alone, and bge-m3's Uzbek is
    # the weakest part of its multilingual coverage — measured: "Битим деб нима
    # тушунилади?" never reached Civil Code art. 101 "Понятие сделок".
    #
    # Legal terminology is a closed vocabulary, which makes a glossary a
    # reasonable bridge where a general bilingual dictionary would not be. Each
    # pair below is a term of art with a single settled counterpart; where a
    # term is genuinely ambiguous across the two systems it is left out.
    frozenset({"bitim", "сделка"}),
    frozenset({"shartnoma", "договор"}),
    frozenset({"mulk", "собственность"}),
    frozenset({"meros", "наследство", "наследование"}),
    frozenset({"jinoyat", "преступление"}),
    frozenset({"o'g'irlik", "кража", "хищение"}),
    frozenset({"qotillik", "odam o'ldirish", "убийство"}),
    frozenset({"ayb", "вина"}),
    frozenset({"so'roq", "допрос"}),
    frozenset({"dalil", "доказательство"}),
    frozenset({"tergovchi", "следователь"}),
    frozenset({"guvoh", "свидетель"}),
    frozenset({"da'vo", "иск"}),
    frozenset({"sud", "суд"}),
    frozenset({"sudya", "судья"}),
    frozenset({"soliq", "налог"}),
    frozenset({"nikoh", "брак"}),
    frozenset({"farzandlikka olish", "усыновление"}),
    frozenset({"vasiylik", "опека"}),
    frozenset({"aliment", "алименты"}),
    frozenset({"huquq", "право"}),
    frozenset({"majburiyat", "обязанность"}),
    frozenset({"qonun", "закон"}),
    frozenset({"modda", "статья"}),
    frozenset({"mehnat", "труд"}),
    frozenset({"zarar", "ущерб", "вред"}),
    frozenset({"muddat", "срок"}),
    frozenset({"ariza", "заявление"}),
    frozenset({"qaror", "решение", "постановление"}),
    frozenset({"shikoyat", "жалоба"}),
)



def _norm(term: str) -> str:
    """Normalise a group entry exactly as query tokens are normalised.

    Without this the table is keyed on "o'zi" while the query arrives as
    "ozi" — the apostrophe having already been folded away — and the lookup
    silently never matches.
    """
    return " ".join(normalise_token(w) for w in term.split())


def _index() -> dict[str, frozenset[str]]:
    """Term -> the other members of its group, in both Uzbek scripts."""
    table: dict[str, frozenset[str]] = {}
    for group in SYNONYM_GROUPS:
        expanded: set[str] = set()
        for term in group:
            expanded.add(_norm(term))
            cyrillic = _norm(latin_to_cyrillic(term))
            if cyrillic:
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
