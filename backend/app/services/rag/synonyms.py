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
               "трудящийся", "employee", "worker"}),
    frozenset({"ish beruvchi", "ishberuvchi", "работодатель", "employer"}),
    # Leaving a job: the statute frames it as terminating the contract.
    # The English terms appear here and in the Russian group below; that is
    # what the union in `_index` is for.
    frozenset({"ishdan bo'shash", "ishdan bo'shatish", "ishdan ketish",
               "ishdan chiqish", "mehnat shartnomasini bekor qilish",
               "dismissal", "dismissed", "fired", "termination of employment"}),
    # Voluntary resignation. The Labour Code frames it as termination "at the
    # employee's initiative"; people say "I want to leave myself". This is the
    # distinction between art. 160 and art. 166 (employer-initiated), so the
    # "own initiative" sense is signal, not scaffolding — which is why the
    # reflexive pronoun is expanded rather than stripped as framing.
    frozenset({"o'z tashabbusi", "o'z xohishi", "o'z arizasi", "o'zi",
               "xodimning tashabbusi", "ixtiyoriy", "resignation", "resign",
               "own initiative", "voluntarily"}),
    frozenset({"ish haqi", "oylik", "maosh", "заработная плата", "зарплата",
               "оплата труда", "wage", "wages", "salary"}),
    frozenset({"mehnat ta'tili", "ta'til", "отпуск", "трудовой отпуск",
               "annual leave", "vacation", "holiday"}),
    # --- Uzbek: general ---------------------------------------------------
    frozenset({"jazo", "jazolash", "наказание", "мера наказания",
               "punishment", "penalty", "sentence"}),
    frozenset({"jarima", "pul jarimasi", "штраф", "денежное взыскание",
               "fine"}),
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
               "юридическая ответственность", "liability", "responsibility"}),
    # --- Russian: employment ----------------------------------------------
    frozenset({"увольнение", "расторжение трудового договора",
               "прекращение трудового договора",
               "dismissal", "dismissed", "fired", "termination of employment"}),
    # Listed without the preposition too: "по" is stripped as framing before
    # expansion runs, so a group keyed only on the full phrase never matches.
    frozenset({"собственному желанию", "собственное желание",
               "инициативе работника", "своей инициативе",
               "resignation", "resign", "own initiative"}),
    # --- Russian: general -------------------------------------------------
    frozenset({"жилье", "жилище", "жилое помещение", "housing", "dwelling"}),
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
    # English is carried in the same groups rather than a separate table.
    #
    # It needs the bridge more than either other language, because there is no
    # English *anywhere* in this corpus — not one chunk. Uzbek and Russian each
    # have text of their own to match against, so a lexical miss still leaves
    # them something; an English question has only dense retrieval, and bge-m3
    # scores English against Uzbek-Cyrillic legal text far lower than it scores
    # Uzbek. Measured before this glossary existed: "What form must a labour
    # contract take?" returned Civil Code 366 at 0.21 and never reached Labour
    # Code 106, while the Uzbek phrasing of the same question hit the Labour
    # Code at 0.40.
    #
    # The same conservatism applies as above, and one distinction is worth
    # naming because English blurs it: "contract" belongs with shartnoma /
    # договор, "transaction" with bitim / сделка. Merging them would undo the
    # separation the two groups exist to preserve.
    frozenset({"bitim", "сделка", "transaction"}),
    frozenset({"shartnoma", "договор", "contract", "agreement"}),
    frozenset({"mulk", "собственность", "property", "ownership"}),
    frozenset({"meros", "наследство", "наследование", "inheritance",
               "succession"}),
    frozenset({"jinoyat", "преступление", "crime", "criminal offence",
               "offence"}),
    frozenset({"o'g'irlik", "кража", "хищение", "theft", "stealing"}),
    frozenset({"qotillik", "odam o'ldirish", "убийство", "murder",
               "homicide"}),
    frozenset({"ayb", "вина", "guilt", "fault"}),
    frozenset({"so'roq", "допрос", "interrogation", "questioning"}),
    frozenset({"dalil", "доказательство", "evidence", "proof"}),
    frozenset({"tergovchi", "следователь", "investigator"}),
    frozenset({"guvoh", "свидетель", "witness"}),
    frozenset({"da'vo", "иск", "claim", "lawsuit"}),
    frozenset({"sud", "суд", "court"}),
    frozenset({"sudya", "судья", "judge"}),
    frozenset({"soliq", "налог", "tax", "taxation"}),
    frozenset({"nikoh", "брак", "marriage"}),
    # Divorce had no group at all, in any language.
    frozenset({"ajralish", "ajrashish", "nikohni bekor qilish", "развод",
               "расторжение брака", "divorce"}),
    frozenset({"farzandlikka olish", "усыновление", "adoption"}),
    frozenset({"vasiylik", "опека", "guardianship", "custody"}),
    frozenset({"aliment", "алименты", "alimony", "child support",
               "maintenance"}),
    frozenset({"huquq", "право", "right", "rights"}),
    frozenset({"majburiyat", "обязанность", "obligation", "duty"}),
    frozenset({"qonun", "закон", "law", "statute"}),
    frozenset({"modda", "статья", "article"}),
    frozenset({"mehnat", "труд", "labour", "labor", "employment"}),
    frozenset({"zarar", "ущерб", "вред", "damage", "damages", "harm"}),
    frozenset({"muddat", "срок", "deadline", "time limit"}),
    frozenset({"ariza", "заявление", "application"}),
    frozenset({"qaror", "решение", "постановление", "decision", "ruling"}),
    frozenset({"shikoyat", "жалоба", "complaint"}),
    # "Unlawful" qualifies half the questions people actually ask, and matched
    # nothing in any language.
    frozenset({"noqonuniy", "g'ayriqonuniy", "незаконный", "незаконно",
               "неправомерный", "unlawful", "illegal", "wrongful"}),
)



def _norm(term: str) -> str:
    """Normalise a group entry exactly as query tokens are normalised.

    Without this the table is keyed on "o'zi" while the query arrives as
    "ozi" — the apostrophe having already been folded away — and the lookup
    silently never matches.
    """
    return " ".join(normalise_token(w) for w in term.split())


#: English terms are Latin script but must not be transliterated.
#:
#: The transliterator exists so an Uzbek Latin entry also matches Uzbek
#: Cyrillic text in the corpus. Run over English it produces garbage —
#: "contract" becomes "cонтраcт", "employee" becomes "емплоее" — keys that
#: cannot match anything in any of the three languages, while still being
#: emitted into every tsquery that expands one of these terms. The cost is
#: latency on exactly the queries the glossary was added to help.
#:
#: Listed explicitly because nothing distinguishes English from Uzbek Latin
#: mechanically: both are ASCII, and "contract" and "shartnoma" look equally
#: Latin to a regex. A test asserts this stays in step with the groups.
_ENGLISH_TERMS = frozenset({
    "employee", "worker", "employer", "dismissal", "dismissed", "fired",
    "termination of employment", "resignation", "resign", "own initiative",
    "voluntarily", "wage", "wages", "salary", "annual leave", "vacation",
    "holiday", "punishment", "penalty", "sentence", "fine", "liability",
    "responsibility", "housing", "dwelling", "transaction", "contract",
    "agreement", "property", "ownership", "inheritance", "succession",
    "crime", "criminal offence", "offence", "theft", "stealing", "murder",
    "homicide", "guilt", "fault", "interrogation", "questioning", "evidence",
    "proof", "investigator", "witness", "claim", "lawsuit", "court", "judge",
    "tax", "taxation", "marriage", "divorce", "adoption", "guardianship",
    "custody", "alimony", "child support", "maintenance", "right", "rights",
    "obligation", "duty", "law", "statute", "article", "labour", "labor",
    "employment", "damage", "damages", "harm", "deadline", "time limit",
    "application", "decision", "ruling", "complaint", "unlawful", "illegal",
    "wrongful",
})


def _index() -> dict[str, frozenset[str]]:
    """Term -> the other members of its group, in both Uzbek scripts."""
    table: dict[str, frozenset[str]] = {}
    for group in SYNONYM_GROUPS:
        expanded: set[str] = set()
        for term in group:
            expanded.add(_norm(term))
            if term in _ENGLISH_TERMS:
                continue
            cyrillic = _norm(latin_to_cyrillic(term))
            if cyrillic:
                expanded.add(cyrillic)
        # English is a key, never a value.
        #
        # There is no English text anywhere in this corpus, so emitting an
        # English term *into* a tsquery cannot match a document — it only
        # consumes the 60-term budget in `build_tsquery`. That budget is a
        # hard truncation, so the cost is not merely waste: adding English to
        # these groups pushed an Uzbek query from 58 terms to over 60 and the
        # overflow silently dropped "ташаббуси" and "ходимнинг ташаббуси",
        # the transliterated forms that are the only way a Latin query reaches
        # the Cyrillic-only Labour Code art. 160. Caught by an existing test.
        emitted = expanded - _english_forms()
        for term in expanded:
            # Union, not assignment. A term can legitimately belong to more
            # than one group — "dismissal" sits with both the Uzbek and the
            # Russian phrasings of leaving a job — and plain assignment would
            # silently keep only whichever group happened to be declared last.
            table[term] = table.get(term, frozenset()) | (emitted - {term})
    return table


def _english_forms() -> frozenset[str]:
    """The English terms as they appear once normalised."""
    global _ENGLISH_NORMALISED
    if _ENGLISH_NORMALISED is None:
        _ENGLISH_NORMALISED = frozenset(_norm(t) for t in _ENGLISH_TERMS)
    return _ENGLISH_NORMALISED


_ENGLISH_NORMALISED: frozenset[str] | None = None


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
