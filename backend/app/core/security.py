"""Password hashing, JWTs, and Plaid token encryption."""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

_hasher = PasswordHasher()


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        return False
    return True


def needs_rehash(hashed: str) -> bool:
    return _hasher.check_needs_rehash(hashed)


# --------------------------------------------------------------------------- #
# Refresh tokens: generated opaque, stored hashed
# --------------------------------------------------------------------------- #
def new_refresh_token() -> tuple[str, str]:
    """Return (token, sha256_hash). Only the hash is persisted."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Access tokens
# --------------------------------------------------------------------------- #
def create_access_token(user_id: str, extra: dict[str, Any] | None = None) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(minutes=s.access_token_ttl_minutes),
        "typ": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.jwt_signing_key, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    s = get_settings()
    return jwt.decode(token, s.jwt_signing_key, algorithms=["HS256"])


# --------------------------------------------------------------------------- #
# Plaid access_token encryption
#
# Encrypted with TOKEN_ENCRYPTION_KEY, deliberately separate from the database
# password: a stolen DB dump alone must not yield usable Plaid tokens.
# Losing this key means re-linking every bank, which burns connection credits.
# --------------------------------------------------------------------------- #
def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set; refusing to handle Plaid tokens")
    return Fernet(key.encode())


def key_fingerprint() -> str:
    """Short, non-reversible identifier for the active encryption key.

    Travels with every exported blob so a restore can tell "wrong key" from
    "corrupt data" before it starts guessing. Derived through SHA-256, so it
    reveals nothing useful about the key itself.
    """
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def encrypt_token(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode())


def decrypt_token(blob: bytes) -> str:
    try:
        return _fernet().decrypt(blob).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Could not decrypt a Plaid access_token. The TOKEN_ENCRYPTION_KEY has "
            "probably changed. Restore the original key -- re-linking costs a "
            "connection credit. See docs/04-plaid-integration.md."
        ) from exc
