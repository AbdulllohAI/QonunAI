"""Intent routing: decide whether a message needs the legal pipeline at all.

Retrieval's own top-3 fallback — meant to rescue a genuine legal question whose
embedding was merely imprecise — cannot distinguish "weakly relevant" from "not
a legal question." Left unguarded, a bare "salom" was observed producing a
confident, fully-cited, HIGH-risk answer about criminal liability for religious
extremism. Catching non-legal intent *before* retrieval is both correct and
free: no retrieval pass, no LLM call, no fabrication to clean up afterwards.

The previous implementation was a single greeting regex, which covered "Salom"
and "Assalomu alaykum" but sent "Qalaysiz?", "Nima gap?" and — most visibly —
"Salom, qalaysiz?" into retrieval, where they came back as a refusal. This
splits intent into four cases and answers the three non-legal ones directly.

Deliberately conservative: anything not clearly conversational is treated as
legal and goes to retrieval. Misrouting a real legal question to a canned reply
is far worse than sending small talk through the pipeline.
"""
from __future__ import annotations

import enum
import re

from app.db.models import Language

__all__ = ["Intent", "classify_intent", "conversational_reply"]


class Intent(enum.Enum):
    GREETING = "greeting"
    SMALLTALK = "smalltalk"
    CAPABILITY = "capability"
    LEGAL = "legal"


#: Trailing punctuation and the several apostrophes Uzbek Latin uses.
_TRIM = r"[\s!.,?…]*"
_APOS = "ʼʻ‘’'`"


def _norm(text: str) -> str:
    """Lowercase, fold apostrophe variants, collapse whitespace."""
    out = text.strip().lower()
    for mark in _APOS:
        out = out.replace(mark, "")
    return " ".join(out.split())


#: A greeting alone. Kept separate from small talk because the natural reply
#: differs: a greeting opens a conversation, "how are you" answers a question.
_GREETING = re.compile(
    r"^(?:"
    r"salom(?:lar)?|assalom(?:u)?\s*alaykum|assalom"
    r"|vaalaykum\s*assalom|valeykum\s*assalom"
    r"|xayrli\s*(?:kun|kech|tong|ertalab)"
    r"|салом(?:лар)?|ассалом(?:у)?\s*алайкум|ассалом"
    r"|хайрли\s*(?:кун|кеч|тонг)"
    r"|привет|здравствуй(?:те)?|добр(?:ый|ое)\s*(?:день|вечер|утро)"
    r"|hi|hello|hey|good\s*(?:morning|afternoon|evening)"
    r")" + _TRIM + r"$",
    re.IGNORECASE | re.UNICODE,
)

#: "How are you / what's up / thanks" — conversational, no legal content.
#: Also matches when prefixed by a greeting ("Salom, qalaysiz?"), which the old
#: regex missed because it anchored the greeting to end-of-string.
_SMALLTALK = re.compile(
    r"^(?:(?:salom|assalom(?:u)?\s*alaykum|салом|привет|hi|hello|hey)[\s,!.-]*)?"
    r"(?:"
    r"qalay(?:siz|san)?|yaxshimisiz|yaxshimisan|ishlar\s*qalay|nima\s*gap"
    r"|nima\s*gaplar|kayfiyat(?:ingiz)?\s*qalay|tinchlikmi"
    r"|қалай(?:сиз|сан)?|яхшимисиз|яхшимисан|ишлар\s*қалай|нима\s*гап"
    r"|нима\s*гаплар|тинчликми"
    r"|как\s*дела|как\s*ты|как\s*вы|что\s*нового|как\s*жизнь"
    r"|how\s*are\s*you|how(?:'|)s\s*it\s*going|what(?:'|)s\s*up"
    r"|rahmat(?:\s*sizga)?|raxmat|раҳмат|рахмат|спасибо|thanks?|thank\s*you"
    r"|ok(?:ay)?|xop|хоп|zor|zo{0,2}r"
    r")" + _TRIM + r"$",
    re.IGNORECASE | re.UNICODE,
)

#: "Who are you / what can you do" — deserves a real description of the product,
#: not a legal-corpus refusal.
_CAPABILITY = re.compile(
    r"^(?:"
    r"(?:sen|siz)\s*kim(?:san|siz)?|kim\s*(?:san|siz)|o?zingni\s*tanishtir"
    r"|nima\s*(?:qila\s*olasan|qila\s*olasiz|ish\s*qilasan)"
    r"|qanday\s*yordam\s*(?:bera\s*olasan|berasiz)"
    r"|sen\s*nima|qonunai\s*nima|bu\s*nima"
    r"|(?:сен|сиз)\s*ким(?:сан|сиз)?|ким\s*(?:сан|сиз)"
    r"|нима\s*қила\s*оласан|қандай\s*ёрдам\s*бера\s*оласан"
    r"|кто\s*ты|кто\s*вы|что\s*ты\s*умеешь|что\s*вы\s*умеете|чем\s*(?:ты|вы)\s*поможешь"
    r"|who\s*are\s*you|what\s*(?:can|do)\s*you\s*do|what\s*is\s*qonunai"
    r")" + _TRIM + r"$",
    re.IGNORECASE | re.UNICODE,
)

#: A message this long is a real question even if it opens with a greeting.
#: "Salom, mehnat shartnomasi haqida savolim bor" must reach retrieval.
_MAX_CONVERSATIONAL_WORDS = 6


def classify_intent(message: str) -> Intent:
    """Route a message. Anything ambiguous is treated as LEGAL."""
    text = _norm(message)
    if not text:
        return Intent.LEGAL

    # A long message is a real question regardless of how it opens.
    if len(text.split()) > _MAX_CONVERSATIONAL_WORDS:
        return Intent.LEGAL

    if _CAPABILITY.match(text):
        return Intent.CAPABILITY
    if _SMALLTALK.match(text):
        return Intent.SMALLTALK
    if _GREETING.match(text):
        return Intent.GREETING
    return Intent.LEGAL


_EXAMPLE: dict[Language, str] = {
    Language.UZ_LATN: "Mehnat shartnomasi qanday shaklda tuziladi?",
    Language.UZ_CYRL: "Меҳнат шартномаси қандай шаклда тузилади?",
    Language.RU: "В какой форме заключается трудовой договор?",
    Language.EN: "What form must a labour contract take?",
}

_GREETING_REPLY: dict[Language, str] = {
    Language.UZ_LATN: (
        "Assalomu alaykum! Men QonunAI — Oʻzbekiston qonunchiligi boʻyicha AI "
        "yordamchisiman. Huquqiy savolingizni bering."
    ),
    Language.UZ_CYRL: (
        "Ассалому алайкум! Мен QonunAI — Ўзбекистон қонунчилиги бўйича AI "
        "ёрдамчисиман. Ҳуқуқий саволингизни беринг."
    ),
    Language.RU: (
        "Здравствуйте! Я QonunAI — ИИ-помощник по законодательству Узбекистана. "
        "Задайте ваш юридический вопрос."
    ),
    Language.EN: (
        "Hello! I'm QonunAI, an AI assistant for the law of the Republic of "
        "Uzbekistan. Ask me your legal question."
    ),
}

_SMALLTALK_REPLY: dict[Language, str] = {
    Language.UZ_LATN: (
        "Rahmat, yaxshiman! Men Oʻzbekiston qonunchiligi boʻyicha savollarga "
        "javob beraman. Masalan:"
    ),
    Language.UZ_CYRL: (
        "Раҳмат, яхшиман! Мен Ўзбекистон қонунчилиги бўйича саволларга жавоб "
        "бераман. Масалан:"
    ),
    Language.RU: (
        "Спасибо, всё хорошо! Я отвечаю на вопросы по законодательству "
        "Узбекистана. Например:"
    ),
    Language.EN: (
        "Thanks, I'm well! I answer questions about the law of Uzbekistan. "
        "For example:"
    ),
}

_CAPABILITY_REPLY: dict[Language, str] = {
    Language.UZ_LATN: (
        "Men QonunAI — Oʻzbekiston qonunchiligi boʻyicha AI yordamchiman.\n\n"
        "• Konstitutsiya va kodekslar boʻyicha savollarga javob beraman\n"
        "• Har bir javobni rasmiy hujjatning aniq moddasiga asoslayman\n"
        "• Har bir iqtibos lex.uz'dagi tegishli moddaga havola qiladi\n"
        "• Oʻzbek (lotin/kirill), rus va ingliz tillarida ishlayman\n\n"
        "Agar bazamda tasdiqlovchi manba boʻlmasa, taxmin qilmayman — "
        "topilmaganini aytaman. Masalan:"
    ),
    Language.UZ_CYRL: (
        "Мен QonunAI — Ўзбекистон қонунчилиги бўйича AI ёрдамчиман.\n\n"
        "• Конституция ва кодекслар бўйича саволларга жавоб бераман\n"
        "• Ҳар бир жавобни расмий ҳужжатнинг аниқ моддасига асослайман\n"
        "• Ҳар бир иқтибос lex.uz'даги тегишли моддага ҳавола қилади\n"
        "• Ўзбек (лотин/кирилл), рус ва инглиз тилларида ишлайман\n\n"
        "Агар базамда тасдиқловчи манба бўлмаса, тахмин қилмайман — "
        "топилмаганини айтаман. Масалан:"
    ),
    Language.RU: (
        "Я QonunAI — ИИ-помощник по законодательству Узбекистана.\n\n"
        "• Отвечаю на вопросы по Конституции и кодексам\n"
        "• Каждый ответ опираю на конкретную статью официального документа\n"
        "• Каждая ссылка ведёт к нужной статье на lex.uz\n"
        "• Работаю на узбекском (латиница/кириллица), русском и английском\n\n"
        "Если в базе нет подтверждающего источника, я не догадываюсь — "
        "прямо говорю, что источник не найден. Например:"
    ),
    Language.EN: (
        "I'm QonunAI, an AI assistant for the law of Uzbekistan.\n\n"
        "• I answer questions on the Constitution and the Codes\n"
        "• Every answer is grounded in a specific article of an official act\n"
        "• Every citation links to that exact article on lex.uz\n"
        "• I work in Uzbek (Latin/Cyrillic), Russian and English\n\n"
        "If my corpus has no supporting source, I don't guess — I say so. "
        "For example:"
    ),
}


def conversational_reply(intent: Intent, language: Language) -> str | None:
    """The canned reply for a non-legal intent, or None for LEGAL.

    Small talk and capability replies end with a worked example, because the
    most useful thing to do with a user who isn't asking a legal question yet
    is show them what a good one looks like.
    """
    if intent is Intent.GREETING:
        return _GREETING_REPLY[language]
    if intent is Intent.SMALLTALK:
        return f'{_SMALLTALK_REPLY[language]}\n\n«{_EXAMPLE[language]}»'
    if intent is Intent.CAPABILITY:
        return f'{_CAPABILITY_REPLY[language]}\n\n«{_EXAMPLE[language]}»'
    return None
