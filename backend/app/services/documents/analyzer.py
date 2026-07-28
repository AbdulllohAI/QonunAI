"""Contract / legal document analysis against Uzbek law.

Structure of the analysis:

1. Extract text (HTML/PDF/DOCX, OCR when scanned).
2. Segment into clauses — retrieval is run *per clause topic*, not once for the
   whole document, because a 30-page contract embedded as one vector retrieves
   nothing specific.
3. Screen for clause types that Uzbek law treats as mandatory or non-waivable.
4. Run the reasoning engine in `document_analysis` mode with the union of
   retrieved provisions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ActType, Language
from app.services.ingestion.parsers import parse_document
from app.services.lang.detect import detect_language
from app.services.lang.translit import normalize
from app.services.reasoning.engine import LegalAnswer, reasoning_engine

log = get_logger(__name__)

# Clause topics we always try to retrieve law for, because their absence or
# mis-drafting is what actually goes wrong in Uzbek contracts.
_CLAUSE_PROBES: list[tuple[str, str]] = [
    ("subject_matter", "shartnoma predmeti muhim shartlari предмет договора существенные условия"),
    ("price", "shartnoma narxi to'lov tartibi цена договора порядок оплаты"),
    ("term", "shartnoma muddati amal qilish срок действия договора"),
    ("termination", "shartnomani bekor qilish bir tomonlama расторжение договора"),
    ("liability", "javobgarlik neustoyka penya ответственность неустойка"),
    ("force_majeure", "yengib bo'lmas kuch fors-major непреодолимая сила"),
    ("dispute", "nizolarni hal qilish sud tartibi разрешение споров подсудность"),
    ("governing_law", "qo'llaniladigan huquq amal qilinadigan qonunchilik применимое право"),
    ("confidentiality", "maxfiylik tijorat siri конфиденциальность коммерческая тайна"),
]

# Red flags: patterns that are frequently unlawful or unenforceable in Uzbekistan.
_RED_FLAGS: list[tuple[str, str, str]] = [
    (
        "waiver_of_statutory_rights",
        r"(voz\s+kechadi|отказыва\w+\s+от\s+прав|waives?\s+(?:all|any)\s+rights?)",
        "Purported waiver of statutory rights. Mandatory (imperative) norms cannot be "
        "excluded by agreement, and such a clause is void to that extent.",
    ),
    (
        "foreign_law_domestic_contract",
        r"(qonunchiligi\s+qo[’'ʻ‘]?llaniladi|регулируется\s+правом|governed\s+by\s+the\s+laws?\s+of)"
        r"(?!.{0,40}(O[’'ʻ‘]?zbekiston|Узбекистан|Uzbekistan))",
        "Foreign governing law selected. Check whether the subject matter is one where "
        "Uzbek law is mandatory (immovable property, employment, public procurement).",
    ),
    (
        "unlimited_liability",
        r"(cheklanmagan\s+javobgarlik|неограниченн\w+\s+ответственност|unlimited\s+liability)",
        "Unlimited liability clause — verify it does not exceed statutory caps and is not "
        "one-sided.",
    ),
    (
        "employment_penalty",
        r"(jarima.{0,40}xodim|xodim\w*.{0,40}jarima"
        r"|штраф.{0,40}работник|работник\w*.{0,40}штраф"
        r"|fine.{0,30}employee|employee.{0,30}fine)",
        "Monetary fines imposed on employees. The Labour Code limits disciplinary sanctions "
        "to those it enumerates; contractual fines on employees are generally unlawful.",
    ),
    (
        "auto_renewal_no_notice",
        r"(avtomatik\s+ravishda\s+uzaytiriladi|автоматически\s+продлевается|automatically\s+renew)",
        "Automatic renewal. Confirm the notice period is workable and mutually available.",
    ),
    (
        "arbitration_abroad",
        r"(xalqaro\s+arbitraj|международн\w+\s+арбитраж|arbitration\s+in\s+(?!Tashkent))",
        "Foreign arbitration seat. Check enforceability of the award in Uzbekistan and any "
        "exclusive jurisdiction rules for this contract type.",
    ),
]

_CLAUSE_SPLIT = re.compile(r"\n(?=\s*(?:\d+(?:\.\d+)*\s*[.)]|[IVXLC]+\s*\.|Статья|Modda|Article))")


@dataclass(slots=True)
class ClauseFlag:
    code: str
    message: str
    excerpt: str


@dataclass(slots=True)
class DocumentAnalysis:
    summary_answer: LegalAnswer
    detected_language: str
    clause_count: int
    heuristic_flags: list[ClauseFlag] = field(default_factory=list)
    probed_topics: list[str] = field(default_factory=list)
    text_length: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        payload = self.summary_answer.to_dict()
        payload["document"] = {
            "detected_language": self.detected_language,
            "clause_count": self.clause_count,
            "text_length": self.text_length,
            "truncated": self.truncated,
            "probed_topics": self.probed_topics,
            "heuristic_flags": [
                {"code": f.code, "message": f.message, "excerpt": f.excerpt}
                for f in self.heuristic_flags
            ],
        }
        return payload


class DocumentAnalyzer:
    #: Beyond this, the document is truncated for the LLM turn. The full text is
    #: still used for clause screening and retrieval probing.
    MAX_ANALYSIS_CHARS = 60_000

    def extract_text(
        self, data: bytes, *, filename: str | None = None, mime_type: str | None = None
    ) -> str:
        parsed = parse_document(data, mime_type=mime_type, filename=filename)
        return normalize(parsed.full_text)

    def segment_clauses(self, text: str) -> list[str]:
        parts = [normalize(p) for p in _CLAUSE_SPLIT.split(text)]
        return [p for p in parts if len(p) > 40]

    def screen(self, text: str) -> list[ClauseFlag]:
        flags: list[ClauseFlag] = []
        for code, pattern, message in _RED_FLAGS:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                start = max(0, match.start() - 80)
                flags.append(
                    ClauseFlag(
                        code=code,
                        message=message,
                        excerpt=normalize(text[start : match.end() + 120]),
                    )
                )
        return flags

    async def analyze(
        self,
        session: AsyncSession,
        *,
        text: str,
        question: str | None = None,
        language: Language | None = None,
        provider: str | None = None,
    ) -> DocumentAnalysis:
        text = normalize(text)
        lang = language or detect_language(text)
        clauses = self.segment_clauses(text)
        flags = self.screen(text)

        # Build a retrieval query that names the clause topics actually present,
        # so the union of retrieved provisions covers the whole document rather
        # than only its opening paragraphs.
        present_topics = [
            name
            for name, probe in _CLAUSE_PROBES
            if _topic_present(text, name)
        ] or [name for name, _ in _CLAUSE_PROBES[:5]]
        probe_text = " ".join(
            probe for name, probe in _CLAUSE_PROBES if name in present_topics
        )

        truncated = len(text) > self.MAX_ANALYSIS_CHARS
        document_text = text[: self.MAX_ANALYSIS_CHARS]

        default_question = (
            "Analyse this document against the law of the Republic of Uzbekistan. "
            "Identify the key clauses, any provisions that conflict with mandatory norms, "
            "the risks, and concrete improvements."
        )
        answer = await reasoning_engine.answer(
            session,
            question=f"{question or default_question}\n\nRelevant topics: {probe_text}",
            mode="document_analysis",
            language=lang,
            document_text=document_text,
            provider=provider,
            top_k=settings.RETRIEVAL_TOP_K_FINAL + 6,
        )

        return DocumentAnalysis(
            summary_answer=answer,
            detected_language=lang.value,
            clause_count=len(clauses),
            heuristic_flags=flags,
            probed_topics=present_topics,
            text_length=len(text),
            truncated=truncated,
        )


def _topic_present(text: str, topic: str) -> bool:
    keywords = {
        "subject_matter": r"predmet|предмет|subject",
        "price": r"narx|to[’'ʻ‘]?lov|цена|оплат|price|payment",
        "term": r"muddat|срок|term\b|duration",
        "termination": r"bekor\s+qil|расторж|terminat",
        "liability": r"javobgarlik|ответственност|liabilit|penalt|neustoyka|неустойк",
        "force_majeure": r"fors[- ]?major|непреодолим|force\s+majeure",
        "dispute": r"nizo|спор|dispute|arbitr|sud\b|суд\b",
        "governing_law": r"qonunchilik|применим\w+\s+прав|governing\s+law",
        "confidentiality": r"maxfiy|конфиденциальн|confidential",
    }
    pattern = keywords.get(topic)
    return bool(pattern and re.search(pattern, text, re.IGNORECASE))


def save_upload(data: bytes, filename: str) -> Path:
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    # Never trust the client-supplied name for a filesystem path.
    safe = re.sub(r"[^\w.\-]", "_", Path(filename).name)[:120]
    path = upload_dir / f"{re.sub(r'[^0-9]', '', str(id(data)))[:8]}_{safe}"
    path.write_bytes(data)
    return path


document_analyzer = DocumentAnalyzer()
