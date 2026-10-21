"""Webhook verification.

The endpoint is public, so the failure modes matter more than the happy path.
Each test here is an attack that must be rejected.
"""
from __future__ import annotations

import hashlib
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.plaid.webhook import WebhookVerificationError, reset_key_cache, verify


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    nums = priv.public_key().public_numbers()
    import base64

    def b64(i: int) -> str:
        return base64.urlsafe_b64encode(i.to_bytes(32, "big")).decode().rstrip("=")

    jwk = {"kty": "EC", "crv": "P-256", "x": b64(nums.x), "y": b64(nums.y), "use": "sig", "alg": "ES256"}
    return priv, jwk


class FakeClient:
    def __init__(self, jwk: dict) -> None:
        self.jwk = jwk
        self.calls = 0

    async def webhook_verification_key(self, key_id: str) -> dict:
        self.calls += 1
        return {"key": self.jwk}


def _sign(priv, body: bytes, kid="k1", iat=None, sha=None):
    claims = {
        "iat": iat if iat is not None else int(time.time()),
        "request_body_sha256": sha if sha is not None else hashlib.sha256(body).hexdigest(),
    }
    return jwt.encode(claims, priv, algorithm="ES256", headers={"kid": kid})


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_key_cache()
    yield
    reset_key_cache()


async def test_valid_webhook_is_accepted() -> None:
    priv, jwk = _keypair()
    body = b'{"webhook_type":"TRANSACTIONS","item_id":"abc"}'
    claims = await verify(body, _sign(priv, body), FakeClient(jwk))
    assert claims["request_body_sha256"] == hashlib.sha256(body).hexdigest()


async def test_missing_header_rejected() -> None:
    _, jwk = _keypair()
    with pytest.raises(WebhookVerificationError, match="missing"):
        await verify(b"{}", None, FakeClient(jwk))


async def test_tampered_body_rejected() -> None:
    """Signature is valid, but the body was changed in flight."""
    priv, jwk = _keypair()
    original = b'{"item_id":"abc"}'
    token = _sign(priv, original)
    tampered = b'{"item_id":"attacker-controlled"}'
    with pytest.raises(WebhookVerificationError, match="does not match"):
        await verify(tampered, token, FakeClient(jwk))


async def test_signature_from_the_wrong_key_rejected() -> None:
    attacker_priv, _ = _keypair()
    _, real_jwk = _keypair()
    body = b'{"item_id":"abc"}'
    with pytest.raises(WebhookVerificationError, match="signature invalid"):
        await verify(body, _sign(attacker_priv, body), FakeClient(real_jwk))


async def test_alg_none_rejected() -> None:
    """Algorithm-confusion: an unsigned token must never be accepted."""
    _, jwk = _keypair()
    body = b'{"item_id":"abc"}'
    forged = jwt.encode(
        {"iat": int(time.time()), "request_body_sha256": hashlib.sha256(body).hexdigest()},
        key="", algorithm="none", headers={"kid": "k1"},
    )
    with pytest.raises(WebhookVerificationError, match="unexpected alg"):
        await verify(body, forged, FakeClient(jwk))


async def test_stale_webhook_rejected() -> None:
    """Replay protection: an old but validly-signed webhook is refused."""
    priv, jwk = _keypair()
    body = b'{"item_id":"abc"}'
    old = _sign(priv, body, iat=int(time.time()) - 3600)
    with pytest.raises(WebhookVerificationError, match="too old"):
        await verify(body, old, FakeClient(jwk))


async def test_missing_sha_claim_rejected() -> None:
    priv, jwk = _keypair()
    body = b'{"item_id":"abc"}'
    token = jwt.encode({"iat": int(time.time())}, priv, algorithm="ES256", headers={"kid": "k1"})
    with pytest.raises(WebhookVerificationError, match="request_body_sha256"):
        await verify(body, token, FakeClient(jwk))


async def test_verification_key_is_cached() -> None:
    """Plaid's key endpoint should not be hit on every webhook."""
    priv, jwk = _keypair()
    client = FakeClient(jwk)
    body = b'{"item_id":"abc"}'
    for _ in range(3):
        await verify(body, _sign(priv, body), client)
    assert client.calls == 1
