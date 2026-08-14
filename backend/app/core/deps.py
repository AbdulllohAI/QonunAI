"""FastAPI dependencies: auth, roles, rate limiting.

No `from __future__ import annotations` here deliberately: it turns the
`request: Request` parameter on `RateLimiter.__call__` into an unresolved
string annotation, which breaks FastAPI's OpenAPI schema generation (it treats
`Request` as a plain field to build a JSON schema for instead of recognizing
and injecting the special request object) — verified by reproducing this
under FastAPI 0.115.6 / pydantic 2.10.4, the pinned versions.
"""

import time
import uuid

import jwt
import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger, user_id_ctx
from app.core.security import decode_token
from app.db.models import User, UserRole
from app.db.session import get_session

log = get_logger(__name__)

bearer = HTTPBearer(auto_error=False)
_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(str(settings.REDIS_DSN), decode_responses=True)
    return _redis


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    try:
        payload = decode_token(credentials.credentials, "access")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired") from None
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from None

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token subject") from None

    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or inactive")

    user_id_ctx.set(str(user.id))
    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """Anonymous access is allowed on read paths, at a lower rate limit."""
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials, session)
    except HTTPException:
        return None


def require_role(*roles: UserRole):
    async def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires one of: {', '.join(r.value for r in roles)}",
            )
        return user

    return dependency


require_admin = require_role(UserRole.ADMIN)


def _window_label(seconds: int) -> str:
    """Human unit for the error message, so it matches what was promised."""
    return {3600: "hour", 86_400: "day", 60: "minute"}.get(seconds, f"{seconds}s")


class RateLimiter:
    """Fixed-window limiter in Redis. Fails open — a Redis outage must not take
    the API down, and the downside of a brief unlimited window is acceptable
    here."""

    def __init__(self, anon: int | None = None, user: int | None = None) -> None:
        self.anon = anon or settings.RATE_LIMIT_ANON
        self.user = user or settings.RATE_LIMIT_USER

    async def __call__(
        self, request: Request, user: User | None = Depends(get_optional_user)
    ) -> None:
        if user and user.role is UserRole.ADMIN:
            return

        identity = str(user.id) if user else _client_ip(request)
        limit = self.user if user else self.anon
        seconds = settings.RATE_LIMIT_WINDOW_SECONDS
        window = int(time.time() // seconds)
        key = f"rl:{window}:{identity}"

        try:
            redis = get_redis()
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, seconds)
        except Exception as exc:
            log.warning("rate limiter unavailable", extra={"error": str(exc)})
            return

        if count > limit:
            # Tell the caller when the window actually resets rather than a
            # flat window length: with a daily quota, "retry in 86400s" is
            # wrong for everyone who did not hit the limit at midnight.
            resets_at = (window + 1) * seconds
            retry_after = max(1, int(resets_at - time.time()))
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"rate limit exceeded ({limit}/{_window_label(seconds)})",
                headers={"Retry-After": str(retry_after)},
            )


rate_limit = RateLimiter()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
