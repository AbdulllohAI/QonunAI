"""Google Sign-In: ID token verification.

Google Identity Services hands the browser a signed JWT and the browser hands
it to us. Everything that makes it trustworthy happens here.

The check that matters most, and the one most often missing:

    **`aud` must equal our own client id.**

An ID token is signed by Google for *some* application. Verifying only the
signature accepts a token minted for any other Google app on the internet — an
attacker signs into their own unrelated site, takes the token Google gives
them, posts it here, and is logged in as whoever that Google account is. The
signature is perfectly valid; it just was not issued for us.

`iss` is checked for the same reason, and `exp` is enforced by pyjwt.
"""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

from app.core.config import settings

__all__ = ["GoogleAuthError", "GoogleProfile", "verify"]

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

#: Google issues both spellings and treats them as equivalent.
VALID_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

_jwk_client: PyJWKClient | None = None


class GoogleAuthError(Exception):
    """The credential is not a valid Google ID token issued to this app."""


@dataclass(frozen=True, slots=True)
class GoogleProfile:
    #: Google's stable identifier. Unlike email, it never changes hands, which
    #: is why accounts are keyed on it.
    google_sub: str
    email: str | None
    email_verified: bool
    full_name: str | None
    picture: str | None


def _signing_key(token: str):
    """Google's public key for this token, fetched and cached by pyjwt.

    Split out so tests can supply their own key without reaching the network —
    verification logic is what is being tested, not Google's TLS.
    """
    global _jwk_client
    if _jwk_client is None:
        # Caches keys and honours their lifetime; Google rotates them, so
        # pinning one would break sign-in without warning.
        _jwk_client = PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True)
    return _jwk_client.get_signing_key_from_jwt(token).key


def verify(credential: str) -> GoogleProfile:
    """Validate a Google ID token and return the profile it attests to."""
    if not settings.GOOGLE_CLIENT_ID:
        raise GoogleAuthError("Google sign-in is not configured")
    if not credential:
        raise GoogleAuthError("Missing credential")

    try:
        key = _signing_key(credential)
    except Exception as exc:  # noqa: BLE001 — network, rotation, malformed token
        raise GoogleAuthError(f"Could not fetch Google signing key: {exc}") from None

    try:
        claims = jwt.decode(
            credential,
            key=key,
            algorithms=["RS256"],
            # Without this the token only proves Google signed something for
            # somebody. See the module docstring.
            audience=settings.GOOGLE_CLIENT_ID,
            issuer=list(VALID_ISSUERS),
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise GoogleAuthError("Google sign-in has expired, please try again") from None
    except jwt.InvalidAudienceError:
        raise GoogleAuthError("Token was not issued for this application") from None
    except jwt.InvalidIssuerError:
        raise GoogleAuthError("Token was not issued by Google") from None
    except jwt.InvalidTokenError as exc:
        raise GoogleAuthError(f"Invalid Google token: {exc}") from None

    sub = claims.get("sub")
    if not sub:
        raise GoogleAuthError("Token has no subject")

    return GoogleProfile(
        google_sub=str(sub),
        email=claims.get("email"),
        # Google sends this as a bool or the string "true" depending on the
        # flow. Anything else is unverified, which is the safe direction since
        # this flag gates account linking.
        #
        # `is True` rather than `in (True, "true")`: Python holds 1 == True, so
        # membership would quietly accept the integer 1 as a verified address.
        # Caught by a test that fed it exactly that.
        email_verified=(
            claims.get("email_verified") is True or claims.get("email_verified") == "true"
        ),
        full_name=claims.get("name"),
        picture=claims.get("picture"),
    )
