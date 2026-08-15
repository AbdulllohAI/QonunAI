"""Legal synonym expansion.

Motivated by a measured failure: Labour Code art. 160 ranks 1st when the
question uses *xodim*, the statute's own word, and does not appear in the top 20
when the same question uses *ishchi*, the word an ordinary person uses. Neither
transliteration nor the multilingual embedding bridged it.

The risk being managed here is the opposite one. In a tool whose claim is that
it cites the governing provision, grouping terms a lawyer distinguishes is worse
than missing a result, so several tests below pin down what must *not* be
treated as equivalent.
"""
from __future__ import annotations

import pytest

from app.db.models import Language
from app.services.rag.keyword import build_tsquery
from app.services.rag.synonyms import SYNONYM_GROUPS, expand_tokens


def _terms(query: str, language: Language) -> set[str]:
    _, tsquery = build_tsquery(query, language)
    return {t.strip().removesuffix(":*") for t in tsquery.split("|")}


# ---------------------------------------------------------------- expansion

def test_ishchi_reaches_the_statutes_word():
    assert "xodim" in expand_tokens(["ishchi"])


def test_expansion_is_bidirectional():
    assert "ishchi" in expand_tokens(["xodim"])


def test_expansion_only_adds():
    """The user's own words must survive; expansion is additive."""
    assert "ishchi" not in expand_tokens(["ishchi"])


def test_multi_word_phrases_expand():
    """Tokens arrive already normalised, so the table must be keyed that way."""
    extra = expand_tokens(["ishdan", "boshash"])
    assert any("mehnat shartnomasini bekor qilish" in e for e in extra), extra


def test_unknown_tokens_expand_to_nothing():
    assert expand_tokens(["kartoshka", "osh"]) == []


def test_empty_input():
    assert expand_tokens([]) == []


def test_russian_employment_synonyms():
    assert "сотрудник" in expand_tokens(["работник"])


# ------------------------------------------------- end-to-end через tsquery

def test_synonym_reaches_the_cyrillic_corpus_form():
    """The Labour Code exists only in Cyrillic, so the useful expansion of a
    Latin query is the Cyrillic form of the statute's word."""
    terms = _terms("Ishchi o'zi ishdan bo'shamoqchi bo'lsa nima qiladi?", Language.UZ_LATN)
    assert any("ходим".startswith(t) for t in terms), sorted(terms)


def test_reflexive_pronoun_carries_the_initiative_sense():
    """"o'zi" looks like scaffolding and is not: "at the employee's own
    initiative" is exactly what separates art. 160 from art. 166
    (employer-initiated), so it is expanded rather than stripped."""
    _, tsquery = build_tsquery(
        "Ishchi o'zi ishdan bo'shamoqchi bo'lsa nima qiladi?", Language.UZ_LATN
    )
    assert "ташаббус" in tsquery, tsquery


def test_multi_word_synonyms_are_valid_tsquery_terms():
    """A space inside a tsquery term is a syntax error, and to_tsquery raising
    takes down the whole branch through its except-and-return-[] handler."""
    _, tsquery = build_tsquery("ish beruvchi kim?", Language.UZ_LATN)
    for term in tsquery.split("|"):
        term = term.strip()
        if " " in term:
            assert term.startswith("(") and "<->" in term, term


# -------------------------------------------- what must NOT be conflated

def test_contract_and_transaction_are_not_synonyms():
    """shartnoma (contract) and bitim (transaction) are distinct in the Civil
    Code, however interchangeable they sound in ordinary speech."""
    assert "bitim" not in expand_tokens(["shartnoma"])
    assert "shartnoma" not in expand_tokens(["bitim"])


def test_russian_contract_and_transaction_are_not_synonyms():
    assert "сделка" not in expand_tokens(["договор"])


def test_fine_and_punishment_are_not_conflated():
    """A fine is one kind of penalty, not a synonym for penalty in general."""
    assert "jazo" not in expand_tokens(["jarima"])


# ------------------------------------------------ Uzbek <-> Russian bridge

def test_uzbek_term_reaches_its_russian_counterpart():
    """43% of this corpus is Russian-only. Without a bridge, an Uzbek question
    cannot reach it through the keyword branches at all."""
    assert "сделка" in expand_tokens(["bitim"])


def test_bridge_is_bidirectional():
    assert "bitim" in expand_tokens(["сделка"])


def test_cyrillic_uzbek_also_bridges():
    """The Uzbek side is written in Latin in the table; Cyrillic is generated."""
    assert "сделка" in expand_tokens(["битим"])


def test_interrogation_bridges_for_procedure_questions():
    assert "допрос" in expand_tokens(["soroq"])


def test_truncated_form_can_reach_a_russian_fleeting_vowel():
    """The bridge is only useful if it survives inflection: the corpus has
    "сделок", the glossary has "сделка", and neither prefixes the other."""
    terms = _terms("Битим деб нима тушунилади?", Language.UZ_CYRL)
    assert any("сделок".startswith(t) for t in terms), sorted(terms)


def test_bridge_keeps_contract_and_transaction_apart_across_languages():
    """The two languages must not become a back channel for merging terms the
    Civil Code distinguishes."""
    assert "договор" not in expand_tokens(["bitim"])
    assert "сделка" not in expand_tokens(["shartnoma"])


# --------------------------------------------- terms of art vs lay phrasing

def test_lay_description_reaches_the_legal_doctrine():
    """The Criminal Code defines "невменяемость" as being unable to understand
    the significance of one's actions — which is how a non-lawyer says it."""
    from app.services.rag.query_prep import content_tokens

    tokens = content_tokens(
        "Отвечает ли человек, который не понимал своих действий из-за болезни?", "ru"
    )
    assert "невменяемость" in expand_tokens(tokens)


def test_admissibility_bridges_to_ordinary_wording():
    from app.services.rag.query_prep import content_tokens

    tokens = content_tokens("Қандай далиллар судда қабул қилинади?", "uz-Cyrl")
    assert any("мақбул" in t for t in expand_tokens(tokens))


def test_expansion_only_ever_adds_candidates():
    """Terms of art widen the net; they never remove what the user typed, so a
    question about adopting a law keeps its own words even though "qabul" also
    carries the admissibility sense."""
    from app.services.rag.query_prep import content_tokens

    tokens = content_tokens("Qonun qanday qabul qilinadi?", "uz-Latn")
    extra = expand_tokens(tokens)
    assert "qonun" in tokens
    assert all(t not in extra for t in tokens)


# ------------------------------------------------------------------ English

# English is the language that most needs the glossary and had none of it: the
# corpus contains no English text at all, so the lexical branches had nothing
# to match and dense retrieval carried English questions alone. Measured
# before these entries existed, "What form must a labour contract take?"
# returned Civil Code 366 at 0.21 and never reached Labour Code 106, while the
# Uzbek phrasing of the same question hit the Labour Code at 0.40.


@pytest.mark.parametrize(
    "english,expected",
    [
        ("contract", "shartnoma"),
        ("employee", "xodim"),
        ("employer", "работодатель"),
        ("dismissal", "увольнение"),
        ("theft", "кража"),
        ("divorce", "развод"),
        ("court", "суд"),
        ("annual leave", "отпуск"),
        ("unlawful", "noqonuniy"),
    ],
)
def test_english_reaches_the_corpus_languages(english, expected):
    assert expected in expand_tokens([english])


def test_english_terms_are_never_transliterated():
    """`latin_to_cyrillic` exists so Uzbek Latin also matches Uzbek Cyrillic
    text. Run over English it produces keys like "cонтраcт" that match nothing
    in any language while still being emitted into every tsquery."""
    import re

    from app.services.rag.synonyms import _TABLE

    mixed = [k for k in _TABLE if re.search(r"[a-z]", k) and re.search(r"[а-яёқғҳў]", k)]
    assert mixed == []


def test_every_declared_english_term_is_actually_used():
    """Keeps the skip-list honest: a term dropped from a group but left in
    `_ENGLISH_TERMS` would be dead weight nobody notices."""
    from app.services.rag.synonyms import _ENGLISH_TERMS

    in_groups: set[str] = set()
    for group in SYNONYM_GROUPS:
        in_groups |= set(group)
    assert _ENGLISH_TERMS - in_groups == set()


def test_english_does_not_collapse_contract_and_transaction():
    """The distinction the Uzbek and Russian groups exist to preserve, which
    English blurs: договор/shartnoma is a contract, сделка/bitim a transaction.
    """
    contract = expand_tokens(["contract"])
    transaction = expand_tokens(["transaction"])

    assert "shartnoma" in contract and "договор" in contract
    assert "bitim" in transaction and "сделка" in transaction
    assert "bitim" not in contract and "сделка" not in contract
    assert "shartnoma" not in transaction and "договор" not in transaction


def test_a_term_in_two_groups_reaches_both():
    """"dismissal" sits with the Uzbek phrasings and the Russian ones. The
    index unions rather than assigns, or whichever group was declared last
    would silently win."""
    out = expand_tokens(["dismissal"])
    assert "увольнение" in out
    assert any("ishdan" in t for t in out)


def test_english_is_a_key_but_never_an_emitted_term():
    """English can start a lookup; it must never come out of one.

    No English text exists anywhere in the corpus, so an English term in a
    tsquery cannot match a document — it only consumes the 60-term budget in
    `build_tsquery`, which truncates rather than errors. When these entries
    were first added they pushed an Uzbek query from 58 terms past the cap,
    and the overflow dropped the transliterated Cyrillic forms that are the
    only route from a Latin query to the Cyrillic-only Labour Code.
    """
    from app.services.rag.synonyms import _ENGLISH_TERMS, _norm

    english = {_norm(t) for t in _ENGLISH_TERMS}
    for term in ("dismissal", "contract", "employee", "divorce"):
        out = set(expand_tokens([term]))
        assert out, f"{term} should still expand"
        assert not (out & english), f"{term} expanded to another English term"


def test_adding_english_did_not_cost_uzbek_any_terms():
    """The regression above, pinned at the level it actually bit."""
    terms = _terms(
        "Xodim o'z tashabbusi bilan mehnat shartnomasini bekor qiladi",
        Language.UZ_LATN,
    )
    assert any("ташаббусига".startswith(t.strip("()")) for t in terms) or any(
        "ташабб" in t for t in terms
    ), sorted(terms)
    assert len(terms) <= 60, "at the truncation cap; something will be dropped"


def test_english_queries_are_not_transliterated_into_cyrillic():
    """Script variants bridge Uzbek Latin and Cyrillic — the same language in
    two alphabets. Applied to English they produced "ҳоw манй дайс оф аннуал
    леаве", which matches nothing and took nine of twenty-one slots in the
    60-term budget, crowding out the terms the glossary had just supplied."""
    import re

    _, tsquery = build_tsquery("How many days of annual leave am I entitled to?", Language.EN)
    terms = [t.strip().removesuffix(":*") for t in tsquery.split("|")]
    garbage = [t for t in terms if re.fullmatch(r"[а-яёқғҳўй]+", t) and t not in {"отпуск", "татил"}]
    assert not any(t in garbage for t in ("ҳоw", "манй", "дайс", "аннуал", "леаве")), terms
    # and the real bridge survived
    assert any("отпуск" in t for t in terms), terms
