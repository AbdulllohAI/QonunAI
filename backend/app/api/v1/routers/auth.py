from __future__ import annotations

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.security import create_token, decode_token, hash_password, verify_password
from app.db.models import User
from app.db.session import get_session
from app.core.logging import get_logger
from app.services.auth import google, sms, telegram
from app.services.auth import phone as phone_auth
from app.schemas.common import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _tokens(user: User) -> TokenPair:
    return TokenPair(
        access_token=create_token(str(user.id), "access", role=user.role.value),
        refresh_token=create_token(str(user.id), "refresh"),
        expires_in=settings.ACCESS_TOKEN_TTL_MIN * 60,
    )


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: AsyncSession = Depends(get_session)):
    existing = (
        await session.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    user = User(
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        preferred_language=payload.preferred_language,
    )
    session.add(user)
    await session.flush()
    return _tokens(user)


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)):
    user = (
        await session.execute(select(User).where(User.email == payload.email.lower()))
    ).scalar_one_or_none()
    # Same error for unknown email and wrong password — do not leak which.
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account disabled")
    return _tokens(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, session: AsyncSession = Depends(get_session)):
    try:
        claims = decode_token(payload.refresh_token, "refresh")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token") from None

    user = (
        await session.execute(select(User).where(User.id == claims["sub"]))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    return _tokens(user)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.post("/telegram", response_model=TokenPair)
async def telegram_login(
    payload: dict, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    """Sign in with the Telegram Login Widget.

    The browser passes Telegram's signed payload straight through, so the
    signature check in `telegram.verify` is the entire security boundary — see
    that module before changing anything here.

    Matching is by Telegram id, never by name or username: both are chosen by
    the user and both can be changed to somebody else's at any time.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Telegram sign-in is not configured"
        )

    try:
        profile = telegram.verify(payload)
    except telegram.TelegramAuthError as exc:
        # 401, not 400: this is a failed authentication, and the distinction
        # matters to anything reading logs for attacks.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None

    user = (
        await session.execute(select(User).where(User.telegram_id == profile.telegram_id))
    ).scalar_one_or_none()

    if user is None:
        user = User(
            telegram_id=profile.telegram_id,
            full_name=profile.full_name,
            # No email and no password: Telegram supplies neither, and inventing
            # either would put a credential in the table that nobody holds.
            email=None,
            hashed_password=None,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    elif profile.full_name and user.full_name != profile.full_name:
        # Keep the display name current, but never touch anything a rename
        # could be used to hijack.
        user.full_name = profile.full_name
        await session.commit()

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")

    return _tokens(user)


@router.post("/google", response_model=TokenPair)
async def google_login(
    payload: dict, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    """Sign in with a Google ID token.

    Account resolution, in order, and the order is the security:

    1. A matching `google_sub` signs in. This is the stable identifier.
    2. Otherwise, an existing account with the same email is *linked* — but
       only when Google says the address is verified. Linking on an unverified
       address is account takeover: anyone can put someone else's email on a
       Google account, and without the check that would hand them the matching
       account here.
    3. Otherwise a new account is created.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Google sign-in is not configured"
        )

    try:
        profile = google.verify(str(payload.get("credential") or ""))
    except google.GoogleAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None

    user = (
        await session.execute(select(User).where(User.google_sub == profile.google_sub))
    ).scalar_one_or_none()

    if user is None and profile.email and profile.email_verified:
        existing = (
            await session.execute(select(User).where(User.email == profile.email))
        ).scalar_one_or_none()
        if existing is not None:
            existing.google_sub = profile.google_sub
            user = existing

    if user is None:
        user = User(
            google_sub=profile.google_sub,
            # Only store the address if Google vouched for it; an unverified
            # one is a claim, not a fact, and it would collide with the
            # unique index against a real owner.
            email=profile.email if profile.email_verified else None,
            full_name=profile.full_name,
            hashed_password=None,
        )
        session.add(user)

    if profile.full_name and user.full_name != profile.full_name:
        user.full_name = profile.full_name

    await session.commit()
    await session.refresh(user)

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")

    return _tokens(user)


@router.post("/phone/request", status_code=status.HTTP_202_ACCEPTED)
async def phone_request(
    payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Send a one-time code by SMS.

    Answers the same way whether or not the number already has an account.
    Telling a caller "no such user" here turns the endpoint into a way to test
    which phone numbers are registered.
    """
    if not sms.sms_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Phone sign-in is not configured"
        )

    try:
        issued = await phone_auth.request_code(session, str(payload.get("phone") or ""))
    except phone_auth.PhoneAuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    try:
        await sms.send_sms(
            issued.phone, f"QonunAI tasdiqlash kodi: {issued.code}"
        )
    except sms.SmsError as exc:
        # The code is already stored, but the user will never see it, so say so
        # rather than leaving them waiting on a message that is not coming.
        log.error("phone_sms_failed", extra={"error": str(exc)[:200]})
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "SMS yuborib boʻlmadi, keyinroq urinib koʻring"
        ) from None

    return {"sent": True, "expires_in": phone_auth.CODE_TTL_SECONDS}


@router.post("/phone/verify", response_model=TokenPair)
async def phone_verify(
    payload: dict, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    """Exchange a valid code for tokens, creating the account on first use."""
    if not sms.sms_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Phone sign-in is not configured"
        )

    try:
        number = await phone_auth.verify_code(
            session, str(payload.get("phone") or ""), str(payload.get("code") or "")
        )
    except phone_auth.PhoneAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from None

    user = (
        await session.execute(select(User).where(User.phone == number))
    ).scalar_one_or_none()

    if user is None:
        # No email, no password: a phone account has neither, and inventing
        # either would store a credential nobody holds.
        user = User(phone=number, email=None, hashed_password=None)
        session.add(user)
        await session.commit()
        await session.refresh(user)

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")

    return _tokens(user)
