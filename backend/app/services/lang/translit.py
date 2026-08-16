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
    # o‘ and g‘ come first, ahead of the y-digraphs.
    #
    # They are single Uzbek letters that happen to be written with two
    # characters, so "yo‘l" is y + o‘ + l. Matching "yo" first splits the word
    # down the middle: it became "ё" + a stranded apostrophe and transliterated
    # to "ёъл" instead of "йўл" — a word that appears nowhere, which is why
    # queries about yo‘l (road), yo‘qotilgan (lost) and yo‘lovchi (passenger)
    # could not reach the Cyrillic corpus at all.
    #
    # Putting them first is safe in the other direction: "yong‘in" still
    # resolves g‘ first and only then matches "yo", giving "ёнғин".
    ("o‘", "ў"), ("O‘", "Ў"), ("o'", "ў"), ("O'", "Ў"), ("oʻ", "ў"), ("Oʻ", "Ў"),
    ("g‘", "ғ"), ("G‘", "Ғ"), ("g'", "ғ"), ("G'", "Ғ"), ("gʻ", "ғ"), ("Gʻ", "Ғ"),
    ("ch", "ч"), ("Ch", "Ч"), ("CH", "Ч"),
    ("sh", "ш"), ("Sh", "Ш"), ("SH", "Ш"),
    ("yo", "ё"), ("Yo", "Ё"), ("YO", "Ё"),
    ("yu", "ю"), ("Yu", "Ю"), ("YU", "Ю"),
    ("ya", "я"), ("Ya", "Я"), ("YA", "Я"),
    ("ye", "е"), ("Ye", "Е"), ("YE", "Е"),
    ("ng", "нг"), ("Ng", "Нг"), ("NG", "НГ"),
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
    # Tutuq belgisi -> hard sign. This must come last: "o‘" and "g‘" are whole
    # letters and are consumed above, so whatever apostrophe survives to here
    # is the glottal stop in "ta'til", "da'vo", "mas'uliyat".
    #
    # U+2018 is the one that actually fires. `normalize_apostrophes` folds
    # every apostrophe glyph into it before this table runs, so the two rules
    # below are dead — they were written for raw input, and normalisation was
    # introduced in front of them without adding the folded form. The result
    # was that a standalone apostrophe survived transliteration entirely:
    # "ta'til" became "та‘тил", which normalises to "татил" and cannot
    # prefix-match the corpus form "таътил". Kept as a deliberate belt: this
    # function is also called directly, without normalisation, in tests.
    ("‘", "ъ"), ("ʼ", "ъ"), ("'", "ъ"),
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


#: A word-initial Latin "e" is Cyrillic "э", not "е".
#:
#: Uzbek Cyrillic writes the initial /e/ as э — этиш, эга, эълон, эътироф —
#: and reserves е for the /ye/ sound, which Latin spells "ye" and the table
#: above already handles. Mapping every "e" to "е" therefore made every
#: э-initial word unreachable from a Latin query: 98 distinct words and 532
#: occurrences in article headings alone, including "этиш" (124), which is
#: about as common as an Uzbek verb gets.
#:
#: This cannot be a table entry because `_apply` is a positionless
#: `str.replace`; the distinction is entirely about where the letter sits.
_WORD_INITIAL_E = re.compile(r"(?<![^\W\d_])(e)", re.IGNORECASE | re.UNICODE)

#: Sentinels, so the table's own "e" rule cannot touch the marked letters.
_E_MARK, _E_MARK_UPPER = "\x01", "\x02"


def latin_to_cyrillic(text: str) -> str:
    text = normalize_apostrophes(text)
    text = _WORD_INITIAL_E.sub(
        lambda m: _E_MARK_UPPER if m.group(1).isupper() else _E_MARK, text
    )
    text = _apply(text, _LAT_TO_CYR)
    return text.replace(_E_MARK, "э").replace(_E_MARK_UPPER, "Э")


#: The mirror of `_WORD_INITIAL_E`: a word-initial Cyrillic "е" is Latin "ye".
#:
#: ер is yer, етказиш is yetkazish. Mapping it to a bare "e" is wrong on its
#: own terms, and once the forward direction reads a word-initial "e" as э it
#: is also unreachable: ер -> er -> эр, a different word.
_WORD_INITIAL_YE = re.compile(r"(?<![^\W\d_])(е)", re.IGNORECASE | re.UNICODE)

_YE_MARK, _YE_MARK_UPPER = "\x03", "\x04"


def cyrillic_to_latin(text: str) -> str:
    text = _WORD_INITIAL_YE.sub(
        lambda m: _YE_MARK_UPPER if m.group(1).isupper() else _YE_MARK, text
    )
    text = _apply(text, _CYR_TO_LAT)
    return text.replace(_YE_MARK, "ye").replace(_YE_MARK_UPPER, "Ye")


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
        cyrillic = latin_to_cyrillic(text)
        variants.append(cyrillic)
        # Latin "ts" is Cyrillic ц in the Russian loanwords that fill legal
        # text — litsenziya/лицензия, deklaratsiya/декларация, protsessual —
        # 230 occurrences in article headings. It cannot be a table rule,
        # because "ts" also arises where a native t meets a native s across a
        # morpheme boundary: ko‘rsatsa is кўрсатса, not кўрсаца. Offering both
        # readings as separate candidates costs a few terms in an OR-query and
        # lets the wrong one simply match nothing.
        if "ts" in text.lower():
            loanword = cyrillic.replace("тс", "ц").replace("Тс", "Ц")
            if loanword != cyrillic:
                variants.append(loanword)
    if script in ("cyrl", "mixed"):
        variants.append(cyrillic_to_latin(text))
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
