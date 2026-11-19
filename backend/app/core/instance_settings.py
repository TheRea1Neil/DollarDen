"""Reading and writing the settings an owner may change.

Deliberately a short, explicit allow-list rather than "any field on
Settings". Two reasons. Some settings cannot be changed at runtime
without the server losing its footing -- the database URL it is
already connected to, the key its stored tokens are encrypted with --
and nothing good comes of a web form that can set them. And an
allow-list is a decision someone made, whereas reflection over every
field is a decision nobody made, which later turns out to include
something it should not.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger("nnf.settings")

# Settings the admin portal may change, by their Settings field name.
EDITABLE: dict[str, str] = {
    # Plaid
    "plaid_client_id": "str",
    "plaid_secret": "secret",
    "plaid_env": "str",
    "plaid_webhook_url": "str",
    "plaid_redirect_uri": "str",
    "plaid_android_package_name": "str",
    # Pulling
    "plaid_sync_enabled": "bool",
    "plaid_sync_interval_minutes": "int",
    # AI
    "ai_provider": "str",
    "ai_host_url": "str",
    "ai_api_base_url": "str",
    "ai_api_key": "secret",
    "ai_model_text": "str",
    "ai_model_vision": "str",
    "ai_confidence_threshold": "float",
    "ai_request_timeout_seconds": "int",
    # Quiet hours
    "ai_quiet_enabled": "bool",
    "ai_quiet_timezone": "str",
    "ai_quiet_start_hour": "int",
    "ai_quiet_end_hour": "int",
    # Accounts
    "allow_open_registration": "bool",
}

# Settings with a fixed set of valid values.
#
# Declared here rather than in the portal's JavaScript so the list has
# one home: the API already refuses an invalid value, and this lets the
# page offer a menu instead of a text box for the same rule. A typo in
# `plaid_env` is not a small thing -- it decides whether a connection
# attempt spends one of a finite number of real bank connections.
CHOICES: dict[str, list[str]] = {
    "plaid_env": ["sandbox", "production"],
    "ai_provider": ["none", "managed", "custom", "openai"],
}


# Never stored, never shown, never settable here.
#
# TOKEN_ENCRYPTION_KEY protects everything else in the database,
# including the secrets in this very table, so it cannot live in it.
# The rest are what the process needs before it can read a database at
# all.
NEVER: frozenset[str] = frozenset({
    "token_encryption_key", "jwt_signing_key",
    "postgres_host", "postgres_port", "postgres_db",
    "postgres_user", "postgres_password",
})


def is_secret(key: str) -> bool:
    return EDITABLE.get(key) == "secret"


def _coerce(key: str, raw: str) -> Any:
    kind = EDITABLE.get(key, "str")
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw


def validate(values: dict[str, Any]) -> dict[str, str]:
    """Check values before storing them. Raises ValueError on the first bad one.

    Validation has to happen here, on the way in, because
    `load_overrides` is deliberately forgiving: it skips anything it
    cannot read so that one bad row can never stop the server. That
    forgiveness means a mistyped number would be accepted, silently
    ignored forever after, and the portal would go on displaying a
    value the server had never adopted.
    """
    out: dict[str, str] = {}
    for key, value in values.items():
        if key not in EDITABLE:
            continue
        text = "" if value is None else str(value)
        if text == "":
            out[key] = text
            continue
        try:
            _coerce(key, text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}: {text!r} is not a valid {EDITABLE[key]}") from exc
        out[key] = text
    return out


def load_overrides(db: Session, key: str | None = None) -> dict[str, Any]:
    """Stored settings, decrypted and typed, ready to overlay.

    `key` is the encryption key, passed in by `get_settings()` because
    this runs inside it: looking it up would re-enter `get_settings()`,
    and `lru_cache` resolves that by keeping the inner call's result
    and throwing the outer one away. Every stored setting then
    silently vanishes.

    Anything unreadable is skipped loudly rather than raising: a single
    bad row must not be able to stop the server from starting, because
    the way out of that would be the very portal it just broke.
    """
    from app.core.security import decrypt_token
    from app.db.models import InstanceSetting

    out: dict[str, Any] = {}
    for row in db.scalars(select(InstanceSetting)).all():
        if row.key not in EDITABLE or row.key in NEVER:
            continue
        try:
            raw = (
                decrypt_token(row.value_encrypted, key)
                if row.value_encrypted is not None
                else row.value
            )
            if raw is None:
                continue
            out[row.key] = _coerce(row.key, raw)
        except Exception:  # noqa: BLE001
            log.exception("ignoring unreadable setting %r", row.key)
    return out


def save(db: Session, values: dict[str, Any], *, by: str | None = None) -> list[str]:
    """Write settings. Returns the keys actually changed.

    An empty string clears a setting, falling back to the environment
    rather than storing a blank -- which is what someone emptying a
    field means, and the only way back to the `.env` value.
    """
    from app.core.security import encrypt_token
    from app.db.models import InstanceSetting

    changed: list[str] = []
    for key, value in values.items():
        if key not in EDITABLE or key in NEVER:
            continue
        row = db.get(InstanceSetting, key)
        text = "" if value is None else str(value)

        if text == "":
            if row is not None:
                db.delete(row)
                changed.append(key)
            continue

        if row is None:
            row = InstanceSetting(key=key)
            db.add(row)
        if is_secret(key):
            row.value, row.value_encrypted = None, encrypt_token(text)
        else:
            row.value, row.value_encrypted = text, None
        row.updated_by = by
        changed.append(key)

    db.flush()
    return changed
