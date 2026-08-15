"""Phone sign-in by SMS code.

Nobody else vouches for the user here — we send a code and trust whoever reads
it — so these tests probe the four things that actually hold it up: the code is
never recoverable from storage, guesses are capped, codes expire and cannot be
replayed, and one number cannot be used to burn SMS credit or harass a stranger.

Number normalisation gets its own section because it is the quiet one: get it
wrong and the same person ends up with three accounts, only one of which they
can reach.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.auth import phone as phone_auth


# ---------------------------------------------------------------- fixtures

class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    def __init__(self, existing=None):
        self._existing = existing
        self.added = []
        self.committed = 0

    async def execute(self, _stmt):
        return FakeResult(self._existing)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1


class FakeOtp:
    def __init__(self, **kw):
        self.id = uuid.uuid4()
        self.phone = kw.get("phone", "+998901234567")
        self.code_hash = kw.get("code_hash", "")
        self.expires_at = kw.get(
            "expires_at", datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        self.attempts = kw.get("attempts", 0)
        self.consumed = kw.get("consumed", False)
        self.created_at = kw.get("created_at", datetime.now(timezone.utc))


# ----------------------------------------------------------- normalisation

@pytest.mark.parametrize(
    "written",
    [
        "+998901234567",
        "998901234567",
        "901234567",
        "90 123 45 67",
        "+998 90 123-45-67",
        "(90) 123 45 67",
        "8998901234567",
    ],
)
def test_every_way_of_writing_one_number_resolves_the_same(written):
    """Otherwise the same person gets several accounts and can only reach one."""
    assert phone_auth.normalise_phone(written) == "+998901234567"


@pytest.mark.parametrize(
    "bad",
    ["", "12345", "+1 202 555 0143", "abcdefghij", "9012345678901234", "+99890123456"],
)
def test_malformed_numbers_are_rejected(bad):
    with pytest.raises(phone_auth.PhoneAuthError):
        phone_auth.normalise_phone(bad)


# ------------------------------------------------------------- code safety

def test_the_code_is_not_recoverable_from_its_hash():
    """A database leak must not be a working login for every pending sign-in."""
    digest = phone_auth._hash("+998901234567", "123456")
    assert "123456" not in digest
    assert len(digest) == 64


def test_the_hash_is_bound_to_the_number():
    """Otherwise a hash lifted for one number is replayable against another."""
    assert phone_auth._hash("+998901234567", "123456") != phone_auth._hash(
        "+998907654321", "123456"
    )


def test_the_hash_is_bound_to_the_app_secret(monkeypatch):
    before = phone_auth._hash("+998901234567", "123456")
    monkeypatch.setattr(settings, "SECRET_KEY", "a-different-secret-key", raising=False)
    assert phone_auth._hash("+998901234567", "123456") != before


def test_codes_come_from_a_cryptographic_source():
    """`random` is seeded predictably enough to guess; this is a credential."""
    import inspect

    source = inspect.getsource(phone_auth.request_code)
    assert "secrets." in source


@pytest.mark.asyncio
async def test_issued_code_is_the_right_shape():
    issued = await phone_auth.request_code(FakeSession(), "901234567")
    assert len(issued.code) == phone_auth.CODE_LENGTH
    assert issued.code.isdigit()
    assert issued.phone == "+998901234567"


@pytest.mark.asyncio
async def test_only_the_hash_is_stored():
    session = FakeSession()
    issued = await phone_auth.request_code(session, "901234567")
    stored = session.added[0]
    assert stored.code_hash != issued.code
    assert issued.code not in stored.code_hash


# ---------------------------------------------------------------- cooldown

@pytest.mark.asyncio
async def test_rapid_resend_is_refused():
    """Every SMS costs money and lands on somebody's phone; without this the
    endpoint is a billing hole and a way to harass a stranger."""
    just_sent = FakeOtp(created_at=datetime.now(timezone.utc) - timedelta(seconds=5))
    with pytest.raises(phone_auth.PhoneAuthError, match="soniyadan"):
        await phone_auth.request_code(FakeSession(just_sent), "901234567")


@pytest.mark.asyncio
async def test_resend_is_allowed_after_the_cooldown():
    old = FakeOtp(
        created_at=datetime.now(timezone.utc)
        - timedelta(seconds=phone_auth.RESEND_COOLDOWN_SECONDS + 5)
    )
    assert await phone_auth.request_code(FakeSession(old), "901234567")


# ------------------------------------------------------------ verification

@pytest.mark.asyncio
async def test_the_right_code_verifies_and_is_consumed():
    phone = "+998901234567"
    otp = FakeOtp(code_hash=phone_auth._hash(phone, "123456"))
    session = FakeSession(otp)

    assert await phone_auth.verify_code(session, "901234567", "123456") == phone
    assert otp.consumed is True, "a consumed code must not be replayable"


@pytest.mark.asyncio
async def test_a_wrong_code_is_counted():
    """Counting before returning is what makes the attempt cap bite."""
    otp = FakeOtp(code_hash=phone_auth._hash("+998901234567", "123456"))
    session = FakeSession(otp)

    with pytest.raises(phone_auth.PhoneAuthError):
        await phone_auth.verify_code(session, "901234567", "000000")
    assert otp.attempts == 1


@pytest.mark.asyncio
async def test_guessing_is_capped():
    """Six digits is a million combinations, which a script walks in minutes."""
    otp = FakeOtp(
        code_hash=phone_auth._hash("+998901234567", "123456"),
        attempts=phone_auth.MAX_ATTEMPTS,
    )
    with pytest.raises(phone_auth.PhoneAuthError, match="urinish"):
        # Even the correct code is refused once the cap is reached.
        await phone_auth.verify_code(FakeSession(otp), "901234567", "123456")


@pytest.mark.asyncio
async def test_an_expired_code_is_refused():
    otp = FakeOtp(
        code_hash=phone_auth._hash("+998901234567", "123456"),
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(phone_auth.PhoneAuthError, match="muddati"):
        await phone_auth.verify_code(FakeSession(otp), "901234567", "123456")


@pytest.mark.asyncio
async def test_verifying_without_requesting_is_refused():
    with pytest.raises(phone_auth.PhoneAuthError):
        await phone_auth.verify_code(FakeSession(None), "901234567", "123456")


@pytest.mark.asyncio
async def test_a_code_for_another_number_does_not_work():
    """The hash is salted with the number, so this fails even if the digits
    happen to be right."""
    otp = FakeOtp(phone="+998901234567", code_hash=phone_auth._hash("+998907654321", "123456"))
    with pytest.raises(phone_auth.PhoneAuthError):
        await phone_auth.verify_code(FakeSession(otp), "901234567", "123456")


def test_comparison_is_constant_time():
    import inspect

    assert "compare_digest" in inspect.getsource(phone_auth.verify_code)


# ---------------------------------------------------------------- provider

def test_sms_is_not_configured_by_default():
    from app.services.auth import sms

    assert sms.sms_configured() is False


@pytest.mark.asyncio
async def test_unconfigured_provider_raises_rather_than_pretending():
    """A silently dropped message leaves the user staring at a code field."""
    from app.services.auth import sms

    with pytest.raises(sms.SmsError, match="not configured"):
        await sms.send_sms("+998901234567", "test")
