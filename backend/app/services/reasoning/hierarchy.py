"""Conflict resolution over the Uzbek hierarchy of normative acts.

The LLM is instructed to apply these rules, but the ordering is also computed
deterministically here so the API can return a structured `conflicts` payload
the UI renders — and so the result does not depend on the model getting the
hierarchy right on any given run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import combinations

from app.db.models import ActType
from app.services.rag.types import RetrievedChunk

# Codes are lex specialis relative to general laws on the same subject.
_SPECIFICITY_BONUS = {
    ActType.CODE: 2,
    ActType.CONSTITUTIONAL_LAW: 1,
}


@dataclass(slots=True)
class ConflictNote:
    higher: str
    lower: str
    rule: str
    explanation: str


@dataclass(slots=True)
class HierarchyAnalysis:
    ordered: list[RetrievedChunk]
    conflicts: list[ConflictNote]
    controlling: RetrievedChunk | None

    def to_dict(self) -> dict:
        return {
            "controlling": self.controlling.citation if self.controlling else None,
            "conflicts": [
                {
                    "higher": c.higher,
                    "lower": c.lower,
                    "rule": c.rule,
                    "explanation": c.explanation,
                }
                for c in self.conflicts
            ],
        }


def _effective_date(chunk: RetrievedChunk) -> date:
    return chunk.last_updated or chunk.date_of_adoption or date.min


def resolve(chunks: list[RetrievedChunk]) -> HierarchyAnalysis:
    """Order by legal force and flag pairs that may be in tension.

    A *potential* conflict is flagged when two provisions of different force
    address the same subject — detected here by act-type divergence within the
    retrieved set. This is a signal for the reader, not an assertion that the
    provisions actually contradict each other; only the text can establish that,
    which is why the explanation is phrased as guidance on which would prevail.
    """
    binding = [c for c in chunks if c.act_type not in (ActType.COMMENTARY, ActType.COURT_DECISION)]
    if not binding:
        return HierarchyAnalysis(ordered=list(chunks), conflicts=[], controlling=None)

    ordered = sorted(
        binding,
        key=lambda c: (
            c.precedence + _SPECIFICITY_BONUS.get(c.act_type, 0),
            _effective_date(c),
            c.score,
        ),
        reverse=True,
    )

    conflicts: list[ConflictNote] = []
    for a, b in combinations(ordered, 2):
        if a.act_id == b.act_id:
            continue
        if a.precedence > b.precedence:
            conflicts.append(
                ConflictNote(
                    higher=a.citation,
                    lower=b.citation,
                    rule="lex superior derogat legi inferiori",
                    explanation=(
                        f"{a.citation} is a {_label(a.act_type)} and {b.citation} is a "
                        f"{_label(b.act_type)}. Where their requirements diverge, "
                        f"{a.citation} prevails and the lower act must be read consistently "
                        f"with it."
                    ),
                )
            )
        elif a.precedence == b.precedence:
            da, db = _effective_date(a), _effective_date(b)
            if da != date.min and db != date.min and da != db:
                later, earlier = (a, b) if da > db else (b, a)
                conflicts.append(
                    ConflictNote(
                        higher=later.citation,
                        lower=earlier.citation,
                        rule="lex posterior derogat legi priori",
                        explanation=(
                            f"Both are of equal legal force. {later.citation} is the later "
                            f"provision ({_effective_date(later).isoformat()} vs "
                            f"{_effective_date(earlier).isoformat()}), so it prevails on any "
                            f"point where they diverge."
                        ),
                    )
                )

    # Only the strongest few are useful; more becomes noise in the UI.
    return HierarchyAnalysis(ordered=ordered, conflicts=conflicts[:5], controlling=ordered[0])


def _label(act_type: ActType) -> str:
    return {
        ActType.CONSTITUTION: "constitutional provision",
        ActType.CONSTITUTIONAL_LAW: "constitutional law",
        ActType.CODE: "code",
        ActType.LAW: "law",
        ActType.PRESIDENTIAL_DECREE: "presidential decree",
        ActType.PRESIDENTIAL_RESOLUTION: "presidential resolution",
        ActType.CABINET_RESOLUTION: "Cabinet of Ministers resolution",
        ActType.MINISTERIAL_ACT: "ministerial act",
        ActType.LOCAL_ACT: "local act",
        ActType.COURT_DECISION: "court decision",
        ActType.COMMENTARY: "commentary",
    }.get(act_type, act_type.value)
