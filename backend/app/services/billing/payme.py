"""Payme (Paycom) Merchant API.

Payme calls us, not the other way round. It sends JSON-RPC 2.0 over a single
endpoint and expects an exact contract back; anything else and the payment is
left in limbo, which for the customer means money taken and no subscription.

Three properties matter more than anything else here:

**Idempotency.** Payme retries. A method arriving twice must produce the same
answer as the first time, not a second transaction and not an error — a
duplicated CreateTransaction is how a customer gets charged twice.

**One open transaction per account.** If a second transaction is created for an
account that already has one waiting, Payme expects the account to be reported
as unavailable, not a cheerful second charge.

**Money is integers.** Amounts are in tiyin (1 soʻm = 100 tiyin), so 79 000 soʻm
arrives as 7 900 000. No float touches an amount anywhere in this file: 0.1 +
0.2 is a rounding curiosity in most code and a reconciliation failure here.

Error codes are Payme's, and the numbers are load-bearing — their side branches
on them.
"""
from __future__ import annotations

import base64
import hmac
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import PaymeState, PaymeTransaction, PlanTier, Subscription, User

log = get_logger(__name__)

# ---------------------------------------------------------------- protocol

ERR_TRANSPORT = -32300
ERR_ACCESS_DENIED = -32504
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_AMOUNT = -31001
ERR_TRANSACTION_NOT_FOUND = -31003
ERR_CANNOT_PERFORM = -31008
ERR_CANNOT_CANCEL = -31007
#: The -3105x band is reserved for "something about the account is wrong", and
#: Payme shows the message we return here directly to the payer.
ERR_ACCOUNT_NOT_FOUND = -31050
ERR_ACCOUNT_BUSY = -31051

#: Payme cancels anything left unperformed for 12 hours; past that a
#: PerformTransaction must be refused rather than quietly honoured.
TRANSACTION_TIMEOUT_MS = 12 * 60 * 60 * 1000

SUBSCRIPTION_DAYS = 30


class PaymeError(Exception):
    """A JSON-RPC error, in the shape Payme expects to receive."""

    def __init__(self, code: int, message: str, data: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_response(self, request_id: Any) -> dict:
        error: dict[str, Any] = {
            "code": self.code,
            # Payme renders one of these to the payer depending on their locale,
            # so all three are supplied rather than a single English string.
            "message": {"ru": self.message, "uz": self.message, "en": self.message},
        }
        if self.data:
            error["data"] = self.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


@dataclass(frozen=True, slots=True)
class Account:
    user_id: uuid.UUID


def _now_ms() -> int:
    return int(time.time() * 1000)


def check_auth(header: str | None) -> None:
    """Authenticate the caller as Payme.

    The credential is `Basic base64("Paycom:<key>")`. Compared with
    `compare_digest`, because a plain `==` on a secret leaks its prefix through
    timing to anyone willing to measure, and this endpoint is public by
    necessity.
    """
    if not settings.PAYME_KEY:
        raise PaymeError(ERR_ACCESS_DENIED, "Payment provider is not configured")
    if not header or not header.lower().startswith("basic "):
        raise PaymeError(ERR_ACCESS_DENIED, "Authorization required")

    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        raise PaymeError(ERR_ACCESS_DENIED, "Malformed authorization header") from None

    expected = f"Paycom:{settings.PAYME_KEY}"
    if not hmac.compare_digest(decoded, expected):
        raise PaymeError(ERR_ACCESS_DENIED, "Invalid credentials")


def _expected_amount_tiyin() -> int:
    return settings.PRO_PRICE_UZS * 100


async def _resolve_account(session: AsyncSession, params: dict) -> Account:
    """Turn Payme's `account` object into one of our users.

    Payme is configured with a single field — `user_id` — which the payer's
    checkout link carries. Anything unparseable is an account error rather than
    a server error, so the payer is told their details are wrong instead of
    seeing a failure they cannot act on.
    """
    account = params.get("account") or {}
    raw = account.get(settings.PAYME_ACCOUNT_FIELD)
    if not raw:
        raise PaymeError(ERR_ACCOUNT_NOT_FOUND, "User not specified")

    try:
        user_id = uuid.UUID(str(raw))
    except (ValueError, AttributeError):
        raise PaymeError(ERR_ACCOUNT_NOT_FOUND, "User not found") from None

    user = (
        await session.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))
    ).scalar_one_or_none()
    if user is None:
        raise PaymeError(ERR_ACCOUNT_NOT_FOUND, "User not found")
    return Account(user_id=user.id)


def _check_amount(params: dict) -> int:
    amount = params.get("amount")
    if not isinstance(amount, int) or amount != _expected_amount_tiyin():
        raise PaymeError(ERR_INVALID_AMOUNT, "Incorrect amount")
    return amount


async def _find(session: AsyncSession, payme_id: str) -> PaymeTransaction | None:
    return (
        await session.execute(
            select(PaymeTransaction).where(PaymeTransaction.payme_id == payme_id)
        )
    ).scalar_one_or_none()


# ----------------------------------------------------------------- methods


async def check_perform_transaction(session: AsyncSession, params: dict) -> dict:
    """Can a transaction be created for this account and amount?

    Called before the payer confirms, so a refusal here is cheap and a wrong
    "yes" is expensive.
    """
    _check_amount(params)
    account = await _resolve_account(session, params)

    existing = (
        await session.execute(
            select(PaymeTransaction).where(
                PaymeTransaction.user_id == account.user_id,
                PaymeTransaction.state == PaymeState.CREATED.value,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise PaymeError(ERR_ACCOUNT_BUSY, "A payment is already in progress")

    return {"allow": True}


async def create_transaction(session: AsyncSession, params: dict) -> dict:
    """Register a transaction, or return the existing one unchanged.

    The second half is the whole point. Payme retries this call, and creating a
    second row for the same `id` would let one payment be performed twice.
    """
    payme_id = str(params.get("id") or "")
    if not payme_id:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, "Transaction id missing")

    existing = await _find(session, payme_id)
    if existing is not None:
        if existing.state != PaymeState.CREATED.value:
            raise PaymeError(ERR_CANNOT_PERFORM, "Transaction is no longer open")
        # Past its window, Payme expects it cancelled rather than resurrected.
        if _now_ms() - existing.payme_time > TRANSACTION_TIMEOUT_MS:
            existing.state = PaymeState.CANCELLED.value
            existing.cancel_time = _now_ms()
            existing.reason = 4  # Payme's code for "timed out"
            await session.commit()
            raise PaymeError(ERR_CANNOT_PERFORM, "Transaction timed out")
        return {
            "create_time": existing.create_time,
            "transaction": str(existing.id),
            "state": existing.state,
        }

    amount = _check_amount(params)
    account = await _resolve_account(session, params)

    open_for_user = (
        await session.execute(
            select(PaymeTransaction).where(
                PaymeTransaction.user_id == account.user_id,
                PaymeTransaction.state == PaymeState.CREATED.value,
            )
        )
    ).scalar_one_or_none()
    if open_for_user is not None:
        raise PaymeError(ERR_ACCOUNT_BUSY, "A payment is already in progress")

    now = _now_ms()
    transaction = PaymeTransaction(
        payme_id=payme_id,
        user_id=account.user_id,
        amount=amount,
        state=PaymeState.CREATED.value,
        payme_time=int(params.get("time") or now),
        create_time=now,
    )
    session.add(transaction)
    await session.commit()
    await session.refresh(transaction)

    log.info(
        "payme_transaction_created",
        extra={"payme_id": payme_id, "user_id": str(account.user_id), "amount": amount},
    )
    return {"create_time": transaction.create_time, "transaction": str(transaction.id), "state": transaction.state}


async def perform_transaction(session: AsyncSession, params: dict) -> dict:
    """Take the money and grant the subscription.

    Returning the stored result for an already-performed transaction is what
    keeps a retry from extending the subscription a second time.
    """
    payme_id = str(params.get("id") or "")
    transaction = await _find(session, payme_id)
    if transaction is None:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, "Transaction not found")

    if transaction.state == PaymeState.PERFORMED.value:
        return {
            "transaction": str(transaction.id),
            "perform_time": transaction.perform_time,
            "state": transaction.state,
        }
    if transaction.state != PaymeState.CREATED.value:
        raise PaymeError(ERR_CANNOT_PERFORM, "Transaction is cancelled")

    if _now_ms() - transaction.payme_time > TRANSACTION_TIMEOUT_MS:
        transaction.state = PaymeState.CANCELLED.value
        transaction.cancel_time = _now_ms()
        transaction.reason = 4
        await session.commit()
        raise PaymeError(ERR_CANNOT_PERFORM, "Transaction timed out")

    now = _now_ms()
    transaction.state = PaymeState.PERFORMED.value
    transaction.perform_time = now
    await _grant_subscription(session, transaction.user_id)
    await session.commit()

    log.info(
        "payme_transaction_performed",
        extra={"payme_id": payme_id, "user_id": str(transaction.user_id)},
    )
    return {"transaction": str(transaction.id), "perform_time": now, "state": transaction.state}


async def cancel_transaction(session: AsyncSession, params: dict) -> dict:
    """Cancel, and revoke the subscription if the money had already been taken.

    Payme distinguishes a cancellation before performing (-1) from one after
    (-2); the second is a refund and must actually withdraw what was granted,
    or a refunded customer keeps Pro for free.
    """
    payme_id = str(params.get("id") or "")
    transaction = await _find(session, payme_id)
    if transaction is None:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, "Transaction not found")

    if transaction.state in (PaymeState.CANCELLED.value, PaymeState.CANCELLED_AFTER_PERFORM.value):
        return {
            "transaction": str(transaction.id),
            "cancel_time": transaction.cancel_time,
            "state": transaction.state,
        }

    now = _now_ms()
    if transaction.state == PaymeState.PERFORMED.value:
        transaction.state = PaymeState.CANCELLED_AFTER_PERFORM.value
        await _revoke_subscription(session, transaction.user_id)
    else:
        transaction.state = PaymeState.CANCELLED.value

    transaction.cancel_time = now
    reason = params.get("reason")
    transaction.reason = reason if isinstance(reason, int) else None
    await session.commit()

    log.info(
        "payme_transaction_cancelled",
        extra={"payme_id": payme_id, "state": transaction.state, "reason": transaction.reason},
    )
    return {"transaction": str(transaction.id), "cancel_time": now, "state": transaction.state}


async def check_transaction(session: AsyncSession, params: dict) -> dict:
    transaction = await _find(session, str(params.get("id") or ""))
    if transaction is None:
        raise PaymeError(ERR_TRANSACTION_NOT_FOUND, "Transaction not found")
    return {
        "create_time": transaction.create_time,
        "perform_time": transaction.perform_time,
        "cancel_time": transaction.cancel_time,
        "transaction": str(transaction.id),
        "state": transaction.state,
        "reason": transaction.reason,
    }


async def get_statement(session: AsyncSession, params: dict) -> dict:
    """Transactions in a window, for Payme's own reconciliation."""
    start = int(params.get("from") or 0)
    end = int(params.get("to") or _now_ms())
    rows = (
        await session.execute(
            select(PaymeTransaction)
            .where(PaymeTransaction.payme_time >= start, PaymeTransaction.payme_time <= end)
            .order_by(PaymeTransaction.payme_time)
        )
    ).scalars()
    return {
        "transactions": [
            {
                "id": t.payme_id,
                "time": t.payme_time,
                "amount": t.amount,
                "account": {settings.PAYME_ACCOUNT_FIELD: str(t.user_id)},
                "create_time": t.create_time,
                "perform_time": t.perform_time,
                "cancel_time": t.cancel_time,
                "transaction": str(t.id),
                "state": t.state,
                "reason": t.reason,
            }
            for t in rows
        ]
    }


# ----------------------------------------------------------- subscriptions


async def _grant_subscription(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Extend from whichever is later: now, or an existing expiry.

    Renewing early should add to the remaining time rather than throw it away —
    a customer who pays on day 25 of 30 keeps those five days.
    """
    now = datetime.now(timezone.utc)
    current = (
        await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    base = current.expires_at if current and current.expires_at > now else now
    session.add(
        Subscription(
            user_id=user_id,
            tier=PlanTier.PRO,
            starts_at=now,
            expires_at=base + timedelta(days=SUBSCRIPTION_DAYS),
        )
    )


async def _revoke_subscription(session: AsyncSession, user_id: uuid.UUID) -> None:
    """End the current subscription now, on refund."""
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            select(Subscription).where(
                Subscription.user_id == user_id, Subscription.expires_at > now
            )
        )
    ).scalars()
    for row in rows:
        row.expires_at = now


async def active_tier(session: AsyncSession, user_id: uuid.UUID | None) -> PlanTier:
    """The tier a user is entitled to right now."""
    if user_id is None:
        return PlanTier.FREE
    now = datetime.now(timezone.utc)
    found = (
        await session.execute(
            select(Subscription.id).where(
                Subscription.user_id == user_id, Subscription.expires_at > now
            ).limit(1)
        )
    ).scalar_one_or_none()
    return PlanTier.PRO if found else PlanTier.FREE


HANDLERS = {
    "CheckPerformTransaction": check_perform_transaction,
    "CreateTransaction": create_transaction,
    "PerformTransaction": perform_transaction,
    "CancelTransaction": cancel_transaction,
    "CheckTransaction": check_transaction,
    "GetStatement": get_statement,
}


async def dispatch(session: AsyncSession, body: dict) -> dict:
    """Route one JSON-RPC call, converting our errors into their shape."""
    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    handler = HANDLERS.get(str(method))
    if handler is None:
        return PaymeError(ERR_METHOD_NOT_FOUND, f"Unknown method: {method}").to_response(request_id)

    try:
        result = await handler(session, params)
    except PaymeError as exc:
        return exc.to_response(request_id)
    except Exception as exc:  # noqa: BLE001
        # Never leak an internal error to a payment provider as a 500: Payme
        # retries hard on transport failures, and a stack trace in the response
        # tells an attacker about the system while helping nobody.
        log.exception("payme_handler_failed", extra={"method": method})
        await session.rollback()
        return PaymeError(ERR_TRANSPORT, "Internal error").to_response(request_id)

    return {"jsonrpc": "2.0", "id": request_id, "result": result}
