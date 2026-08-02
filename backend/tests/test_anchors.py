"""Unit tests for lex.uz article anchor extraction.

The fixtures are minimal but verbatim: the markup shapes below were copied from
real lex.uz responses (Labour Code uz-Cyrl 6257288, Criminal Code ru 111457),
including the quirk that sub-numbered articles render as space-separated plain
digits (``Статья 57 1 .``) rather than superscript entities.

A regression here produces citations that open the wrong article, which in a
legal tool is worse than producing no link at all — hence the emphasis on the
disambiguation and graceful-degradation cases.
"""
from __future__ import annotations

from app.services.ingestion.anchors import (
    build_deep_link,
    extract_anchors,
    match_anchor,
    verify_anchors,
)

# --- fixtures -------------------------------------------------------------

UZ_TOC = """
<div class="docNabvar__child-link"><p><a class="search-text"
 href="javascript:scrollText('6257549');">1-модда. Ушбу Кодекс билан тартибга солинадиган муносабатлар</a></p></div>
<div class="docNabvar__child-link"><p><a class="search-text"
 href="javascript:scrollText('6259020');">80-модда. Жамоа келишувларининг тушунчаси ва шакли</a></p></div>
<div class="docNavbar__item-link"><a class="stopProp search-text"
 href="javascript:scrollText('6259019');">9-боб. Жамоа келишувлари</a></div>
<p id="6257549">…</p><p id="6259020">…</p><p id="6259019">…</p>
"""

RU_TOC = """
<div><p><a href="javascript:scrollText('156025');">Статья 57. Назначение более мягкого наказания</a></p></div>
<div><p><a href="javascript:scrollText('1723513');">Статья 57 1 . Назначение наказания при деятельном раскаянии виновного в содеянном</a></p></div>
<div><p><a href="javascript:scrollText('5301236');">Статья 57 2 . Назначение наказания по преступлениям, по которым заключено соглашение</a></p></div>
<div><p><a href="javascript:scrollText('149556');">Глава I. Задачи и принципы Уголовного кодекса</a></p></div>
<p id="156025">…</p><p id="1723513">…</p><p id="5301236">…</p><p id="149556">…</p>
"""


# --- extraction -----------------------------------------------------------

def test_extracts_uzbek_articles_and_skips_chapters():
    anchors = extract_anchors(UZ_TOC)
    numbers = {a.article_number for a in anchors}
    assert numbers == {"1", "80"}, "chapter (9-боб) must not be treated as an article"
    by_num = {a.article_number: a for a in anchors}
    assert by_num["80"].anchor_id == "6259020"
    assert by_num["80"].heading.startswith("Жамоа келишувларининг")


def test_extracts_russian_subnumbered_articles_distinctly():
    """57, 57¹ and 57² are legally distinct — collapsing them is the bug."""
    anchors = extract_anchors(RU_TOC)
    by_num = {a.article_number: a.anchor_id for a in anchors}
    assert by_num == {
        "57": "156025",
        "57-1": "1723513",
        "57-2": "5301236",
    }


def test_verify_drops_anchors_with_no_target_element():
    html = RU_TOC.replace('<p id="1723513">…</p>', "")
    kept = verify_anchors(html, extract_anchors(html))
    assert "57-1" not in {a.article_number for a in kept}
    assert "57" in {a.article_number for a in kept}


# --- matching stored chunks back to anchors -------------------------------

def test_match_disambiguates_collapsed_article_numbers_by_heading():
    """The live corpus stores 57, 57¹ and 57² all as "57"."""
    anchors = extract_anchors(RU_TOC)
    cases = [
        ("Назначение более мягкого наказания", "156025"),
        ("Назначение наказания при деятельном раскаянии виновного в содеянном", "1723513"),
        ("Назначение наказания по преступлениям, по которым заключено соглашение", "5301236"),
    ]
    for heading, expected in cases:
        got = match_anchor(anchors, "57", heading)
        assert got is not None and got.anchor_id == expected, heading


def test_match_tolerates_stored_article_prefix_in_heading():
    anchors = extract_anchors(RU_TOC)
    got = match_anchor(anchors, "57", "57-modda. Назначение более мягкого наказания")
    assert got is not None and got.anchor_id == "156025"


def test_match_returns_none_rather_than_guessing():
    """Ambiguous number with an unrecognisable heading must not pick one."""
    anchors = extract_anchors(RU_TOC)
    assert match_anchor(anchors, "57", "completely unrelated text") is None
    assert match_anchor(anchors, "57", None) is None
    assert match_anchor(anchors, "9999", "anything") is None
    assert match_anchor(anchors, None, "anything") is None


def test_match_unambiguous_number_needs_no_heading():
    anchors = extract_anchors(UZ_TOC)
    got = match_anchor(anchors, "80", None)
    assert got is not None and got.anchor_id == "6259020"


# --- link construction ----------------------------------------------------

def test_build_deep_link_appends_fragment():
    assert (
        build_deep_link("https://lex.uz/docs/6257288", "6259020")
        == "https://lex.uz/docs/6257288#6259020"
    )
    assert (
        build_deep_link("https://lex.uz/ru/docs/111457", "156025")
        == "https://lex.uz/ru/docs/111457#156025"
    )


def test_build_deep_link_degrades_gracefully():
    assert build_deep_link("https://lex.uz/docs/111457", None) == "https://lex.uz/docs/111457"
    assert build_deep_link(None, "6259020") is None
    assert build_deep_link(None, None) is None


def test_build_deep_link_replaces_existing_fragment():
    assert (
        build_deep_link("https://lex.uz/docs/6257288#stale", "6259020")
        == "https://lex.uz/docs/6257288#6259020"
    )


def test_build_deep_link_never_rewrites_foreign_urls():
    """Anchor ids are a lex.uz convention; applying them elsewhere is nonsense."""
    assert (
        build_deep_link("https://example.com/docs/1", "6259020")
        == "https://example.com/docs/1"
    )
