"""Encrypted token backup.

The whole value of this feature rests on one property: what lands on the
phone must be useless to anyone who does not hold TOKEN_ENCRYPTION_KEY.
These tests attack that property rather than confirming the happy path.
"""
from __future__ import annotations

import base64

import pytest

from app.core.security import decrypt_token, encrypt_token, key_fingerprint

# A *valid* Fernet key that is not ours: 32 random bytes, urlsafe-base64.
# An arbitrary base64 string is rejected by Fernet before decryption is even
# attempted, which would make these tests pass for the wrong reason.
OTHER_KEY = "DRIIjChu06Uh-AtrR6tU3B999m87EvbFq27AJT-8j_A="


@pytest.fixture(autouse=True)
def _isolate_settings_cache():
    """Settings are lru_cached, so a key swapped in by one test leaks.

    Clearing on both sides keeps each test honest.
    """
    import app.core.config as cfg

    cfg.get_settings.cache_clear()
    yield
    cfg.get_settings.cache_clear()


class TestBlobIsOpaque:
    def test_ciphertext_does_not_contain_the_token(self) -> None:
        token = "access-sandbox-9f5524a0-36c1-4d2e-8f1a-000000000000"
        blob = encrypt_token(token)
        assert token.encode() not in blob
        assert b"access-sandbox" not in blob

    def test_base64_transport_form_is_also_opaque(self) -> None:
        """What actually travels to the phone must leak nothing either."""
        token = "access-sandbox-9f5524a0-36c1-4d2e-8f1a-000000000000"
        b64 = base64.b64encode(encrypt_token(token)).decode()
        assert "access-sandbox" not in b64
        assert token not in b64
        # And it must survive the round trip intact.
        assert decrypt_token(base64.b64decode(b64)) == token

    def test_a_different_key_cannot_open_the_blob(self, monkeypatch) -> None:
        """The core guarantee: stolen phone + no key == nothing."""
        import app.core.config as cfg

        token = "access-sandbox-deadbeef"
        blob = encrypt_token(token)

        # Swap in an attacker's (valid but different) key and try again.
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", OTHER_KEY)
        cfg.get_settings.cache_clear()

        with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY has"):
            decrypt_token(blob)

    def test_ciphertext_differs_between_encryptions(self) -> None:
        """Fernet is nonce-based, so identical tokens must not look identical."""
        token = "access-sandbox-same-token"
        assert encrypt_token(token) != encrypt_token(token)
        assert decrypt_token(encrypt_token(token)) == token


class TestKeyFingerprint:
    def test_is_stable_for_the_same_key(self) -> None:
        assert key_fingerprint() == key_fingerprint()

    def test_does_not_reveal_the_key(self) -> None:
        import os

        fp = key_fingerprint()
        key = os.environ["TOKEN_ENCRYPTION_KEY"]
        assert fp not in key
        assert key[:16] != fp
        assert len(fp) == 16

    def test_changes_with_the_key(self, monkeypatch) -> None:
        import app.core.config as cfg

        first = key_fingerprint()
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", OTHER_KEY)
        cfg.get_settings.cache_clear()
        assert key_fingerprint() != first
