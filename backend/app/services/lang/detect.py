"""Language detection tuned for uz-Latn / uz-Cyrl / ru / en.

Off-the-shelf detectors routinely tag Uzbek Latin as Turkish/Azerbaijani and
Uzbek Cyrillic as Russian, so a stopword-and-script heuristic runs first and a
generic detector is only the fallback.
"""
from __future__ import annotations

import re

from app.db.models import Language
from app.services.lang.translit import cyrillic_flavour, normalize, script_of

# High-frequency function words + legal vocabulary that rarely cross languages.
_UZ_MARKERS = {
    "va", "uchun", "bilan", "boʻyicha", "boyicha", "hisoblanadi", "qonun", "modda",
    "kodeks", "shartnoma", "huquq", "majburiyat", "javobgarlik", "tomonidan",
    "belgilangan", "nazarda", "tutilgan", "oʻzbekiston", "ozbekiston", "respublikasi",
    "qanday", "nima", "qilish", "boʻlsa", "bolsa", "agar", "yoki", "ammo", "lekin",
}
_RU_MARKERS = {
    "и", "в", "на", "по", "для", "статья", "закон", "кодекс", "договор", "право",
    "обязательство", "ответственность", "который", "если", "или", "не", "что",
    "как", "узбекистан", "республики", "лица", "случае", "порядке",
}
_EN_MARKERS = {
    "the", "and", "of", "to", "in", "is", "article", "law", "code", "contract",
    "right", "obligation", "liability", "what", "how", "if", "or", "shall", "must",
}

_WORD_RE = re.compile(r"[\w‘ʼ']+", re.UNICODE)


def _score(tokens: set[str], markers: set[str]) -> int:
    return len(tokens & markers)


_CYRILLIC_CHAR_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")


def _cyrillic_dominant(text: str, threshold: float = 0.55) -> bool:
    """True when Cyrillic clearly outweighs Latin in mixed-script text."""
    cyr = len(_CYRILLIC_CHAR_RE.findall(text))
    lat = len(_LATIN_CHAR_RE.findall(text))
    total = cyr + lat
    return total > 0 and (cyr / total) >= threshold


def detect_language(text: str, default: Language = Language.UZ_LATN) -> Language:
    text = normalize(text)
    if not text:
        return default

    tokens = {t.lower() for t in _WORD_RE.findall(text)}
    script = script_of(text)

    # "mixed" is the normal case for scraped pages, not an edge case: a legal
    # portal wraps Cyrillic statutory text in Latin-script navigation, so a
    # genuinely Cyrillic document routinely lands around 0.6-0.7 Cyrillic and
    # is classed "mixed". Treating that as non-Cyrillic discards a correct
    # cyrillic_flavour signal and mislabels the document as Latin, which then
    # breaks language filtering and FTS dictionary selection downstream.
    if script == "mixed" and _cyrillic_dominant(text):
        script = "cyrl"

    if script == "cyrl":
        flavour = cyrillic_flavour(text)
        if flavour == "uz":
            return Language.UZ_CYRL
        if flavour == "ru":
            return Language.RU
        # Ambiguous Cyrillic — fall back to marker counts.
        return Language.RU if _score(tokens, _RU_MARKERS) >= _score(tokens, _UZ_MARKERS) else Language.UZ_CYRL

    uz, en = _score(tokens, _UZ_MARKERS), _score(tokens, _EN_MARKERS)
    if uz > en:
        return Language.UZ_LATN
    if en > uz:
        return Language.EN

    # No decisive markers: Uzbek-specific orthography is the tiebreaker.
    if re.search(r"[oOgG][‘'ʻ]|\bq|\bx", text):
        return Language.UZ_LATN
    return _fallback_detect(text, default)


def _fallback_detect(text: str, default: Language) -> Language:
    try:
        from langdetect import DetectorFactory, detect  # type: ignore

        DetectorFactory.seed = 0
        code = detect(text)
    except Exception:
        return default
    return {
        "ru": Language.RU,
        "en": Language.EN,
        "uz": Language.UZ_LATN,
        # Frequent Uzbek-Latin misclassifications.
        "tr": Language.UZ_LATN,
        "az": Language.UZ_LATN,
    }.get(code, default)


def target_search_languages(lang: Language) -> list[Language]:
    """Which corpus languages to search for a query in `lang`.

    Uzbek acts are authoritative; Russian translations are official and English
    ones are unofficial. We always search across all of them and let the
    reranker sort it out, but the query language is listed first so ties break
    toward the user's own language.
    """
    ordering = {
        Language.UZ_LATN: [Language.UZ_LATN, Language.UZ_CYRL, Language.RU, Language.EN],
        Language.UZ_CYRL: [Language.UZ_CYRL, Language.UZ_LATN, Language.RU, Language.EN],
        Language.RU: [Language.RU, Language.UZ_LATN, Language.UZ_CYRL, Language.EN],
        Language.EN: [Language.EN, Language.UZ_LATN, Language.RU, Language.UZ_CYRL],
    }
    return ordering[lang]
