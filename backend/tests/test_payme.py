"""Payme merchant protocol.

These tests are about money, so they aim at the ways money actually goes wrong
rather than at coverage: a retry becoming a second charge, an amount accepted
at whatever was sent, a refund that leaves the subscription running, and a
timing-comparable secret.

The handlers are exercised against a fake session rather than Postgres. What is
being pinned here is the protocol and the state machine — the parts Payme's own
sandbox will check — and those are decided in this module, not in the database.
"""
from __future__ import annotations

import base64
import uuid

import pytest

from app.core.config import settings
from app.db.models import PaymeState
from app.services.billing import payme


# ---------------------------------------------------------------- fixtures

class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self._value if isinstance(self._value, list) else []


class FakeSession:
    """Just enough AsyncSession to drive the handlers."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.added = []
        self.committed = 0
        self.rolled_back = 0

    async def execute(self, _stmt):
        return FakeResult(self.results.pop(0) if self.results else None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


def _auth(key: str) -> str:
    return "Basic " + base64.b64encode(f"Paycom:{key}".encode()).decode()


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "PAYME_KEY", "test-merchant-key", raising=False)
    monkeypatch.setattr(settings, "PRO_PRICE_UZS", 79_000, raising=False)
    monkeypatch.setattr(settings, "PAYME_ACCOUNT_FIELD", "user_id", raising=False)


# --------------------------------------------------------------------- auth

def test_correct_key_is_accepted():
    payme.check_auth(_auth("test-merchant-key"))


def test_wrong_key_is_rejected():
    with pytest.raises(payme.PaymeError) as exc:
        payme.check_auth(_auth("not-the-key"))
    assert exc.value.code == payme.ERR_ACCESS_DENIED


def test_missing_header_is_rejected():
    with pytest.raises(payme.PaymeError) as exc:
        payme.check_auth(None)
    assert exc.value.code == payme.ERR_ACCESS_DENIED


def test_malformed_header_is_rejected():
    for header in ("Bearer abc", "Basic !!!not-base64!!!", "Basic "):
        with pytest.raises(payme.PaymeError):
            payme.check_auth(header)


def test_key_comparison_is_constant_time():
    """A plain `==` leaks the secret's prefix through timing, and this endpoint
    is public by necessity."""
    import inspect

    assert "compare_digest" in inspect.getsource(payme.check_auth)


def test_unconfigured_provider_refuses_every_caller(monkeypatch):
    monkeypatch.setattr(settings, "PAYME_KEY", "", raising=False)
    with pytest.raises(payme.PaymeError) as exc:
        payme.check_auth(_auth("anything"))
    assert exc.value.code == payme.ERR_ACCESS_DENIED


# ------------------------------------------------------------------ amounts

def test_amount_must_match_the_price_exactly():
    """79 000 soʻm is 7 900 000 tiyin. Anything else is refused rather than
    accepted at whatever was sent."""
    assert payme._expected_amount_tiyin() == 7_900_000

    for wrong in (7_900_001, 7_899_999, 790_000, 0, -7_900_000):
        with pytest.raises(payme.PaymeError) as exc:
            payme._check_amount({"amount": wrong})
        assert exc.value.code == payme.ERR_INVALID_AMOUNT


def test_amount_must_be_an_integer():
    """Floats do not survive reconciliation; 7900000.0 is not acceptable."""
    for wrong in (7_900_000.0, "7900000", None):
        with pytest.raises(payme.PaymeError):
            payme._check_amount({"amount": wrong})


# ------------------------------------------------------------ idempotency

class _Txn:
    """Stand-in for a stored PaymeTransaction row."""

    def __init__(self, **kw):
        self.id = kw.get("id", uuid.uuid4())
        self.payme_id = kw.get("payme_id", "abc")
        self.user_id = kw.get("user_id", uuid.uuid4())
        self.amount = kw.get("amount", 7_900_000)
        self.state = kw.get("state", PaymeState.CREATED.value)
        self.payme_time = kw.get("payme_time", payme._now_ms())
        self.create_time = kw.get("create_time", payme._now_ms())
        self.perform_time = kw.get("perform_time", 0)
        self.cancel_time = kw.get("cancel_time", 0)
        self.reason = kw.get("reason")


@pytest.mark.asyncio
async def test_repeated_create_returns_the_same_transaction():
    """Payme retries. A second row for the same id is a second charge."""
    existing = _Txn(payme_id="txn-1")
    session = FakeSession([existing])

    result = await payme.create_transaction(session, {"id": "txn-1", "amount": 7_900_000})

    assert result["transaction"] == str(existing.id)
    assert result["state"] == PaymeState.CREATED.value
    assert session.added == [], "a retry must not insert another transaction"


@pytest.mark.asyncio
async def test_repeated_perform_does_not_extend_the_subscription_twice():
    performed = _Txn(state=PaymeState.PERFORMED.value, perform_time=123456)
    session = FakeSession([performed])

    result = await payme.perform_transaction(session, {"id": performed.payme_id})

    assert result["perform_time"] == 123456
    assert session.added == [], "a retry must not grant a second subscription"


@pytest.mark.asyncio
async def test_repeated_cancel_is_stable():
    cancelled = _Txn(state=PaymeState.CANCELLED.value, cancel_time=999)
    session = FakeSession([cancelled])

    result = await payme.cancel_transaction(session, {"id": cancelled.payme_id, "reason": 3})

    assert result["cancel_time"] == 999
    assert result["state"] == PaymeState.CANCELLED.value


# ----------------------------------------------------------- state machine

@pytest.mark.asyncio
async def test_cannot_perform_a_cancelled_transaction():
    session = FakeSession([_Txn(state=PaymeState.CANCELLED.value)])
    with pytest.raises(payme.PaymeError) as exc:
        await payme.perform_transaction(session, {"id": "abc"})
    assert exc.value.code == payme.ERR_CANNOT_PERFORM


@pytest.mark.asyncio
async def test_performing_an_unknown_transaction_is_an_error():
    session = FakeSession([None])
    with pytest.raises(payme.PaymeError) as exc:
        await payme.perform_transaction(session, {"id": "nope"})
    assert exc.value.code == payme.ERR_TRANSACTION_NOT_FOUND


@pytest.mark.asyncio
async def test_expired_transaction_cannot_be_performed():
    """Payme cancels anything unperformed for 12 hours. Honouring it late would
    take money for a checkout the payer has long since abandoned."""
    stale = _Txn(payme_time=payme._now_ms() - payme.TRANSACTION_TIMEOUT_MS - 1000)
    session = FakeSession([stale])

    with pytest.raises(payme.PaymeError) as exc:
        await payme.perform_transaction(session, {"id": stale.payme_id})

    assert exc.value.code == payme.ERR_CANNOT_PERFORM
    assert stale.state == PaymeState.CANCELLED.value


@pytest.mark.asyncio
async def test_cancelling_after_perform_uses_the_refund_state():
    """-1 and -2 are different facts: the second means money moved and has to
    be given back, which is also when the subscription must be withdrawn."""
    performed = _Txn(state=PaymeState.PERFORMED.value)
    session = FakeSession([performed, []])

    result = await payme.cancel_transaction(session, {"id": performed.payme_id, "reason": 5})

    assert result["state"] == PaymeState.CANCELLED_AFTER_PERFORM.value


@pytest.mark.asyncio
async def test_cancelling_before_perform_uses_the_plain_state():
    created = _Txn(state=PaymeState.CREATED.value)
    session = FakeSession([created])

    result = await payme.cancel_transaction(session, {"id": created.payme_id})

    assert result["state"] == PaymeState.CANCELLED.value


# --------------------------------------------------------------- accounts

@pytest.mark.asyncio
async def test_second_open_transaction_for_one_account_is_refused():
    """Otherwise a payer with a half-finished checkout can start another and be
    charged twice for one month."""
    class _User:
        id = uuid.uuid4()

    session = FakeSession([_User(), _Txn()])  # user found, then an open txn
    with pytest.raises(payme.PaymeError) as exc:
        await payme.check_perform_transaction(
            session, {"amount": 7_900_000, "account": {"user_id": str(uuid.uuid4())}}
        )
    assert exc.value.code == payme.ERR_ACCOUNT_BUSY


@pytest.mark.asyncio
async def test_unknown_user_is_an_account_error_not_a_crash():
    session = FakeSession([None])
    with pytest.raises(payme.PaymeError) as exc:
        await payme._resolve_account(session, {"account": {"user_id": str(uuid.uuid4())}})
    assert exc.value.code == payme.ERR_ACCOUNT_NOT_FOUND


@pytest.mark.asyncio
async def test_garbage_account_id_is_an_account_error():
    session = FakeSession([])
    for value in ("not-a-uuid", "", None, 12345):
        with pytest.raises(payme.PaymeError) as exc:
            await payme._resolve_account(session, {"account": {"user_id": value}})
        assert exc.value.code == payme.ERR_ACCOUNT_NOT_FOUND


# --------------------------------------------------------------- dispatch

@pytest.mark.asyncio
async def test_unknown_method_returns_a_json_rpc_error():
    response = await payme.dispatch(FakeSession(), {"id": 7, "method": "DropDatabase"})
    assert response["error"]["code"] == payme.ERR_METHOD_NOT_FOUND
    assert response["id"] == 7


@pytest.mark.asyncio
async def test_handler_crash_becomes_a_protocol_error_not_a_500(monkeypatch):
    """Payme retries hard on transport failures, and a stack trace in the
    response helps an attacker and nobody else."""

    async def boom(*_a, **_k):
        raise RuntimeError("database on fire")

    monkeypatch.setitem(payme.HANDLERS, "CheckTransaction", boom)
    session = FakeSession()

    response = await payme.dispatch(session, {"id": 1, "method": "CheckTransaction"})

    assert response["error"]["code"] == payme.ERR_TRANSPORT
    assert "fire" not in str(response), "internal detail must not leak"
    assert session.rolled_back == 1


@pytest.mark.asyncio
async def test_errors_carry_all_three_locales():
    """Payme renders the message to the payer in their own language."""
    response = await payme.dispatch(FakeSession(), {"id": 1, "method": "Nope"})
    assert set(response["error"]["message"]) == {"ru", "uz", "en"}


def test_state_values_match_the_published_protocol():
    """These integers are Payme's, not ours; changing one silently breaks
    reconciliation on their side."""
    assert PaymeState.CREATED.value == 1
    assert PaymeState.PERFORMED.value == 2
    assert PaymeState.CANCELLED.value == -1
    assert PaymeState.CANCELLED_AFTER_PERFORM.value == -2
