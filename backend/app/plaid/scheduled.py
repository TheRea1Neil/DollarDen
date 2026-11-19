"""Pull from Plaid on a schedule, not only when a webhook arrives.

Two reasons, and the second is the one that matters long term.

**A plain-HTTP install has no webhooks at all.** Plaid will not post to
an unencrypted URL, and a home server without a domain and a
certificate does not have one. Without a schedule such an install only
updates when someone pulls down on the screen.

**Webhooks get missed even when they work.** The server is down for an
update, a request is dropped, a reverse-proxy path changes and starts
404ing — which has happened here, and the only evidence was a run of
404s in the proxy log while Plaid went on reporting the connection as
healthy. Transactions simply stop arriving. A schedule is the backstop
that turns a silent, indefinite outage into a delay of one interval.

It is deliberately NOT paused during quiet hours. Those exist so the
inference machine is left asleep overnight; this makes a network call
and wakes nothing. Transactions that arrive at 3am are categorised when
the AI is allowed to run again, which the job queue already handles.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.models import PlaidItem, User
from app.db.session import SessionLocal
from app.plaid.client import PlaidClient
from app.plaid.sync import refresh_items

log = logging.getLogger("nnf.plaid.scheduled")

# The floor, whatever the configuration says.
#
# Each pass asks Plaid to re-read every connected bank. Plaid's own
# limit is generous enough that this is not about their quota -- it is
# about not turning a mistyped setting into a request every second
# against someone's bank, forever, unattended.
MIN_INTERVAL_MINUTES = 2

# How often to wake and consider working. Small, so that changing the
# interval takes effect within the minute rather than after the old one
# finally elapses.
_TICK_SECONDS = 30.0


def interval_minutes(s: Settings) -> int:
    """The configured interval, floored. Never below MIN_INTERVAL_MINUTES."""
    return max(MIN_INTERVAL_MINUTES, int(s.plaid_sync_interval_minutes))


async def run_once(settings: Settings | None = None) -> dict[str, int]:
    """One pass over every connected item, for every user.

    Returns a tally for the logs. Failures are counted and swallowed:
    one broken connection must not stop the others, and must not stop
    the schedule.
    """
    s = settings or get_settings()
    totals = {"users": 0, "synced": 0, "skipped": 0, "added": 0, "nudged": 0,
              "errors": 0}

    with SessionLocal() as db:
        # Grouped by user because an item belongs to one, and because a
        # per-user Plaid environment is coming (docs/17).
        user_ids = list(db.scalars(select(PlaidItem.user_id).distinct()).all())
        for uid in user_ids:
            items = list(db.scalars(
                select(PlaidItem).where(PlaidItem.user_id == uid)).all())
            if not items:
                continue
            # Built for the account, not the instance: sandbox and
            # real accounts share a server, and a production secret
            # against the sandbox host authenticates nothing.
            account = db.get(User, uid)
            client = PlaidClient.for_user(s, account) if account else PlaidClient(s)
            try:
                tally = await refresh_items(db, client, items, s, skip_dead=True)
                db.commit()
            except Exception:  # noqa: BLE001
                log.exception("scheduled sync failed for user %s", uid)
                db.rollback()
                totals["errors"] += 1
                continue
            totals["users"] += 1
            totals["synced"] += tally.synced
            totals["skipped"] += tally.skipped
            totals["added"] += tally.added
            totals["nudged"] += tally.nudged
            totals["errors"] += len(tally.errors)

    return totals


async def sync_loop() -> None:
    """Run `run_once` on the configured interval, forever.

    Woken every `_TICK_SECONDS` rather than sleeping for the whole
    interval, so that changing the setting takes effect promptly
    instead of after the previous long sleep expires.

    Assumes a single instance, like the AI job worker above it. Two
    replicas would mean two schedules and double the bank traffic; if
    this is ever run more than once, the claim needs to move into the
    database.
    """
    log.info("Plaid sync schedule started")
    last_run: datetime | None = None

    while True:
        try:
            s = get_settings()
            if not s.plaid_sync_enabled:
                await asyncio.sleep(_TICK_SECONDS)
                continue

            due = last_run is None or (
                (datetime.now(UTC) - last_run).total_seconds()
                >= interval_minutes(s) * 60
            )
            if due:
                last_run = datetime.now(UTC)
                totals = await run_once(s)
                if totals["added"] or totals["errors"]:
                    log.info("scheduled sync: %s", totals)
                else:
                    log.debug("scheduled sync: %s", totals)
        except asyncio.CancelledError:
            log.info("Plaid sync schedule stopping")
            raise
        except Exception:  # noqa: BLE001
            # The loop must never die: a crash here would silently end
            # every future pull, which is exactly the failure this whole
            # module exists to prevent.
            log.exception("scheduled sync loop error")

        await asyncio.sleep(_TICK_SECONDS)
