"""Plaid webhook verification.

The webhook endpoint is publicly reachable, so an unverified handler is an
open door: anyone could forge sync triggers or fake ITEM_LOGIN_REQUIRED.

Plaid signs each request with a JWT in the Plaid-Verification header. The JWT
is ES256 and its payload carries a SHA-256 of the request body, so verifying
it proves both the sender and that the body was not altered.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import jwt
from jwt.algorithms import ECAlgorithm

log = logging.getLogger("nnf.plaid.webhook")

# key_id -> JWK. Plaid rotates keys, so this is a cache, not a fixture.
_key_cache: dict[str, dict[str, Any]] = {}

MAX_AGE_SECONDS = 5 * 60


class WebhookVerificationError(Exception):
    pass


async def verify(body: bytes, header: str | None, client: Any) -> dict[str, Any]:
    """Verify a Plaid webhook. Returns the decoded JWT claims.

    Raises WebhookVerificationError on anything suspicious. Callers must
    reject the request on any exception -- never process unverified input.
    """
    if not header:
        raise WebhookVerificationError("missing Plaid-Verification header")

    try:
        unverified = jwt.get_unverified_header(header)
    except jwt.PyJWTError as exc:
        raise WebhookVerificationError(f"malformed JWT header: {exc}") from exc

    if unverified.get("alg") != "ES256":
        # Refusing anything but ES256 blocks algorithm-confusion attacks,
        # including the alg=none class.
        raise WebhookVerificationError(f"unexpected alg: {unverified.get('alg')}")

    key_id = unverified.get("kid")
    if not key_id:
        raise WebhookVerificationError("JWT header has no kid")

    jwk = _key_cache.get(key_id)
    if jwk is None:
        try:
            resp = await client.webhook_verification_key(key_id)
        except Exception as exc:
            raise WebhookVerificationError(f"could not fetch verification key: {exc}") from exc
        jwk = resp.get("key")
        if not jwk:
            raise WebhookVerificationError("Plaid returned no key for that kid")
        _key_cache[key_id] = jwk

    try:
        public_key = ECAlgorithm.from_jwk(jwk)
        claims = jwt.decode(header, public_key, algorithms=["ES256"], options={"verify_aud": False})
    except jwt.PyJWTError as exc:
        raise WebhookVerificationError(f"JWT signature invalid: {exc}") from exc

    # Replay window.
    issued_at = claims.get("iat")
    if not isinstance(issued_at, int):
        raise WebhookVerificationError("JWT has no usable iat")
    age = time.time() - issued_at
    if age > MAX_AGE_SECONDS:
        raise WebhookVerificationError(f"webhook too old ({int(age)}s)")

    # Body integrity: the claim must match the bytes we actually received.
    expected = claims.get("request_body_sha256")
    if not expected:
        raise WebhookVerificationError("JWT has no request_body_sha256 claim")
    actual = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(expected, actual):
        raise WebhookVerificationError("request body does not match its signature")

    return claims


def reset_key_cache() -> None:
    _key_cache.clear()
