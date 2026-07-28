"""Citation validation — the enforcement half of "only answer from retrieved sources".

The prompt *asks* the model to cite only supplied tags. This module *checks* it,
and that difference is the whole anti-hallucination story:

* A `[S7]` in the answer when only S1–S5 were supplied is a fabricated citation.
  It is stripped and reported.
* An article number asserted in prose ("Article 412 of the Civil Code") that
  belongs to no retrieved source is flagged as unverified, because that is
  exactly the shape a hallucinated statute takes.
* A tag attributed to the WRONG act ("...as established in the Land Code [S1]"
  where S1 is really the Tax Code) is flagged. This is the subtlest failure of
  the three: every tag resolves, every article number exists, so tag- and
  article-level checks both pass — yet the reader is told the answer rests on a
  law that was never retrieved. It shows up when a weaker model papers over a
  gap in the corpus by renaming a source it did get.
* An answer that makes legal assertions with no citations at all is rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.rag.context_builder import SourceRef

_TAG_RE = re.compile(r"\[(S\d+)\]")
_MULTI_TAG_RE = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)+)\]")

_ARTICLE_CLAIM_RE = re.compile(
    r"(?:article|modda|moddasi|стать[яеий])\w*\s*[№#]?\s*(\d+(?:[-–]\d+)?)"
    r"|(\d+(?:[-–]\d+)?)\s*[-–]\s*modda",
    re.IGNORECASE,
)

# --------------------------------------------------------------- act identity
#
# Maps a written act name in any of the corpus scripts (Uzbek Latin, Uzbek
# Cyrillic, Russian, English) onto a canonical key, so a claim made in the
# answer can be compared with the `law_name` of the source it cites.
#
# ORDER MATTERS: procedural codes are checked before their substantive
# namesakes, otherwise "Fuqarolik protsessual kodeksi" matches "civil" first
# and every procedural citation looks mislabelled.
_ACT_KEYS: list[tuple[str, re.Pattern[str]]] = [
    ("civil_procedure", re.compile(
        r"fuqarolik\s+(protsessual|prots)|фуқаролик\s+процессуал"
        r"|гражданск\w*\s+процессуальн|civil\s+procedur", re.I)),
    ("criminal_procedure", re.compile(
        r"jinoyat[-\s]*protsessual|жиноят[-\s]*процессуал"
        r"|уголовно[-\s]*процессуальн|criminal\s+procedur", re.I)),
    ("administrative", re.compile(
        r"ma[’'ʻ‘]?muriy\s+javobgarlik|маъмурий\s+жавобгарлик"
        r"|административн\w*\s+ответственност|administrative\s+liabilit", re.I)),
    ("constitution", re.compile(r"konstitutsiya|конституц|constitution", re.I)),
    ("civil", re.compile(r"fuqarolik\s+kodeks|фуқаролик\s+кодекс|гражданск\w*\s+кодекс|civil\s+code", re.I)),
    ("criminal", re.compile(r"jinoyat\s+kodeks|жиноят\s+кодекс|уголовн\w*\s+кодекс|criminal\s+code", re.I)),
    ("labour", re.compile(r"mehnat\s+kodeks|меҳнат\s+кодекс|трудов\w*\s+кодекс|labou?r\s+code", re.I)),
    ("tax", re.compile(r"soliq\s+kodeks|солиқ\s+кодекс|налогов\w*\s+кодекс|tax\s+code", re.I)),
    ("family", re.compile(r"oila\s+kodeks|оила\s+кодекс|семейн\w*\s+кодекс|family\s+code", re.I)),
    ("land", re.compile(r"yer\s+kodeks|ер\s+кодекс|земельн\w*\s+кодекс|land\s+code", re.I)),
    ("housing", re.compile(r"uy-joy\s+kodeks|уй-жой\s+кодекс|жилищн\w*\s+кодекс|housing\s+code", re.I)),
    ("customs", re.compile(r"bojxona\s+kodeks|божхона\s+кодекс|таможенн\w*\s+кодекс|customs\s+code", re.I)),
    ("budget", re.compile(r"byudjet\s+kodeks|бюджет\w*\s+кодекс|budget\s+code", re.I)),
    ("urban", re.compile(r"shaharsozlik|шаҳарсозлик|градостроительн|urban\s+planning", re.I)),
]

#: How far back from a tag to look for the act name it is attributed to.
_ATTRIBUTION_WINDOW = 90


def act_key(name: str | None) -> str | None:
    """Canonical key for an act name written in any supported script."""
    if not name:
        return None
    for key, pattern in _ACT_KEYS:
        if pattern.search(name):
            return key
    return None


@dataclass(slots=True)
class MislabelledCitation:
    """A tag attributed in prose to an act other than the one it points at."""

    tag: str
    claimed: str
    actual: str

    def to_dict(self) -> dict:
        return {"tag": self.tag, "claimed": self.claimed, "actual": self.actual}


@dataclass(slots=True)
class ValidationResult:
    text: str
    used_tags: list[str] = field(default_factory=list)
    invalid_tags: list[str] = field(default_factory=list)
    unverified_articles: list[str] = field(default_factory=list)
    mislabelled: list[MislabelledCitation] = field(default_factory=list)
    uncited_acts: list[str] = field(default_factory=list)
    has_citations: bool = True
    rejected: bool = False
    reason: str | None = None

    @property
    def is_clean(self) -> bool:
        return (
            not self.invalid_tags
            and not self.unverified_articles
            and not self.mislabelled
            and not self.uncited_acts
            and self.has_citations
        )

    def to_dict(self) -> dict:
        return {
            "used_tags": self.used_tags,
            "invalid_tags": self.invalid_tags,
            "unverified_articles": self.unverified_articles,
            "mislabelled": [m.to_dict() for m in self.mislabelled],
            "uncited_acts": self.uncited_acts,
            "has_citations": self.has_citations,
            "clean": self.is_clean,
        }


def _normalise_multi_tags(text: str) -> str:
    """`[S1, S2]` -> `[S1][S2]` so a single regex pass finds every tag."""
    return _MULTI_TAG_RE.sub(
        lambda m: "".join(f"[{t.strip()}]" for t in m.group(1).split(",")), text
    )


def _check_act_attribution(
    text: str, sources: list[SourceRef]
) -> tuple[list[MislabelledCitation], list[str]]:
    """Verify the act each tag is attributed to matches the act it points at.

    Returns (mislabelled, uncited_acts):

    * `mislabelled` — the answer names act X immediately before `[Sn]`, but Sn
      belongs to act Y. Neither the tag check nor the article check catches
      this, because both the tag and the number are genuine.
    * `uncited_acts` — the answer names an act that no retrieved source belongs
      to at all. That is a claim to have consulted law that is not in the
      corpus, which for a citation-grounded system is a false statement of
      provenance even when the surrounding prose happens to be accurate.
    """
    key_by_tag = {s.tag: act_key(s.chunk.law_name) for s in sources}
    retrieved_keys = {k for k in key_by_tag.values() if k}

    mislabelled: list[MislabelledCitation] = []
    seen: set[tuple[str, str]] = set()

    for match in _TAG_RE.finditer(text):
        tag = match.group(1)
        actual = key_by_tag.get(tag)
        if actual is None:
            continue  # unknown/fabricated tag — handled by the tag check

        window = text[max(0, match.start() - _ATTRIBUTION_WINDOW) : match.start()]
        # Nearest act mention wins: take the match closest to the tag.
        claimed = None
        best = -1
        for key, pattern in _ACT_KEYS:
            for m in pattern.finditer(window):
                if m.start() > best:
                    best, claimed = m.start(), key

        if claimed and claimed != actual and (tag, claimed) not in seen:
            seen.add((tag, claimed))
            mislabelled.append(
                MislabelledCitation(tag=tag, claimed=claimed, actual=actual)
            )

    # Acts named anywhere in the answer that nothing retrieved actually is.
    uncited: list[str] = []
    for key, pattern in _ACT_KEYS:
        if key in retrieved_keys or key in uncited:
            continue
        if pattern.search(text):
            uncited.append(key)

    return mislabelled, uncited


def validate(
    answer: str,
    sources: list[SourceRef],
    *,
    strict_articles: bool = True,
) -> ValidationResult:
    text = _normalise_multi_tags(answer)
    valid_tags = {s.tag for s in sources}

    found = _TAG_RE.findall(text)
    used = [t for t in dict.fromkeys(found) if t in valid_tags]
    invalid = [t for t in dict.fromkeys(found) if t not in valid_tags]

    # Remove fabricated tags rather than showing them to the user as if real.
    for tag in invalid:
        text = text.replace(f"[{tag}]", "")
    text = re.sub(r"[ \t]{2,}", " ", text)

    unverified: list[str] = []
    if strict_articles:
        known = {s.chunk.article_number for s in sources if s.chunk.article_number}
        # Normalise en-dashes so "54–1" and "54-1" compare equal.
        known_norm = {a.replace("–", "-") for a in known}
        for match in _ARTICLE_CLAIM_RE.finditer(text):
            num = (match.group(1) or match.group(2) or "").replace("–", "-")
            if num and num not in known_norm and num not in unverified:
                unverified.append(num)

    mislabelled, uncited_acts = _check_act_attribution(text, sources)

    has_citations = bool(used)

    result = ValidationResult(
        text=text.strip(),
        used_tags=used,
        invalid_tags=invalid,
        unverified_articles=unverified,
        mislabelled=mislabelled,
        uncited_acts=uncited_acts,
        has_citations=has_citations,
    )

    # An answer with substantive legal content but zero citations is exactly the
    # failure mode this system exists to prevent.
    if not has_citations and len(text.strip()) > 400 and sources:
        result.rejected = True
        result.reason = "answer_without_citations"

    return result


def build_warning(result: ValidationResult, language_code: str = "en") -> str | None:
    """Human-readable warning appended to the answer when validation is not clean."""
    if result.is_clean:
        return None
    parts: list[str] = []
    if result.invalid_tags:
        parts.append(
            f"{len(result.invalid_tags)} citation(s) referenced sources that were not "
            f"retrieved and have been removed."
        )
    if result.unverified_articles:
        listed = ", ".join(result.unverified_articles[:6])
        parts.append(
            f"Article number(s) {listed} were mentioned but do not appear in the "
            f"retrieved provisions — verify them directly against lex.uz before relying "
            f"on them."
        )
    if result.mislabelled:
        detail = "; ".join(
            f"{m.tag} is attributed to the {_ACT_LABELS.get(m.claimed, m.claimed)} "
            f"but belongs to the {_ACT_LABELS.get(m.actual, m.actual)}"
            for m in result.mislabelled[:4]
        )
        parts.append(
            f"Citation(s) attributed to the wrong act — {detail}. Treat the act names "
            f"in this answer as unreliable and check the Sources panel, which shows "
            f"what was actually retrieved."
        )
    if result.uncited_acts:
        listed = ", ".join(_ACT_LABELS.get(k, k) for k in result.uncited_acts[:4])
        parts.append(
            f"The answer refers to the {listed}, which is not present in the retrieved "
            f"provisions — nothing here is grounded in that act."
        )
    if not result.has_citations:
        parts.append("This answer contains no verifiable citation to a retrieved provision.")
    return " ".join(parts) if parts else None


_ACT_LABELS: dict[str, str] = {
    "constitution": "Constitution",
    "civil": "Civil Code",
    "civil_procedure": "Civil Procedure Code",
    "criminal": "Criminal Code",
    "criminal_procedure": "Criminal Procedure Code",
    "administrative": "Code of Administrative Liability",
    "labour": "Labour Code",
    "tax": "Tax Code",
    "family": "Family Code",
    "land": "Land Code",
    "housing": "Housing Code",
    "customs": "Customs Code",
    "budget": "Budget Code",
    "urban": "Urban Planning Code",
}


def used_source_dicts(result: ValidationResult, sources: list[SourceRef]) -> list[dict]:
    """Citation payload for the API, limited to sources the answer actually used."""
    by_tag = {s.tag: s for s in sources}
    cited = [by_tag[t].to_citation_dict() for t in result.used_tags if t in by_tag]
    # Cross-referenced supporting sources are included even when uncited, so the
    # UI can show what the model was given.
    supporting = [
        s.to_citation_dict()
        for s in sources
        if s.tag not in result.used_tags and s.chunk.via_crossref_from
    ]
    return cited + supporting
