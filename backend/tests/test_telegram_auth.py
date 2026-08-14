"""Telegram login verification.

The browser hands us this payload, so the signature check is the whole security
boundary — anything it accepts becomes a signed-in account. These tests are
written as an attacker would probe it: forge the hash, tamper a field after
signing, replay an old payload, strip the signature entirely.

A test suite that only checks the happy path here would pass against an
implementation that accepts everything.
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.core.config import settings
from app.services.auth import telegram

BOT_TOKEN = "123456:TEST-BOT-TOKEN-abcdefghijklmnop"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", BOT_TOKEN, raising=False)


def sign(payload: dict) -> dict:
    """Produce a genuine payload the way Telegram does."""
    check = "\n".join(
        f"{k}={payload[k]}"
        for k in sorted(payload)
        if k != "hash" and k in telegram.SIGNED_FIELDS
    )
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    return {
        **payload,
        "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(),
    }


def valid_payload(**overrides) -> dict:
    base = {
        "id": 987654321,
        "first_name": "Aziz",
        "last_name": "Karimov",
        "username": "azizk",
        "photo_url": "https://t.me/i/userpic/320/azizk.jpg",
        "auth_date": int(time.time()),
    }
    base.update(overrides)
    return sign(base)


# ------------------------------------------------------------------ genuine

def test_a_genuine_payload_is_accepted():
    profile = telegram.verify(valid_payload())
    assert profile.telegram_id == 987654321
    assert profile.full_name == "Aziz Karimov"
    assert profile.username == "azizk"


def test_missing_last_name_is_fine():
    payload = valid_payload()
    del payload["last_name"]
    profile = telegram.verify(sign({k: v for k, v in payload.items() if k != "hash"}))
    assert profile.full_name == "Aziz"


def test_unknown_extra_fields_do_not_break_the_signature():
    """Telegram adding a field must not lock every honest user out."""
    payload = valid_payload()
    payload["some_future_field"] = "whatever"
    assert telegram.verify(payload).telegram_id == 987654321


# ------------------------------------------------------------------ forgery

def test_a_forged_hash_is_rejected():
    payload = valid_payload()
    payload["hash"] = "0" * 64
    with pytest.raises(telegram.TelegramAuthError):
        telegram.verify(payload)


def test_tampering_after_signing_is_rejected():
    """The whole point: change the id to somebody else's and the hash no
    longer matches."""
    payload = valid_payload()
    payload["id"] = 111111111
    with pytest.raises(telegram.TelegramAuthError):
        telegram.verify(payload)


def test_tampering_with_the_name_is_rejected():
    payload = valid_payload()
    payload["first_name"] = "Someone Else"
    with pytest.raises(telegram.TelegramAuthError):
        telegram.verify(payload)


def test_missing_hash_is_rejected():
    payload = valid_payload()
    del payload["hash"]
    with pytest.raises(telegram.TelegramAuthError):
        telegram.verify(payload)


def test_a_payload_signed_with_another_token_is_rejected():
    """Someone else's bot must not mint accounts on ours."""
    other = "999:SOMEONE-ELSES-BOT"
    fields = {"id": 5, "first_name": "X", "auth_date": int(time.time())}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(other.encode()).digest()
    forged = {**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}

    with pytest.raises(telegram.TelegramAuthError):
        telegram.verify(forged)


def test_the_bot_token_is_hashed_not_used_raw():
    """Telegram's scheme keys the HMAC with SHA256(token). Using the token
    directly validates nothing an attacker cannot reproduce."""
    fields = {"id": 5, "first_name": "X", "auth_date": int(time.time())}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    wrong = {
        **fields,
        "hash": hmac.new(BOT_TOKEN.encode(), check.encode(), hashlib.sha256).hexdigest(),
    }
    with pytest.raises(telegram.TelegramAuthError):
        telegram.verify(wrong)


def test_comparison_is_constant_time():
    import inspect

    assert "compare_digest" in inspect.getsource(telegram.verify)


# -------------------------------------------------------------------- replay

def test_a_stale_login_is_rejected():
    """A payload captured from a log, a URL or a screenshot must stop working."""
    old = valid_payload(auth_date=int(time.time()) - telegram.MAX_AUTH_AGE_SECONDS - 60)
    with pytest.raises(telegram.TelegramAuthError, match="expired"):
        telegram.verify(old)


def test_a_login_from_the_future_is_rejected():
    ahead = valid_payload(auth_date=int(time.time()) + 3600)
    with pytest.raises(telegram.TelegramAuthError, match="future"):
        telegram.verify(ahead)


def test_small_clock_skew_is_tolerated():
    """Rejecting a payload thirty seconds ahead would fail honest users whose
    device clock runs slightly fast."""
    assert telegram.verify(valid_payload(auth_date=int(time.time()) + 30))


def test_malformed_auth_date_is_rejected():
    payload = valid_payload()
    payload["auth_date"] = "not-a-number"
    with pytest.raises(telegram.TelegramAuthError):
        telegram.verify(payload)


# ------------------------------------------------------------- configuration

def test_unconfigured_bot_rejects_everything(monkeypatch):
    """Without a token there is no key, so nothing can be verified — and an
    unverified login must never be treated as valid."""
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "", raising=False)
    with pytest.raises(telegram.TelegramAuthError, match="not configured"):
        telegram.verify(valid_payload())
