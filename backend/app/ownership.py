"""Name the owner of an existing instance.

A fresh install has no accounts, so the first-run wizard creates the
owner and this is never needed. This exists for the other case: a
server that was already running before roles existed, where there is
no safe way to work out automatically which of the accounts belongs to
the person who built it.

    docker compose exec api python -m app.ownership you@example.com
    docker compose exec api python -m app.ownership --list

Deliberately a command rather than an endpoint. Granting ownership is
the one action that cannot be authorised by someone who is already an
owner, because the whole point is that there is not one yet -- so it
is restricted to whoever can reach the machine itself.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.db.models import User
from app.db.session import SessionLocal


def _list() -> int:
    with SessionLocal() as db:
        users = db.scalars(select(User).order_by(User.created_at)).all()
        if not users:
            print("No accounts yet. Open the server in a browser to set it up.")
            return 0
        print(f"{'role':<8} {'created':<17} email")
        for u in users:
            print(f"{u.role:<8} {u.created_at:%Y-%m-%d %H:%M}  {u.email}")
    return 0


def promote(email: str) -> int:
    wanted = email.strip().lower()
    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == wanted)).first()
        if user is None:
            print(f"No account with the address {wanted!r}.", file=sys.stderr)
            print("Run with --list to see them.", file=sys.stderr)
            return 1
        if user.role == "owner":
            print(f"{wanted} is already the owner.")
            return 0
        user.role = "owner"
        db.commit()
        print(f"{wanted} is now an owner of this instance.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--list":
        return _list()
    return promote(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
