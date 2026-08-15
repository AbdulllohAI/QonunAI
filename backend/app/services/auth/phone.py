"""Phone sign-in by one-time SMS code.

Unlike Telegram and Google, nobody else vouches for the user here — we send a
code and trust whoever reads it. That puts the whole security burden on four
things, and each is cheap to get wrong:

**The code is never stored.** Only a hash. A database leak should not be a
working login for every pending sign-in, and verification only ever needs to
compare.

**Attempts are capped.** Six digits is a million combinations, which a script
walks in minutes. The cap is what makes a short code safe, not its length.

**Codes expire quickly.** A code read off a lock screen weeks later must be
worthless.

**Requests are throttled per number.** Every SMS costs money and lands on
somebody's phone. Without a cooldown this endpoint is both a billing hole and a
way to harass a stranger.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import PhoneOtp

log = get_logger(__name__)

__all__ = ["PhoneAuthError", "normalise_phone", "request_code", "verify_code"]

CODE_LENGTH = 6
CODE_TTL_SECONDS = 5 * 60
MAX_ATTEMPTS = 5
#: Minimum gap between codes for one number.
RESEND_COOLDOWN_SECONDS = 60

#: Uzbek mobile numbers are +998 followed by nine digits.
_UZ_PATTERN = re.compile(r"^\+998\d{9}$")


class PhoneAuthError(Exception):
    """The request or the code is not acceptable."""


@dataclass(frozen=True, slots=True)
class IssuedCode:
    phone: str
    code: str
    expires_at: datetime


def normalise_phone(raw: str) -> str:
    """Reduce any way of writing a number to one E.164 form.

    `90 123 45 67`, `+998 90 123-45-67` and `998901234567` are the same number,
    and without this they would become three accounts — the third of which
    cannot be reached by the person who owns the first.
    """
    if not raw:
        raise PhoneAuthError("Telefon raqami koʻrsatilmagan")

    digits = re.sub(r"\D", "", raw)

    # Local nine-digit form, and the 8-prefixed form people still write.
    if len(digits) == 9:
        digits = "998" + digits
    elif len(digits) == 12 and digits.startswith("998"):
        pass
    elif len(digits) == 13 and digits.startswith("8998"):
        digits = digits[1:]
    else:
        raise PhoneAuthError("Telefon raqami notoʻgʻri")

    phone = "+" + digits
    if not _UZ_PATTERN.match(phone):
        raise PhoneAuthError("Telefon raqami notoʻgʻri")
    return phone


def _hash(phone: str, code: str) -> str:
    """Hash the code, salted with the number and the app secret.

    Salting with the phone stops one leaked hash from being replayed against a
    different number, and SECRET_KEY means a stolen table alone is not enough
    to precompute all million codes.
    """
    return hashlib.sha256(f"{settings.SECRET_KEY}:{phone}:{code}".encode()).hexdigest()


async def request_code(session: AsyncSession, raw_phone: str) -> IssuedCode:
    """Issue a code for a number, subject to the resend cooldown."""
    phone = normalise_phone(raw_phone)
    now = datetime.now(timezone.utc)

    recent = (
        await session.execute(
            select(PhoneOtp)
            .where(PhoneOtp.phone == phone, PhoneOtp.consumed.is_(False))
            .order_by(PhoneOtp.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if recent is not None:
        age = (now - recent.created_at).total_seconds()
        if age < RESEND_COOLDOWN_SECONDS:
            wait = int(RESEND_COOLDOWN_SECONDS - age)
            raise PhoneAuthError(f"Yangi kod {wait} soniyadan keyin soʻralishi mumkin")

    # secrets, not random: this is a credential, and `random` is seeded
    # predictably enough to be guessed.
    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    expires_at = now + timedelta(seconds=CODE_TTL_SECONDS)

    session.add(
        PhoneOtp(phone=phone, code_hash=_hash(phone, code), expires_at=expires_at)
    )
    await session.commit()

    # The code is never logged. A code in the logs is a credential in the logs.
    log.info("phone_otp_issued", extra={"phone": phone[:7] + "***"})
    return IssuedCode(phone=phone, code=code, expires_at=expires_at)


async def verify_code(session: AsyncSession, raw_phone: str, code: str) -> str:
    """Check a code and return the normalised phone it authenticates.

    Consumes the code on success so it cannot be replayed, and counts failures
    so it cannot be guessed.
    """
    phone = normalise_phone(raw_phone)
    now = datetime.now(timezone.utc)

    otp = (
        await session.execute(
            select(PhoneOtp)
            .where(PhoneOtp.phone == phone, PhoneOtp.consumed.is_(False))
            .order_by(PhoneOtp.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if otp is None:
        raise PhoneAuthError("Kod soʻralmagan yoki allaqachon ishlatilgan")
    if otp.expires_at <= now:
        raise PhoneAuthError("Kod muddati tugagan, yangisini soʻrang")
    if otp.attempts >= MAX_ATTEMPTS:
        raise PhoneAuthError("Juda koʻp urinish. Yangi kod soʻrang")

    if not hmac.compare_digest(otp.code_hash, _hash(phone, str(code).strip())):
        # Count the failure before returning, or the cap never bites.
        otp.attempts += 1
        await session.commit()
        raise PhoneAuthError("Kod notoʻgʻri")

    otp.consumed = True
    await session.commit()
    return phone
