"""Uzbek Latin <-> Cyrillic transliteration.

LexUZ publishes the same act in both scripts and users type in either, so every
query and every indexed chunk is normalised through here. Getting this wrong
silently halves recall on Uzbek queries.

Order matters: multi-character digraphs must be replaced before single letters,
otherwise "sh" degrades to "с"+"ҳ" instead of "ш".
"""
from __future__ import annotations

import re
import unicodedata

# Latin digraphs first (longest match wins), then singles.
_LAT_TO_CYR: list[tuple[str, str]] = [
    ("ch", "ч"), ("Ch", "Ч"), ("CH", "Ч"),
    ("sh", "ш"), ("Sh", "Ш"), ("SH", "Ш"),
    ("yo", "ё"), ("Yo", "Ё"), ("YO", "Ё"),
    ("yu", "ю"), ("Yu", "Ю"), ("YU", "Ю"),
    ("ya", "я"), ("Ya", "Я"), ("YA", "Я"),
    ("ye", "е"), ("Ye", "Е"), ("YE", "Е"),
    ("ng", "нг"), ("Ng", "Нг"), ("NG", "НГ"),
    # o' / g' with every apostrophe variant seen in the wild
    ("o‘", "ў"), ("O‘", "Ў"), ("o'", "ў"), ("O'", "Ў"), ("oʻ", "ў"), ("Oʻ", "Ў"),
    ("g‘", "ғ"), ("G‘", "Ғ"), ("g'", "ғ"), ("G'", "Ғ"), ("gʻ", "ғ"), ("Gʻ", "Ғ"),
    ("a", "а"), ("A", "А"),
    ("b", "б"), ("B", "Б"),
    ("d", "д"), ("D", "Д"),
    ("e", "е"), ("E", "Е"),
    ("f", "ф"), ("F", "Ф"),
    ("g", "г"), ("G", "Г"),
    ("h", "ҳ"), ("H", "Ҳ"),
    ("i", "и"), ("I", "И"),
    ("j", "ж"), ("J", "Ж"),
    ("k", "к"), ("K", "К"),
    ("l", "л"), ("L", "Л"),
    ("m", "м"), ("M", "М"),
    ("n", "н"), ("N", "Н"),
    ("o", "о"), ("O", "О"),
    ("p", "п"), ("P", "П"),
    ("q", "қ"), ("Q", "Қ"),
    ("r", "р"), ("R", "Р"),
    ("s", "с"), ("S", "С"),
    ("t", "т"), ("T", "Т"),
    ("u", "у"), ("U", "У"),
    ("v", "в"), ("V", "В"),
    ("x", "х"), ("X", "Х"),
    ("y", "й"), ("Y", "Й"),
    ("z", "з"), ("Z", "З"),
    ("ʼ", "ъ"), ("'", "ъ"),
]

_CYR_TO_LAT: list[tuple[str, str]] = [
    ("ў", "o‘"), ("Ў", "O‘"),
    ("ғ", "g‘"), ("Ғ", "G‘"),
    ("ч", "ch"), ("Ч", "Ch"),
    ("ш", "sh"), ("Ш", "Sh"),
    ("щ", "sh"), ("Щ", "Sh"),
    ("ё", "yo"), ("Ё", "Yo"),
    ("ю", "yu"), ("Ю", "Yu"),
    ("я", "ya"), ("Я", "Ya"),
    ("ц", "ts"), ("Ц", "Ts"),
    ("а", "a"), ("А", "A"),
    ("б", "b"), ("Б", "B"),
    ("в", "v"), ("В", "V"),
    ("г", "g"), ("Г", "G"),
    ("д", "d"), ("Д", "D"),
    ("е", "e"), ("Е", "E"),
    ("ж", "j"), ("Ж", "J"),
    ("з", "z"), ("З", "Z"),
    ("и", "i"), ("И", "I"),
    ("й", "y"), ("Й", "Y"),
    ("к", "k"), ("К", "K"),
    ("қ", "q"), ("Қ", "Q"),
    ("л", "l"), ("Л", "L"),
    ("м", "m"), ("М", "M"),
    ("н", "n"), ("Н", "N"),
    ("о", "o"), ("О", "O"),
    ("п", "p"), ("П", "P"),
    ("р", "r"), ("Р", "R"),
    ("с", "s"), ("С", "S"),
    ("т", "t"), ("Т", "T"),
    ("у", "u"), ("У", "U"),
    ("ф", "f"), ("Ф", "F"),
    ("х", "x"), ("Х", "X"),
    ("ҳ", "h"), ("Ҳ", "H"),
    ("ъ", "ʼ"), ("Ъ", "ʼ"),
    ("ь", ""), ("Ь", ""),
    ("ы", "i"), ("Ы", "I"),
    ("э", "e"), ("Э", "E"),
]

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# Cyrillic letters that exist in Uzbek but not Russian — a strong UZ signal.
_UZ_ONLY_CYRILLIC = set("ўқғҳЎҚҒҲ")
# Russian-only letters — a strong RU signal.
_RU_ONLY_CYRILLIC = set("щыэьъЩЫЭЬЪ")


def _apply(text: str, table: list[tuple[str, str]]) -> str:
    for src, dst in table:
        text = text.replace(src, dst)
    return text


def latin_to_cyrillic(text: str) -> str:
    return _apply(normalize_apostrophes(text), _LAT_TO_CYR)


def cyrillic_to_latin(text: str) -> str:
    return _apply(text, _CYR_TO_LAT)


def normalize_apostrophes(text: str) -> str:
    """Collapse the many apostrophe glyphs used for oʻ/gʻ into U+2018."""
    for variant in ("`", "´", "ʹ", "ʻ", "’", "'"):
        text = text.replace(variant, "‘")
    return text


def normalize(text: str) -> str:
    """NFC + apostrophe normalisation + whitespace collapse."""
    text = unicodedata.normalize("NFC", text)
    text = normalize_apostrophes(text)
    return re.sub(r"[ \t ]+", " ", text).strip()


def script_of(text: str) -> str:
    """'cyrl' | 'latn' | 'mixed' | 'unknown'."""
    cyr = len(_CYRILLIC_RE.findall(text))
    lat = len(_LATIN_RE.findall(text))
    if cyr == 0 and lat == 0:
        return "unknown"
    if cyr and lat:
        ratio = cyr / (cyr + lat)
        if 0.2 < ratio < 0.8:
            return "mixed"
        return "cyrl" if ratio >= 0.8 else "latn"
    return "cyrl" if cyr else "latn"


def cyrillic_flavour(text: str) -> str | None:
    """Distinguish Uzbek Cyrillic from Russian. Returns 'uz' | 'ru' | None."""
    chars = set(text)
    uz_hits = len(chars & _UZ_ONLY_CYRILLIC)
    ru_hits = len(chars & _RU_ONLY_CYRILLIC)
    if uz_hits > ru_hits:
        return "uz"
    if ru_hits > uz_hits:
        return "ru"
    return None


def script_variants(text: str) -> list[str]:
    """Both script forms of a query, de-duplicated — used to widen keyword search."""
    text = normalize(text)
    variants = [text]
    script = script_of(text)
    if script in ("latn", "mixed"):
        variants.append(latin_to_cyrillic(text))
    if script in ("cyrl", "mixed"):
        variants.append(cyrillic_to_latin(text))
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
