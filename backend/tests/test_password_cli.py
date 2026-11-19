"""Setting a password from the machine, because there is no reset email.

Self-hosted software cannot send one: there is no mail server, and the
person locked out is usually the person who owns the box. Physical
access is the authority instead, which is honest -- anyone who can run
a command inside the container could read the database anyway.
"""
from __future__ import annotations

import uuid

from app.core.security import hash_password, verify_password
from app.db.models import User
from app.db.session import SessionLocal
from app.password import MIN_LENGTH, set_password
from tests.conftest import requires_db


def _user(password: str = "original-password") -> str:
    email = f"pw-{uuid.uuid4().hex[:8]}@example.com"
    with SessionLocal() as db:
        db.add(User(email=email, password_hash=hash_password(password),
                    timezone="UTC"))
        db.commit()
    return email


@requires_db
class TestSettingAPassword:
    def test_it_replaces_the_password(self) -> None:
        email = _user()
        assert set_password(email, "a-brand-new-password") == 0
        with SessionLocal() as db:
            u = db.query(User).filter(User.email == email).first()
            assert verify_password("a-brand-new-password", u.password_hash)
            assert not verify_password("original-password", u.password_hash)

    def test_the_address_is_matched_case_insensitively(self) -> None:
        """Addresses are stored lowercase; people do not type them that way."""
        email = _user()
        assert set_password(email.upper(), "a-brand-new-password") == 0

    def test_an_unknown_address_changes_nothing(self) -> None:
        assert set_password("nobody@example.com", "a-brand-new-password") == 1

    def test_a_short_password_is_refused(self) -> None:
        email = _user()
        assert set_password(email, "short") == 1
        with SessionLocal() as db:
            u = db.query(User).filter(User.email == email).first()
            assert verify_password("original-password", u.password_hash), \
                "the old password must still work after a refusal"

    def test_the_minimum_matches_what_the_portal_asks_for(self) -> None:
        """Otherwise the wizard promises one rule and the CLI enforces another."""
        assert MIN_LENGTH == 12


class TestTheCredentialIsNotLeftLyingAround:
    def test_the_password_is_not_an_argument(self) -> None:
        """An argument shows up in `ps` and in shell history."""
        import inspect

        from app import password as mod

        src = inspect.getsource(mod)
        assert "getpass.getpass" in src
        assert "args[1]" not in src, "the password must not come from argv"
