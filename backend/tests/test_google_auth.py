"""Google ID token verification.

Tokens are minted here with a throwaway RSA key and verified through the real
code path, so the signature check is genuinely exercised rather than mocked
away. Only the key fetch is substituted — that is Google's TLS, not our logic.

The test that matters most is `test_token_for_another_application_is_rejected`.
An ID token is signed by Google for *some* app; accepting one without checking
`aud` means an attacker can sign into their own unrelated site, take the token
Google hands them, post it here, and be logged in as that Google account. The
signature is valid. It just was not issued for us. That check is the single
most commonly missing line in a Google sign-in integration.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import settings
from app.services.auth import google

CLIENT_ID = "1234567890-test.apps.googleusercontent.com"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _key.private_bytes(
    encoding=__import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.PEM,
    format=__import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).PrivateFormat.PKCS8,
    encryption_algorithm=__import__(
        "cryptography.hazmat.primitives.serialization", fromlist=["x"]
    ).NoEncryption(),
)
PUBLIC_KEY = _key.public_key()


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", CLIENT_ID, raising=False)
    # Substitute only the key fetch; everything else runs for real.
    monkeypatch.setattr(google, "_signing_key", lambda _token: PUBLIC_KEY)


def mint(**overrides) -> str:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "108176283746152839472",
        "email": "aziz@example.com",
        "email_verified": True,
        "name": "Aziz Karimov",
        "picture": "https://lh3.googleusercontent.com/a/abc",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_PEM, algorithm="RS256")


# ------------------------------------------------------------------ genuine

def test_a_genuine_token_is_accepted():
    profile = google.verify(mint())
    assert profile.google_sub == "108176283746152839472"
    assert profile.email == "aziz@example.com"
    assert profile.email_verified is True
    assert profile.full_name == "Aziz Karimov"


def test_both_issuer_spellings_are_accepted():
    """Google issues both and treats them as equivalent."""
    for issuer in ("accounts.google.com", "https://accounts.google.com"):
        assert google.verify(mint(iss=issuer)).google_sub


def test_email_verified_as_a_string_is_honoured():
    """Google sends this as a bool or "true" depending on the flow."""
    assert google.verify(mint(email_verified="true")).email_verified is True


def test_anything_else_counts_as_unverified():
    """Safe direction: this flag gates account linking."""
    for value in ("false", False, None, "maybe", 1):
        assert google.verify(mint(email_verified=value)).email_verified is False


# ------------------------------------------------------------------ forgery

def test_token_for_another_application_is_rejected():
    """The single most commonly missing check. A token minted for any other
    Google app is validly signed and must still be refused."""
    other_app = mint(aud="9999-someone-elses.apps.googleusercontent.com")
    with pytest.raises(google.GoogleAuthError, match="not issued for this application"):
        google.verify(other_app)


def test_token_from_another_issuer_is_rejected():
    with pytest.raises(google.GoogleAuthError, match="not issued by Google"):
        google.verify(mint(iss="https://evil.example.com"))


def test_expired_token_is_rejected():
    stale = mint(exp=int(time.time()) - 60, iat=int(time.time()) - 3600)
    with pytest.raises(google.GoogleAuthError, match="expired"):
        google.verify(stale)


def test_token_signed_by_another_key_is_rejected():
    """Somebody else's RSA key must not mint accounts here."""
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization

    pem = attacker.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    forged = jwt.encode(
        {
            "iss": "https://accounts.google.com",
            "aud": CLIENT_ID,
            "sub": "1",
            "exp": int(time.time()) + 3600,
        },
        pem,
        algorithm="RS256",
    )
    with pytest.raises(google.GoogleAuthError):
        google.verify(forged)


def test_unsigned_token_is_rejected():
    """The `alg: none` attack: a token with no signature at all."""
    unsigned = jwt.encode(
        {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1",
         "exp": int(time.time()) + 3600},
        key="",
        algorithm="none",
    )
    with pytest.raises(google.GoogleAuthError):
        google.verify(unsigned)


def test_token_without_a_subject_is_rejected():
    with pytest.raises(google.GoogleAuthError):
        google.verify(mint(sub=None))


def test_garbage_is_rejected():
    for value in ("", "not-a-jwt", "a.b.c"):
        with pytest.raises(google.GoogleAuthError):
            google.verify(value)


# ------------------------------------------------------------- configuration

def test_unconfigured_client_rejects_everything(monkeypatch):
    """Without a client id there is nothing to check `aud` against, and a
    token accepted on signature alone is one minted for another app."""
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "", raising=False)
    with pytest.raises(google.GoogleAuthError, match="not configured"):
        google.verify(mint())
