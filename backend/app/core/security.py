"""Password hashing and JWT issue/verify."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from passlib.context import CryptContext

from app.core.config import settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

TokenType = Literal["access", "refresh"]


def hash_password(raw: str) -> str:
    return _pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return _pwd.verify(raw, hashed)


def create_token(subject: str, token_type: TokenType = "access", **claims: Any) -> str:
    now = datetime.now(timezone.utc)
    ttl = (
        timedelta(minutes=settings.ACCESS_TOKEN_TTL_MIN)
        if token_type == "access"
        else timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    )
    payload = {
        "sub": subject,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        **claims,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str, expected_type: TokenType = "access") -> dict[str, Any]:
    """Raises jwt.PyJWTError subclasses on failure — callers translate to HTTP 401."""
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    return payload
