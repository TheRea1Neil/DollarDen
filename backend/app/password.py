"""Set an account's password, from the machine the server runs on.

Self-hosted software has no "email me a reset link": there is no mail
server, and the person locked out is usually the person who owns the
box. So the way back in is physical access, which is the right
authority anyway -- anyone who can run a command inside the container
could read the database regardless.

    docker compose exec api python -m app.password you@example.com

The password is typed at a prompt, never passed as an argument. An
argument would be visible in `ps` to anyone else on the machine and
would be written verbatim into shell history, which is a poor fate for
the credential guarding someone's bank data.
"""
from __future__ import annotations

import getpass
import sys

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import User
from app.db.session import SessionLocal

MIN_LENGTH = 12


def set_password(email: str, password: str) -> int:
    wanted = email.strip().lower()
    if len(password) < MIN_LENGTH:
        print(f"Too short: at least {MIN_LENGTH} characters.", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == wanted)).first()
        if user is None:
            print(f"No account with the address {wanted!r}.", file=sys.stderr)
            print("Run `python -m app.ownership --list` to see them.",
                  file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
        db.commit()
    # Existing sessions keep working: refresh tokens are not derived
    # from the password, so a phone that is already signed in stays
    # signed in. Said out loud because the opposite is often assumed.
    print(f"Password set for {wanted}.")
    print("Devices already signed in stay signed in.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    email = args[0]
    first = getpass.getpass("New password: ")
    again = getpass.getpass("Again: ")
    if first != again:
        print("Those did not match; nothing was changed.", file=sys.stderr)
        return 1
    return set_password(email, first)


if __name__ == "__main__":
    raise SystemExit(main())
