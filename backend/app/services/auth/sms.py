"""Sending the SMS.

Kept behind a small interface because the provider is a business decision, not
an architectural one: Eskiz is the usual choice in Uzbekistan, but Play Mobile
and others speak similar HTTP APIs, and swapping should not touch the OTP logic.

With no provider configured this raises rather than pretending to send. A
sign-in flow that silently drops the message leaves the user staring at a code
field forever, which is worse than being told the feature is off.
"""
from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["SmsError", "send_sms", "sms_configured"]

ESKIZ_BASE = "https://notify.eskiz.uz/api"


class SmsError(Exception):
    """The message could not be sent."""


def sms_configured() -> bool:
    return bool(settings.ESKIZ_EMAIL and settings.ESKIZ_PASSWORD)


async def _eskiz_token(client: httpx.AsyncClient) -> str:
    """Eskiz issues a bearer token from the account credentials.

    Fetched per send rather than cached: sign-ins are infrequent, and a cached
    token that has silently expired fails exactly when someone is trying to get
    in. Correctness beats one saved round trip here.
    """
    response = await client.post(
        f"{ESKIZ_BASE}/auth/login",
        data={"email": settings.ESKIZ_EMAIL, "password": settings.ESKIZ_PASSWORD},
    )
    if response.status_code >= 400:
        raise SmsError(f"SMS provider rejected credentials ({response.status_code})")
    token = (response.json().get("data") or {}).get("token")
    if not token:
        raise SmsError("SMS provider returned no token")
    return str(token)


async def send_sms(phone: str, text: str) -> None:
    """Send one message, or raise.

    The message body is never logged — it carries the one-time code.
    """
    if not sms_configured():
        raise SmsError("SMS provider is not configured")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            token = await _eskiz_token(client)
            response = await client.post(
                f"{ESKIZ_BASE}/message/sms/send",
                headers={"Authorization": f"Bearer {token}"},
                data={
                    # Eskiz wants the number without the leading +.
                    "mobile_phone": phone.lstrip("+"),
                    "message": text,
                    "from": settings.ESKIZ_SENDER,
                },
            )
    except httpx.HTTPError as exc:
        raise SmsError(f"Could not reach SMS provider: {exc}") from None

    if response.status_code >= 400:
        raise SmsError(f"SMS provider refused the message ({response.status_code})")

    log.info("sms_sent", extra={"phone": phone[:7] + "***"})
