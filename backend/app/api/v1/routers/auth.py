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
from app.services.auth import telegram
from app.schemas.common import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)

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
