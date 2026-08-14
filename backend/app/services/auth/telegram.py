"""Telegram Login Widget verification.

The widget hands the browser a signed payload and the browser hands it to us,
which means the only thing standing between an attacker and an account is this
signature check. Getting it subtly wrong — comparing with `==`, forgetting to
exclude `hash` from the checked string, skipping the freshness check — produces
code that works perfectly for honest users and is trivially forged.

Telegram's scheme:

    data_check_string = "\\n".join(sorted "key=value", excluding hash)
    secret_key        = SHA256(bot_token)
    expected          = HMAC_SHA256(data_check_string, secret_key)

Note the asymmetry that catches people out: the bot token is *hashed* to make
the key, not used as the key directly. Using it raw validates nothing an
attacker cannot reproduce once they know the token format.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from app.core.config import settings

__all__ = ["TelegramAuthError", "TelegramProfile", "verify"]

#: How old a login payload may be. Telegram recommends rejecting stale ones;
#: without this, a payload captured from a URL, a log or a shared screenshot
#: stays a valid credential forever.
MAX_AUTH_AGE_SECONDS = 24 * 60 * 60

#: Fields Telegram signs. Anything else in the payload is ignored rather than
#: folded into the check string, which would make the signature fail for honest
#: users the moment Telegram adds a field.
SIGNED_FIELDS = frozenset(
    {"auth_date", "first_name", "id", "last_name", "photo_url", "username"}
)


class TelegramAuthError(Exception):
    """The payload is not a genuine, fresh Telegram login."""


@dataclass(frozen=True, slots=True)
class TelegramProfile:
    telegram_id: int
    full_name: str | None
    username: str | None
    photo_url: str | None


def _data_check_string(payload: dict[str, str]) -> str:
    return "\n".join(
        f"{key}={payload[key]}"
        for key in sorted(payload)
        # `hash` is the signature itself; including it would be checking a
        # value against a string that contains it.
        if key != "hash" and key in SIGNED_FIELDS
    )


def verify(payload: dict[str, object]) -> TelegramProfile:
    """Validate a login payload and return the profile it attests to.

    Raises TelegramAuthError for anything that is not a genuine, fresh login.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        raise TelegramAuthError("Telegram sign-in is not configured")

    flat = {k: str(v) for k, v in payload.items() if v is not None}

    received = flat.get("hash")
    if not received:
        raise TelegramAuthError("Missing signature")

    secret_key = hashlib.sha256(settings.TELEGRAM_BOT_TOKEN.encode()).digest()
    expected = hmac.new(
        secret_key, _data_check_string(flat).encode("utf-8"), hashlib.sha256
    ).hexdigest()

    # compare_digest, not ==: a byte-by-byte comparison that returns early
    # leaks how much of a forged signature was correct.
    if not hmac.compare_digest(expected, received):
        raise TelegramAuthError("Signature does not match")

    try:
        auth_date = int(flat.get("auth_date", ""))
    except ValueError:
        raise TelegramAuthError("Malformed auth_date") from None

    age = time.time() - auth_date
    if age > MAX_AUTH_AGE_SECONDS:
        raise TelegramAuthError("Login has expired, please sign in again")
    # A little clock skew is normal; a payload minted well in the future is not.
    if age < -300:
        raise TelegramAuthError("Login timestamp is in the future")

    try:
        telegram_id = int(flat["id"])
    except (KeyError, ValueError):
        raise TelegramAuthError("Missing Telegram id") from None

    name = " ".join(
        part for part in (flat.get("first_name"), flat.get("last_name")) if part
    ).strip()

    return TelegramProfile(
        telegram_id=telegram_id,
        full_name=name or None,
        username=flat.get("username"),
        photo_url=flat.get("photo_url"),
    )
