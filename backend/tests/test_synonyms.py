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

from app.db.models import Language
from app.services.rag.keyword import build_tsquery
from app.services.rag.synonyms import expand_tokens


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
