"""Risk scoring.

The model states a risk level in its answer; this module computes one
independently from signals the model cannot fudge (which codes were retrieved,
how strong the retrieval was, whether conflicts were detected) and takes the
**higher** of the two. Under-stating risk is the expensive error here, so the
combination is deliberately asymmetric.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from app.db.models import ActType, Language
from app.services.rag.types import RetrievedChunk


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]

    @classmethod
    def max(cls, a: "RiskLevel", b: "RiskLevel") -> "RiskLevel":
        return a if a.rank >= b.rank else b


# Topics where a wrong answer carries liberty or large financial consequences.
_HIGH_RISK_PATTERNS = [
    (re.compile(r"jinoy|уголовн|criminal|jazo\b|наказан|punish|prison|qamoq", re.I),
     "criminal liability"),
    (re.compile(r"deadline|muddat|срок|limitation|da[’'ʼ]?vo muddati|исковая давность", re.I),
     "procedural deadline"),
    (re.compile(r"soliq|налог\w*|tax\b|penalt|jarima|штраф", re.I),
     "tax or penalty exposure"),
    (re.compile(r"ishdan bo[’'ʼ]?shat|увольнен|dismissal|termination of employment", re.I),
     "employment termination"),
    (re.compile(r"litsenziya|лиценз|licen[cs]e|permit|ruxsatnoma", re.I),
     "licensing requirement"),
    (re.compile(r"bankrot|банкрот|insolven|liquidat|tugatish", re.I),
     "insolvency"),
    (re.compile(r"migration|migratsiya|виза|visa|deport|fuqarolik\s+olish", re.I),
     "immigration status"),
]

_HIGH_RISK_ACT_TYPES = {ActType.CONSTITUTION}


@dataclass(slots=True)
class RiskAssessment:
    level: RiskLevel
    factors: list[str]
    model_stated: RiskLevel | None = None

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "factors": self.factors,
            "model_stated": self.model_stated.value if self.model_stated else None,
        }


_STATED_RE = re.compile(
    r"(?:risk\s*level|xavf\s*darajasi|хавф\s*даражаси|уровень\s*риска)"
    # Everything between the label and the value is noise to skip over, not
    # structure to model: a colon or dash, whitespace/newlines, and markdown
    # emphasis chars — which can show up as ONE run ("**Label** HIGH") or as
    # TWO separate runs closing the label and opening the value's own span
    # ("**Label**\n\n`HIGH`", "**Label**\n\n**HIGH**") depending on how the
    # model chose to format it. A single `\**` only ever matched the first
    # run and silently failed the second, which meant a stated line in that
    # (common, model-preferred) style went undetected — read as "absent" and
    # given a second, duplicate section by ensure_stated_risk right under a
    # real one it never actually failed to write.
    r"[\s`*:\-–]*"
    r"(LOW|MEDIUM|HIGH|PAST|OʻRTA|O‘RTA|ORTA|YUQORI|НИЗКИЙ|СРЕДНИЙ|ВЫСОКИЙ|ПАСТ|ЎРТА|ЮҚОРИ)",
    re.IGNORECASE,
)

_RISK_LABEL_BY_LANG: dict[Language, str] = {
    Language.EN: "Risk level",
    Language.UZ_LATN: "Xavf darajasi",
    Language.UZ_CYRL: "Хавф даражаси",
    Language.RU: "Уровень риска",
}

_STATED_MAP = {
    "low": RiskLevel.LOW, "past": RiskLevel.LOW, "низкий": RiskLevel.LOW, "паст": RiskLevel.LOW,
    "medium": RiskLevel.MEDIUM, "oʻrta": RiskLevel.MEDIUM, "o‘rta": RiskLevel.MEDIUM,
    "orta": RiskLevel.MEDIUM, "средний": RiskLevel.MEDIUM, "ўрта": RiskLevel.MEDIUM,
    "high": RiskLevel.HIGH, "yuqori": RiskLevel.HIGH, "высокий": RiskLevel.HIGH,
    "юқори": RiskLevel.HIGH,
}


def parse_stated_risk(answer: str) -> RiskLevel | None:
    match = _STATED_RE.search(answer)
    if not match:
        return None
    return _STATED_MAP.get(match.group(1).lower())


def rewrite_stated_risk(answer: str, final: RiskLevel) -> str:
    """Rewrite the model's own inline risk-level token to the final, reconciled
    level, so the answer body and the risk badge never visibly disagree.

    `assess()` takes the *higher* of the model's self-reported level and the
    rule-based one — correct policy, but left unapplied it produces a body
    that still reads "Risk level: MEDIUM" under a badge that says HIGH: the
    same answer contradicting itself. The system prompt fixes the label to
    the literal English tokens LOW/MEDIUM/HIGH regardless of answer language,
    so only that token needs replacing — the surrounding label word and
    justification sentence (already in the answer's own language) are left
    untouched.
    """
    match = _STATED_RE.search(answer)
    if not match:
        return answer
    start, end = match.span(1)
    return answer[:start] + final.value.upper() + answer[end:]


def ensure_stated_risk(answer: str, assessment: RiskAssessment, lang: Language) -> str:
    """Guarantee a risk-level line is present and matches `assessment.level`.

    The system prompt *asks* every mode for a risk-level section, but asking
    is not enforcement — smaller/faster models in particular sometimes drop a
    trailing required section under time or length pressure, same failure
    mode a prompt-only citation rule would have if the validator didn't back
    it mechanically. If the model's own line is present, its token is
    reconciled to the final level (`rewrite_stated_risk`); if it's missing
    entirely, one is synthesised from `assessment` — the same data already
    driving the risk badge, so body and badge can never silently diverge
    either way.

    The synthesised line's label is localised to the answer's language (to
    match the model's own convention); the value stays the literal English
    token, same as the model is instructed to use; the justification is
    built from `assessment.factors`, which are deterministic English strings
    not localised per-language — an acceptable seam for a fallback path that,
    by definition, only fires when the model didn't supply its own (already
    localised) justification.
    """
    if _STATED_RE.search(answer):
        return rewrite_stated_risk(answer, assessment.level)

    label = _RISK_LABEL_BY_LANG.get(lang, _RISK_LABEL_BY_LANG[Language.EN])
    justification = "; ".join(assessment.factors) or "Assessed from the retrieved provisions."
    line = f"\n\n**{label}** {assessment.level.value.upper()} — {justification}"
    return answer.rstrip() + line


def assess(
    *,
    question: str,
    answer: str,
    chunks: list[RetrievedChunk],
    conflict_count: int = 0,
    context_truncated: bool = False,
) -> RiskAssessment:
    factors: list[str] = []
    level = RiskLevel.LOW

    haystack = f"{question}\n{answer}"
    for pattern, label in _HIGH_RISK_PATTERNS:
        if pattern.search(haystack):
            factors.append(f"Subject matter involves {label}.")
            level = RiskLevel.HIGH

    # --- retrieval-quality signals ------------------------------------------
    if not chunks:
        factors.append("No legal provisions were retrieved to support an answer.")
        level = RiskLevel.HIGH
    else:
        primary = [c for c in chunks if c.via_crossref_from is None]
        best = max((c.score for c in primary), default=0.0)
        if best < 0.45:
            factors.append("Retrieved provisions are only weakly related to the question.")
            level = RiskLevel.max(level, RiskLevel.MEDIUM)
        if len(primary) < 2:
            factors.append("The answer rests on a single retrieved provision.")
            level = RiskLevel.max(level, RiskLevel.MEDIUM)

        if any(c.act_type in _HIGH_RISK_ACT_TYPES for c in chunks):
            factors.append("Constitutional provisions are engaged.")
            level = RiskLevel.max(level, RiskLevel.MEDIUM)

        if any(c.act_type in (ActType.COMMENTARY, ActType.COURT_DECISION) for c in chunks) and not any(
            c.act_type not in (ActType.COMMENTARY, ActType.COURT_DECISION) for c in chunks
        ):
            factors.append("Only non-binding materials (commentary/case law) were retrieved.")
            level = RiskLevel.HIGH

    if conflict_count:
        factors.append(
            f"{conflict_count} potential conflict(s) between provisions of different legal force."
        )
        level = RiskLevel.max(level, RiskLevel.MEDIUM)

    if context_truncated:
        factors.append("Retrieved context exceeded the window and was truncated.")
        level = RiskLevel.max(level, RiskLevel.MEDIUM)

    # --- hedging language in the answer itself ------------------------------
    if re.search(
        r"do(es)? not (address|cover|specify)|topilmadi|не\s+(содержит|регулирует)|insufficient",
        answer,
        re.IGNORECASE,
    ):
        factors.append("The answer reports that the sources do not fully cover the question.")
        level = RiskLevel.max(level, RiskLevel.MEDIUM)

    stated = parse_stated_risk(answer)
    final = RiskLevel.max(level, stated) if stated else level
    if not factors:
        factors.append("Routine question answered directly from the retrieved provisions.")

    return RiskAssessment(level=final, factors=factors, model_stated=stated)
