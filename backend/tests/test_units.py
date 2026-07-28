"""Unit tests for the pure logic — no database or network required.

These cover the pieces where a silent regression would be most damaging:
transliteration (halves Uzbek recall if wrong), hierarchy parsing (wrong
citations), citation validation (hallucinations reaching users), and the
hierarchy-of-force rules (wrong legal conclusions).
"""
from __future__ import annotations

from datetime import date

import pytest

from app.db.models import ActType, Language, NodeType
from app.services.ingestion.chunker import LegalChunker, estimate_tokens
from app.services.ingestion.hierarchy_builder import classify, hierarchy_builder
from app.services.ingestion.parsers.base import ParsedBlock
from app.services.lang.detect import detect_language
from app.services.lang.translit import (
    cyrillic_to_latin,
    latin_to_cyrillic,
    script_of,
    script_variants,
)
from app.services.rag.context_builder import ContextBuilder
from app.services.rag.crossref import extract_references
from app.services.rag.keyword import extract_article_numbers, infer_act_types
from app.services.rag.types import RetrievedChunk
from app.services.reasoning import hierarchy as hierarchy_mod
from app.services.reasoning import risk as risk_mod
from app.services.reasoning import validator as validator_mod


# ----------------------------------------------------------- transliteration


@pytest.mark.parametrize(
    "latin,expected_cyr",
    [
        ("shartnoma", "шартнома"),
        ("o‘zbekiston", "ўзбекистон"),
        ("g‘alaba", "ғалаба"),
        ("chegara", "чегара"),
        ("qonun", "қонун"),
        ("huquq", "ҳуқуқ"),
    ],
)
def test_latin_to_cyrillic(latin, expected_cyr):
    assert latin_to_cyrillic(latin) == expected_cyr


def test_digraphs_are_not_split():
    # If "sh" degraded to с+ҳ, Uzbek keyword search would collapse.
    assert latin_to_cyrillic("shahar") == "шаҳар"
    assert latin_to_cyrillic("choy") == "чой"


def test_roundtrip_preserves_meaning():
    original = "O‘zbekiston Respublikasi qonuni"
    assert cyrillic_to_latin(latin_to_cyrillic(original)).lower() == original.lower()


def test_apostrophe_variants_normalise():
    for variant in ("o'zbek", "oʻzbek", "o‘zbek", "o`zbek"):
        assert latin_to_cyrillic(variant) == "ўзбек"


def test_script_detection():
    assert script_of("shartnoma") == "latn"
    assert script_of("шартнома") == "cyrl"
    assert script_of("") == "unknown"


def test_script_variants_covers_both():
    variants = script_variants("shartnoma")
    assert "shartnoma" in variants
    assert "шартнома" in variants


# ------------------------------------------------------- language detection


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Shartnoma qanday tuziladi?", Language.UZ_LATN),
        ("Как заключается договор по закону?", Language.RU),
        ("How is a contract concluded under the law?", Language.EN),
        ("Шартнома қандай тузилади?", Language.UZ_CYRL),
    ],
)
def test_language_detection(text, expected):
    assert detect_language(text) == expected


def test_uzbek_cyrillic_not_confused_with_russian():
    # ў/қ/ғ/ҳ exist in Uzbek Cyrillic but not Russian.
    assert detect_language("Ўзбекистон Республикаси қонунлари") == Language.UZ_CYRL


def test_cyrillic_body_wrapped_in_latin_chrome_still_detects_cyrillic():
    """Scraped pages are mixed-script by nature and must not fall back to Latin.

    lex.uz wraps Cyrillic statutory text in Latin-script navigation, landing
    around 60-70% Cyrillic — which `script_of` calls "mixed". Before the
    dominance check, that discarded a correct Cyrillic signal and tagged whole
    acts as uz-Latn, breaking language filtering and FTS dictionary choice.
    """
    latin_chrome = "Hujjatga taklif yuborish Audioni tinglash Bosh sahifa Qidiruv " * 12
    cyrillic_body = (
        "Ўзбекистон Республикасининг Конституцияси. 1-модда. Ўзбекистон "
        "суверен демократик республикадир. Давлат ҳокимияти халқ манфаатларига "
        "хизмат қилади ва қонун устуворлиги таъминланади. " * 12
    )
    assert detect_language(latin_chrome + cyrillic_body) == Language.UZ_CYRL


def test_predominantly_latin_mixed_text_stays_latin():
    """The dominance check must not flip genuinely Latin text to Cyrillic."""
    text = (
        "Shartnoma yozma shaklda tuziladi va tomonlar tomonidan imzolanadi. "
        "Fuqarolik kodeksi bo'yicha majburiyatlar belgilanadi. " * 12
    ) + "Гражданский кодекс"
    assert detect_language(text) == Language.UZ_LATN


# ------------------------------------------------------- hierarchy parsing


@pytest.mark.parametrize(
    "text,node_type,number",
    [
        ("54-modda. Shartnoma tushunchasi", NodeType.MODDA, "54"),
        ("Modda 12", NodeType.MODDA, "12"),
        ("Статья 105. Убийство", NodeType.MODDA, "105"),
        ("Article 7. Definitions", NodeType.MODDA, "7"),
        ("III bob. Jinoyat tushunchasi", NodeType.BOB, "III"),
        ("ГЛАВА IV", NodeType.BOB, "IV"),
        ("BIRINCHI BO‘LIM UMUMIY QOIDALAR", NodeType.BOLIM, "BIRINCHI"),
        ("UMUMIY QISM", NodeType.QISM, "UMUMIY"),
        ("54-1-modda. Qo‘shimcha", NodeType.MODDA, "54-1"),
    ],
)
def test_classify(text, node_type, number):
    result = classify(text)
    assert result is not None, f"failed to classify: {text}"
    assert result[0] is node_type
    assert result[1] == number


def test_plain_text_is_not_a_heading():
    assert classify("Shartnoma yozma shaklda tuziladi.") is None


@pytest.mark.parametrize(
    "text,node_type,number",
    [
        # Uzbek Cyrillic. These are DIFFERENT WORDS from the Russian ones
        # (модда ≠ статья, боб ≠ глава, бўлим ≠ раздел). lex.uz serves many
        # acts in this script; when these patterns were missing, every Uzbek
        # Cyrillic act ingested with zero detected articles — the text was
        # searchable but nothing in it could be cited or article-pinned.
        ("5-модда. Давлат suvereniteti", NodeType.MODDA, "5"),
        ("МОДДА 12", NodeType.MODDA, "12"),
        ("105-модда", NodeType.MODDA, "105"),
        ("III боб. Умумий қоидалар", NodeType.BOB, "III"),
        ("БИРИНЧИ БЎЛИМ", NodeType.BOLIM, "БИРИНЧИ"),
        ("УМУМИЙ ҚИСМ", NodeType.QISM, "УМУМИЙ"),
        ("МАХСУС ҚИСМ", NodeType.QISM, "МАХСУС"),
    ],
)
def test_classify_uzbek_cyrillic(text, node_type, number):
    result = classify(text)
    assert result is not None, f"failed to classify Uzbek Cyrillic: {text}"
    assert result[0] is node_type
    assert result[1] == number


def test_uzbek_cyrillic_article_extraction_from_query():
    """A user typing Cyrillic must still get exact-article pinning."""
    assert extract_article_numbers("Жиноят кодексининг 105-моддаси") == ["105"]
    assert extract_article_numbers("модда 54") == ["54"]


def test_uzbek_cyrillic_self_reference():
    self_refs, _ = extract_references(
        "ушбу Кодекснинг 333-моддасида назарда тутилган ҳолларда"
    )
    assert "333" in self_refs


def test_builder_nests_articles_under_chapters():
    blocks = [
        ParsedBlock(text="UMUMIY QISM", role="heading"),
        ParsedBlock(text="I bob. Asosiy qoidalar", role="heading"),
        ParsedBlock(text="1-modda. Qonunchilik", role="heading"),
        ParsedBlock(text="Jinoyat qonunchiligi Konstitutsiyaga asoslanadi.", role="body"),
        ParsedBlock(text="2-modda. Prinsiplar", role="heading"),
        ParsedBlock(text="Qonuniylik prinsipi amal qiladi.", role="body"),
    ]
    nodes = hierarchy_builder.build(blocks, language=Language.UZ_LATN)
    articles = [n for n in nodes if n.node_type is NodeType.MODDA]

    assert [a.number for a in articles] == ["1", "2"]
    assert articles[0].article_number == "1"
    assert "Konstitutsiyaga" in articles[0].body
    # Path records the full structural location.
    assert articles[0].path.endswith("/1")
    assert "I" in articles[0].path


def test_body_without_heading_becomes_preamble():
    nodes = hierarchy_builder.build(
        [ParsedBlock(text="Ushbu Kodeks munosabatlarni tartibga soladi.", role="body")],
        language=Language.UZ_LATN,
    )
    assert nodes[0].node_type is NodeType.PREAMBLE


# -------------------------------------------------------------- chunking


def test_short_article_stays_one_chunk():
    from app.services.ingestion.hierarchy_builder import BuiltNode

    node = BuiltNode(
        node_type=NodeType.MODDA, number="54", article_number="54",
        heading="Shartnoma", language=Language.UZ_LATN, path="I/54",
    )
    node.body_parts.append("Shartnoma yozma shaklda tuziladi.")
    chunks = LegalChunker().chunk_nodes([node])

    assert len(chunks) == 1
    assert chunks[0].article_number == "54"
    # The chunk repeats its own citation context — it is retrieved in isolation.
    assert "54-modda" in chunks[0].text


def test_long_article_splits_on_clauses_and_keeps_article_number():
    from app.services.ingestion.hierarchy_builder import BuiltNode

    node = BuiltNode(
        node_type=NodeType.MODDA, number="99", article_number="99",
        heading="Uzun modda", language=Language.UZ_LATN, path="I/99",
    )
    node.body_parts.append(
        "\n".join(f"{i}) " + "matn " * 120 for i in range(1, 8))
    )
    chunks = LegalChunker(target_tokens=200, max_tokens=300).chunk_nodes([node])

    assert len(chunks) > 1
    assert all(c.article_number == "99" for c in chunks)


def test_token_estimate_is_positive():
    assert estimate_tokens("qisqa matn") > 0


# ---------------------------------------------------------- query parsing


@pytest.mark.parametrize(
    "query,expected",
    [
        ("What does Article 54 of the Civil Code say?", ["54"]),
        ("Jinoyat kodeksining 105-moddasi", ["105"]),
        ("Что говорит статья 208?", ["208"]),
        ("ст. 12 и статья 15", ["12", "15"]),
        ("no article here", []),
    ],
)
def test_extract_article_numbers(query, expected):
    assert extract_article_numbers(query) == expected


def test_infer_act_types():
    assert ActType.CODE in infer_act_types("Fuqarolik kodeksi bo'yicha savol")
    assert ActType.CONSTITUTION in infer_act_types("Konstitutsiya 21-modda")
    assert infer_act_types("umumiy savol") == []


# ------------------------------------------------------- cross-references


def test_extract_self_references():
    self_refs, _ = extract_references(
        "Ushbu Kodeksning 333-moddasida nazarda tutilgan hollarda javobgarlik yuzaga keladi."
    )
    assert "333" in self_refs


def test_extract_russian_self_reference():
    self_refs, _ = extract_references("в случаях, предусмотренных статьей 45 настоящего Кодекса")
    assert "45" in self_refs


def test_extract_external_reference():
    _, external = extract_references("Article 54 of the Civil Code applies here.")
    assert any(article == "54" for _, article in external)


# ------------------------------------------------------ citation validation


def _source(tag: str, article: str, act_type=ActType.CODE):
    from app.services.rag.context_builder import SourceRef

    return SourceRef(
        tag=tag,
        chunk=RetrievedChunk(
            chunk_id=__import__("uuid").uuid4(),
            act_id=__import__("uuid").uuid4(),
            text=f"Text of article {article}",
            law_name="Civil Code",
            article_number=article,
            act_type=act_type,
            language=Language.EN,
            hierarchy_path="I/" + article,
        ),
    )


def test_valid_citations_pass():
    sources = [_source("S1", "54"), _source("S2", "55")]
    result = validator_mod.validate("A contract is binding [S1] and enforceable [S2].", sources)

    assert result.is_clean
    assert result.used_tags == ["S1", "S2"]
    assert not result.invalid_tags


def test_fabricated_tag_is_stripped():
    sources = [_source("S1", "54")]
    result = validator_mod.validate("This is true [S1] and so is this [S7].", sources)

    assert result.invalid_tags == ["S7"]
    assert "[S7]" not in result.text
    assert "[S1]" in result.text


def test_unretrieved_article_number_is_flagged():
    sources = [_source("S1", "54")]
    result = validator_mod.validate("See Article 999 of the Civil Code [S1].", sources)

    assert "999" in result.unverified_articles
    assert not result.is_clean
    assert validator_mod.build_warning(result) is not None


def test_multi_tag_syntax_is_normalised():
    sources = [_source("S1", "54"), _source("S2", "55")]
    result = validator_mod.validate("Both apply [S1, S2].", sources)
    assert set(result.used_tags) == {"S1", "S2"}


def _source_named(tag: str, law_name: str, article: str):
    from app.services.rag.context_builder import SourceRef

    return SourceRef(
        tag=tag,
        chunk=RetrievedChunk(
            chunk_id=__import__("uuid").uuid4(),
            act_id=__import__("uuid").uuid4(),
            text="...",
            law_name=law_name,
            article_number=article,
            act_type=ActType.CODE,
            language=Language.UZ_CYRL,
            hierarchy_path="",
        ),
    )


_TAX_RU = "30.12.2019. Ўзбекистон Республикасининг Солиқ кодекси"
_CIVIL_UZC = "29.08.1996. Ўзбекистон Республикасининг Фуқаролик кодекси (иккинчи қисм)"
_CIVPROC_UZC = "22.01.2018. Ўзбекистон Республикасининг Фуқаролик процессуал кодекси"


def test_citation_attributed_to_wrong_act_is_flagged():
    """Observed failure: a real tag presented as a law that was never retrieved.

    The model answered a Land Code question using Tax and Civil Code sources
    and wrote "...va Yer kodeksida [S1]", where S1 is Tax Code Art. 424. Tag
    and article checks both pass — only comparing the claimed act name against
    the source's own law_name catches it.
    """
    sources = [_source_named("S1", _TAX_RU, "424"), _source_named("S2", _CIVIL_UZC, "575")]
    result = validator_mod.validate(
        "Yer uchastkasini ijaraga olish tartibi Fuqarolik kodeksida [S2] "
        "va Yer kodeksida [S1] belgilab qo'yilgan.",
        sources,
    )
    assert any(
        m.tag == "S1" and m.claimed == "land" and m.actual == "tax"
        for m in result.mislabelled
    )
    assert not result.is_clean
    assert "Land Code" in (validator_mod.build_warning(result) or "")


def test_act_absent_from_corpus_is_reported():
    sources = [_source_named("S1", _TAX_RU, "424")]
    result = validator_mod.validate("Yer kodeksiga ko'ra [S1] ijara tuziladi.", sources)
    assert "land" in result.uncited_acts


def test_correct_act_attribution_is_not_flagged():
    sources = [_source_named("S2", _CIVIL_UZC, "575")]
    result = validator_mod.validate(
        "Fuqarolik kodeksining 575-moddasiga ko'ra [S2] shartnoma tuziladi.", sources
    )
    assert result.mislabelled == []


def test_procedural_code_not_confused_with_substantive():
    """'Fuqarolik protsessual' must not match the plain 'civil' pattern."""
    sources = [_source_named("S1", _CIVPROC_UZC, "146")]
    result = validator_mod.validate(
        "Fuqarolik protsessual kodeksiga muvofiq [S1] da'vo taqdim etiladi.", sources
    )
    assert result.mislabelled == []


def test_act_attribution_matches_across_scripts():
    """A Russian-language claim about a Uzbek-Cyrillic source still matches."""
    sources = [_source_named("S1", _TAX_RU, "424")]
    assert validator_mod.validate(
        "Согласно Налоговому кодексу [S1] применяется ставка.", sources
    ).mislabelled == []
    assert any(
        m.claimed == "land"
        for m in validator_mod.validate(
            "Согласно Земельному кодексу [S1] ...", sources
        ).mislabelled
    )


def test_mislabelled_citation_gets_an_inline_correction():
    """The wrong act name stays in the prose (rewriting an inflected act name
    in place risks broken grammar across scripts), but the correction now
    sits right next to the tag instead of only in a warning paragraph the
    reader has to cross-reference separately."""
    sources = [_source_named("S1", _TAX_RU, "424"), _source_named("S2", _CIVIL_UZC, "575")]
    result = validator_mod.validate(
        "Yer uchastkasini ijaraga olish tartibi Fuqarolik kodeksida [S2] "
        "va Yer kodeksida [S1] belgilab qo'yilgan.",
        sources,
    )
    assert "[S1] *(actually Tax Code)*" in result.text
    assert "[S2] va" in result.text  # correctly-attributed S2 is left untouched


def test_annotate_mislabelled_is_a_noop_when_nothing_flagged():
    sources = [_source_named("S2", _CIVIL_UZC, "575")]
    result = validator_mod.validate(
        "Fuqarolik kodeksining 575-moddasiga ko'ra [S2] shartnoma tuziladi.", sources
    )
    assert "actually" not in result.text


def test_long_uncited_answer_is_rejected():
    sources = [_source("S1", "54")]
    result = validator_mod.validate("Legal prose without any citation. " * 30, sources)

    assert result.rejected
    assert result.reason == "answer_without_citations"


# --------------------------------------------------------- legal hierarchy


def _chunk(act_type: ActType, article: str, updated: date | None = None, score: float = 0.8):
    import uuid

    c = RetrievedChunk(
        chunk_id=uuid.uuid4(),
        act_id=uuid.uuid4(),
        text="...",
        law_name=act_type.value,
        article_number=article,
        act_type=act_type,
        language=Language.UZ_LATN,
        hierarchy_path="",
        last_updated=updated,
    )
    c.fused_score = score
    return c


def test_constitution_outranks_cabinet_resolution():
    chunks = [
        _chunk(ActType.CABINET_RESOLUTION, "3"),
        _chunk(ActType.CONSTITUTION, "21"),
    ]
    analysis = hierarchy_mod.resolve(chunks)

    assert analysis.controlling.act_type is ActType.CONSTITUTION
    assert any(c.rule == "lex superior derogat legi inferiori" for c in analysis.conflicts)


def test_later_act_wins_at_equal_force():
    chunks = [
        _chunk(ActType.LAW, "1", updated=date(2015, 1, 1)),
        _chunk(ActType.LAW, "2", updated=date(2024, 1, 1)),
    ]
    analysis = hierarchy_mod.resolve(chunks)

    assert any(c.rule == "lex posterior derogat legi priori" for c in analysis.conflicts)
    assert analysis.controlling.last_updated == date(2024, 1, 1)


def test_commentary_is_not_controlling():
    analysis = hierarchy_mod.resolve([_chunk(ActType.COMMENTARY, "x")])
    assert analysis.controlling is None


def test_precedence_ordering_is_total():
    order = [
        ActType.CONSTITUTION, ActType.CONSTITUTIONAL_LAW, ActType.CODE, ActType.LAW,
        ActType.PRESIDENTIAL_DECREE, ActType.CABINET_RESOLUTION, ActType.MINISTERIAL_ACT,
    ]
    values = [t.precedence for t in order]
    assert values == sorted(values, reverse=True)


# ------------------------------------------------------------------- risk


def test_criminal_topic_is_high_risk():
    assessment = risk_mod.assess(
        question="Jinoyat uchun qanday jazo beriladi?",
        answer="Jazo belgilanadi [S1].",
        chunks=[_chunk(ActType.CODE, "105")],
    )
    assert assessment.level is risk_mod.RiskLevel.HIGH


def test_no_sources_is_high_risk():
    assessment = risk_mod.assess(question="q", answer="a", chunks=[])
    assert assessment.level is risk_mod.RiskLevel.HIGH


def test_routine_question_is_low_risk():
    assessment = risk_mod.assess(
        question="Shartnoma qanday shaklda tuziladi?",
        answer="Yozma shaklda tuziladi [S1]. Risk level: LOW",
        chunks=[_chunk(ActType.CODE, "54"), _chunk(ActType.CODE, "55")],
    )
    assert assessment.level is risk_mod.RiskLevel.LOW


def test_model_stated_risk_can_only_escalate():
    # The model says HIGH; heuristics say LOW. HIGH must win.
    assessment = risk_mod.assess(
        question="Shartnoma shakli?",
        answer="Javob [S1]. Risk level: HIGH",
        chunks=[_chunk(ActType.CODE, "54"), _chunk(ActType.CODE, "55")],
    )
    assert assessment.level is risk_mod.RiskLevel.HIGH


def test_parse_stated_risk_multilingual():
    assert risk_mod.parse_stated_risk("Уровень риска: ВЫСОКИЙ") is risk_mod.RiskLevel.HIGH
    assert risk_mod.parse_stated_risk("Xavf darajasi: YUQORI") is risk_mod.RiskLevel.HIGH


def test_parse_stated_risk_handles_separately_wrapped_label_and_value():
    """Regression: the model's actual preferred style closes the label's own
    bold span before opening a new one (or a backtick span) for the value,
    e.g. "**Xavf darajasi**\\n\\n`LOW`" or "**Xavf darajasi**\\n\\n**HIGH**".
    A regex that only skips one contiguous run of wrapper characters misses
    this and reads the line as absent — which used to make ensure_stated_risk
    append a second, duplicate section right under a real one."""
    assert risk_mod.parse_stated_risk("**Xavf darajasi**\n\n`LOW` — sabab.") is risk_mod.RiskLevel.LOW
    assert risk_mod.parse_stated_risk("**Xavf darajasi**\n\n**HIGH** — sabab.") is risk_mod.RiskLevel.HIGH
    assert risk_mod.parse_stated_risk("**Risk level**\n`MEDIUM`") is risk_mod.RiskLevel.MEDIUM


def test_ensure_stated_risk_does_not_duplicate_a_separately_wrapped_line():
    answer = "Javob [S1].\n\n**Xavf darajasi**\n\n`MEDIUM` — sabab.\n\n**Manbalar**\n* [S1] ..."
    assessment = risk_mod.RiskAssessment(level=risk_mod.RiskLevel.HIGH, factors=["x"])
    result = risk_mod.ensure_stated_risk(answer, assessment, Language.UZ_LATN)
    assert result.lower().count("xavf darajasi") == 1
    assert "`HIGH`" in result or "HIGH`" in result
    assert "MEDIUM" not in result


def test_rewrite_stated_risk_escalates_the_visible_label():
    # Body says MEDIUM; escalate it to HIGH so it never contradicts a badge
    # that reflects the reconciled (higher) level.
    answer = "Xulosa [S1]. Xavf darajasi: MEDIUM — asoslash."
    rewritten = risk_mod.rewrite_stated_risk(answer, risk_mod.RiskLevel.HIGH)
    assert "Xavf darajasi: HIGH" in rewritten
    assert "MEDIUM" not in rewritten


def test_rewrite_stated_risk_preserves_surrounding_text():
    answer = "Javob [S1]. Уровень риска: НИЗКИЙ — bir gap."
    rewritten = risk_mod.rewrite_stated_risk(answer, risk_mod.RiskLevel.LOW)
    assert rewritten == "Javob [S1]. Уровень риска: LOW — bir gap."


def test_ensure_stated_risk_reconciles_an_existing_line():
    # Delegates to rewrite_stated_risk when the model did include a line —
    # no duplicate section gets appended on top of it.
    answer = "Javob [S1]. Risk level: MEDIUM — sabab."
    assessment = risk_mod.RiskAssessment(level=risk_mod.RiskLevel.HIGH, factors=["x"])
    result = risk_mod.ensure_stated_risk(answer, assessment, Language.EN)
    assert result == "Javob [S1]. Risk level: HIGH — sabab."
    assert result.count("Risk level") == 1


def test_ensure_stated_risk_synthesises_a_missing_line():
    # The model skipped the section entirely — one must still appear, using
    # the same assessment that drives the risk badge, so body and badge
    # can't diverge either way.
    answer = "**Manbalar**\n\n* [S1] 44-modda."
    assessment = risk_mod.RiskAssessment(
        level=risk_mod.RiskLevel.HIGH, factors=["Subject matter involves criminal liability."]
    )
    result = risk_mod.ensure_stated_risk(answer, assessment, Language.UZ_LATN)
    assert result.startswith(answer)
    assert "Xavf darajasi" in result
    assert "HIGH" in result
    assert "criminal liability" in result


def test_ensure_stated_risk_localises_the_label_per_language():
    assessment = risk_mod.RiskAssessment(level=risk_mod.RiskLevel.LOW, factors=["Routine."])
    assert "Уровень риска" in risk_mod.ensure_stated_risk("Javob.", assessment, Language.RU)
    assert "Хавф даражаси" in risk_mod.ensure_stated_risk("Javob.", assessment, Language.UZ_CYRL)
    assert "Risk level" in risk_mod.ensure_stated_risk("Javob.", assessment, Language.EN)


def test_rewrite_stated_risk_is_a_noop_without_a_stated_line():
    answer = "Javob faqat matn, hech qanday risk yorlig'isiz [S1]."
    assert risk_mod.rewrite_stated_risk(answer, risk_mod.RiskLevel.HIGH) == answer


# --------------------------------------------------------- context builder


def test_context_groups_by_legal_force():
    chunks = [_chunk(ActType.CABINET_RESOLUTION, "3"), _chunk(ActType.CONSTITUTION, "21")]
    built = ContextBuilder().build(chunks, answer_language=Language.EN)

    assert built.sources
    # The Constitution block must precede the resolution block.
    assert built.text.index("CONSTITUTION") < built.text.index("CABINET")
    assert built.valid_tags == {"S1", "S2"}


def test_context_marks_crossref_sources():
    chunk = _chunk(ActType.CODE, "333")
    chunk.via_crossref_from = "Article 54 of the Civil Code"
    built = ContextBuilder().build([_chunk(ActType.CODE, "54"), chunk], answer_language=Language.EN)

    assert "CROSS-REFERENCED" in built.text
    assert built.sources[-1].chunk.via_crossref_from is not None


def test_context_truncates_within_budget():
    chunks = []
    for i in range(80):
        c = _chunk(ActType.CODE, str(i))
        c.text = "matn " * 2000
        chunks.append(c)
    built = ContextBuilder(max_context_tokens=2000).build(chunks, answer_language=Language.EN)

    assert built.truncated
    assert len(built.sources) < len(chunks)
