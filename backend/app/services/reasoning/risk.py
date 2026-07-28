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

from app.db.models import ActType
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
    r"(?:risk\s*level|xavf\s*darajasi|уровень\s*риска)\s*[:\-–]?\s*\**\s*"
    r"(LOW|MEDIUM|HIGH|PAST|OʻRTA|O‘RTA|ORTA|YUQORI|НИЗКИЙ|СРЕДНИЙ|ВЫСОКИЙ)",
    re.IGNORECASE,
)

_STATED_MAP = {
    "low": RiskLevel.LOW, "past": RiskLevel.LOW, "низкий": RiskLevel.LOW,
    "medium": RiskLevel.MEDIUM, "oʻrta": RiskLevel.MEDIUM, "o‘rta": RiskLevel.MEDIUM,
    "orta": RiskLevel.MEDIUM, "средний": RiskLevel.MEDIUM,
    "high": RiskLevel.HIGH, "yuqori": RiskLevel.HIGH, "высокий": RiskLevel.HIGH,
}


def parse_stated_risk(answer: str) -> RiskLevel | None:
    match = _STATED_RE.search(answer)
    if not match:
        return None
    return _STATED_MAP.get(match.group(1).lower())


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
