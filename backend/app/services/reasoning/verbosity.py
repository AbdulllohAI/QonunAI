"""How long the answer should be, and how the user says so.

People signal length in one word — *batafsil*, *qisqaroq*, *short* — and expect
it to stick. Two behaviours follow from that, and both matter:

**A bare directive refers to the previous answer.** "batafsil" on its own is not
a question; it means "that last answer, but longer". Retrieved as written it
matches nothing, and the user gets a "no sources found" reply to a message that
was really a formatting request. The previous question has to be carried
forward.

**The preference persists.** Someone who asks for detail once wants detail until
they say otherwise, so it is read back from the conversation rather than reset
each turn. That also avoids a schema migration: the last directive the user
typed *is* the stored state.
"""
from __future__ import annotations

import enum
import re

__all__ = [
    "Verbosity",
    "detect_directive",
    "is_bare_directive",
    "resolve",
    "style_instruction",
]


class Verbosity(enum.Enum):
    BRIEF = "brief"
    NORMAL = "normal"
    DETAILED = "detailed"


#: Requests to expand. Uzbek in both scripts, Russian, English.
_DETAILED = re.compile(
    r"\b("
    r"batafsil|to[’'ʼ`]?liq|kengroq|tushuntir\w*|misol\w*\s+bilan|davom\s+et\w*|"
    r"analiz\s+qil\w*|chuqurroq|ko[’'ʼ`]?proq|"
    r"батафсил|тўлиқ|кенгроқ|тушунтир\w*|мисол\w*\s+билан|давом\s+эт\w*|чуқурроқ|"
    r"подробн\w*|детальн\w*|разверн\w*|поподробнее|объясни\w*|продолж\w*|"
    r"detailed|in\s+detail|explain\s+more|elaborate|expand|longer|long\s+answer|"
    r"more\s+detail\w*|go\s+deeper|continue"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

#: Requests to compress.
_BRIEF = re.compile(
    r"\b("
    r"qisqa\w*|juda\s+qisqa|bir\s+gapda|xulosa\s+qil\w*|"
    r"қисқа\w*|жуда\s+қисқа|бир\s+гапда|хулоса\s+қил\w*|"
    r"кратк\w*|короче|покороче|вкратце|в\s+двух\s+словах|резюм\w*|одним\s+предложением|"
    r"short\w*|brief\w*|summar\w*|tl;?dr|in\s+one\s+sentence|concise"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

#: Words that carry no question of their own, so a message made only of these
#: plus a directive is a directive and nothing else.
_FILLER = re.compile(
    r"\b(iltimos|please|пожалуйста|men|menga|meni|manga|bu|buni|shuni|uni|"
    r"это|мне|мой|моя|it|this|that|the|a|an|me|my|javob\w*|ответ\w*|answer|"
    # Speech verbs: "bir gapda ayting" carries no question, only a length.
    r"ayt\w*|айт\w*|yoz\w*|ёз\w*|ber\w*|бер\w*|qil\w*|қил\w*|"
    r"скажи\w*|напиши\w*|расскаж\w*|дай|сделай|"
    r"say|tell|write|give|make|keep)\b",
    re.IGNORECASE | re.UNICODE,
)

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)

#: A directive plus a real question ("qisqacha aytganda, mehnat shartnomasi
#: nima?") is a question, so bare-directive detection needs a length ceiling as
#: well as a keyword match.
_MAX_BARE_WORDS = 5


def detect_directive(message: str) -> Verbosity | None:
    """The length the user just asked for, or None if they did not ask.

    A message containing both is read as a request to expand: "qisqacha
    tushuntir" ("explain briefly") is far more often a request for explanation
    than for compression, and being too long is the recoverable error.
    """
    if not message:
        return None
    if _DETAILED.search(message):
        return Verbosity.DETAILED
    if _BRIEF.search(message):
        return Verbosity.BRIEF
    return None


def is_bare_directive(message: str) -> bool:
    """True when the message is *only* a length request.

    Such a message has no question in it, so answering it on its own retrieves
    nothing. The caller should re-answer the previous question at the new
    length instead.
    """
    if detect_directive(message) is None:
        return False

    remainder = _DETAILED.sub(" ", message)
    remainder = _BRIEF.sub(" ", remainder)
    remainder = _FILLER.sub(" ", remainder)
    remainder = _PUNCT.sub(" ", remainder)
    return not remainder.split()


def resolve(message: str, remembered: Verbosity | None = None) -> Verbosity:
    """The length to use this turn.

    An explicit request always wins over the remembered preference; otherwise
    the preference carries, because someone who asked for detail once should
    not have to ask again every turn.
    """
    return detect_directive(message) or remembered or Verbosity.NORMAL


def remembered_from_history(user_messages: list[str]) -> Verbosity | None:
    """The most recent explicit preference in the conversation, newest first.

    Reading it back from the messages keeps the preference durable without a
    column to migrate or a cache to invalidate.
    """
    for message in reversed(user_messages):
        directive = detect_directive(message)
        if directive is not None:
            return directive
    return None


_STYLE = {
    Verbosity.BRIEF: (
        "## Length this turn\n\n"
        "The user asked for a short answer. Give the conclusion in one or two "
        "sentences, cited, and stop. Use no section headings at all. Omit "
        "explanation, next steps and legal context entirely — they asked for "
        "less, so leaving things out is the instruction, not a lapse. Citation "
        "tags still apply: brevity never licenses an uncited legal claim."
    ),
    Verbosity.NORMAL: (
        "## Length this turn\n\n"
        "Keep it tight. Two to four sentences of substance for an ordinary "
        "question, and only the sections that carry real content — a short "
        "answer plus a brief explanation is usually the whole of it. Do not "
        "pad to fill the structure; a complete answer that uses two headings "
        "is better than a thin one that uses five."
    ),
    Verbosity.DETAILED: (
        "## Length this turn\n\n"
        "The user asked for depth. Use the full structure: work through the "
        "rule, worked examples, the procedure step by step, exceptions, "
        "deadlines, and what happens if the deadline is missed. Depth means "
        "more legal substance, not more words about the same point — every "
        "added paragraph should carry a fact the shorter answer omitted, and "
        "every legal claim still needs its own citation tag."
    ),
}


def style_instruction(verbosity: Verbosity) -> str:
    return _STYLE[verbosity]
