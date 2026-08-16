"""Uzbek Latin <-> Cyrillic transliteration.

94% of this corpus is Cyrillic, so this layer decides whether a Latin query
can reach the text at all. A defect here is invisible from outside: the query
succeeds, retrieval returns *something*, and only the governing article is
missing.

Every case below is a real corpus word, taken from article headings in the
production corpus, and each group records a bug that was live. Measured over
the full Uzbek heading vocabulary (3,683 distinct words / 20,719 occurrences),
these four fixes together took the unreachable share from **4.05% to 0.14%**.
"""
from __future__ import annotations

import pytest

from app.services.lang.translit import (
    cyrillic_to_latin,
    latin_to_cyrillic,
    script_variants,
)


# ------------------------------------------------------- the glottal stop

# `normalize_apostrophes` folds every apostrophe glyph into U+2018 before the
# mapping table runs, and the table only had rules for U+02BC and U+0027. A
# standalone apostrophe therefore survived transliteration: "ta'til" became
# "та‘тил", which normalises to "татил" and cannot prefix-match "таътил".


@pytest.mark.parametrize(
    "latin,cyrillic",
    [
        ("ta'til", "таътил"),          # leave — 22 occurrences in headings
        ("mehnat ta'tili", "меҳнат таътили"),
        ("da'vo", "даъво"),            # claim — and a glossary term
        ("mas'uliyat", "масъулият"),   # liability — also a glossary term
        ("ma'no", "маъно"),
        ("e'lon", "эълон"),
    ],
)
def test_apostrophe_becomes_the_hard_sign(latin, cyrillic):
    assert latin_to_cyrillic(latin) == cyrillic


def test_every_apostrophe_glyph_reaches_the_same_form():
    """Users and sources type at least six different apostrophes."""
    for glyph in ("'", "’", "ʼ", "‘", "`", "ʻ"):
        assert latin_to_cyrillic(f"ta{glyph}til") == "таътил"


# --------------------------------------------------------- word-initial e

# Uzbek Cyrillic writes initial /e/ as э and reserves е for /ye/. Mapping
# every "e" to "е" made 98 distinct э-initial words unreachable — 532
# occurrences in headings, including "этиш" at 124.


@pytest.mark.parametrize(
    "latin,cyrillic",
    [
        ("etish", "этиш"),
        ("ega", "эга"),
        ("e'tirof", "эътироф"),
        ("eslatma", "эслатма"),
    ],
)
def test_word_initial_e_is_the_broad_e(latin, cyrillic):
    assert latin_to_cyrillic(latin) == cyrillic


def test_non_initial_e_is_unaffected():
    """The rule is positional; "mehnat" must not become "меҳнэт"."""
    assert latin_to_cyrillic("mehnat") == "меҳнат"
    assert latin_to_cyrillic("tekshirish") == "текшириш"
    assert latin_to_cyrillic("kelishuv") == "келишув"


@pytest.mark.parametrize(
    "latin,cyrillic",
    [("yer", "ер"), ("yetkazish", "етказиш"), ("yetim", "етим")],
)
def test_ye_still_maps_to_the_narrow_e(latin, cyrillic):
    assert latin_to_cyrillic(latin) == cyrillic


def test_the_reverse_direction_agrees():
    """ер is yer, not er — otherwise the round trip lands on эр, a different
    word ("husband")."""
    assert cyrillic_to_latin("ер") == "yer"
    assert latin_to_cyrillic(cyrillic_to_latin("ер")) == "ер"
    assert latin_to_cyrillic(cyrillic_to_latin("эр")) == "эр"


# ------------------------------------------------- o‘ and g‘ are letters

# "yo‘l" is y + o‘ + l. Matching the "yo" digraph first split it down the
# middle and produced "ёъл", a string that appears nowhere, so questions about
# yo‘l (road), yo‘qotilgan (lost) and yo‘lovchi (passenger) could not reach
# the corpus.


@pytest.mark.parametrize(
    "latin,cyrillic",
    [
        ("yo'l", "йўл"),
        ("yo'qotilgan", "йўқотилган"),
        ("yo'lovchi", "йўловчи"),
        ("yo'qolgan", "йўқолган"),
    ],
)
def test_o_apostrophe_beats_the_y_digraph(latin, cyrillic):
    assert latin_to_cyrillic(latin) == cyrillic


@pytest.mark.parametrize(
    "latin,cyrillic",
    [("yong'in", "ёнғин"), ("yordam", "ёрдам"), ("yuridik", "юридик")],
)
def test_the_y_digraphs_still_work(latin, cyrillic):
    """Reordering must not cost the ordinary cases: yong‘in resolves g‘ first
    and only then matches "yo"."""
    assert latin_to_cyrillic(latin) == cyrillic


# -------------------------------------------------------- ts loanwords

def test_ts_offers_both_readings():
    """Latin "ts" is ц in the Russian loanwords that fill legal text, and a
    plain t+s elsewhere. A table rule would have to pick one and would corrupt
    the other, so both are offered as candidates in an OR-query."""
    assert "лицензия" in script_variants("litsenziya")
    assert "декларация" in script_variants("deklaratsiya")


def test_a_native_ts_word_keeps_its_correct_form():
    """ko‘rsatsa is кўрсатса. The extra ц candidate is allowed to exist
    alongside it — it simply matches nothing."""
    assert "кўрсатса" in script_variants("ko'rsatsa")


def test_variants_never_lose_the_original():
    for query in ("mehnat shartnomasi", "litsenziya", "yo'l"):
        assert script_variants(query)[0]
        assert len(script_variants(query)) >= 2
