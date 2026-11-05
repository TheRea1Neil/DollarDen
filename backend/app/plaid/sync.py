"""Transaction sync via /transactions/sync.

Two things here are easy to get wrong and expensive to get wrong:

1. SIGN CONVENTION. Plaid returns a POSITIVE amount for money LEAVING the
   account and a negative amount for money arriving. That is the opposite of
   most people's intuition. We normalise once, here, into two non-negative
   columns (outflow_cents / inflow_cents) rather than one signed column, so a
   sign error cannot silently propagate into category balances.

2. PENDING -> POSTED. Plaid removes the pending transaction and adds a posted
   one with a DIFFERENT transaction_id. Treating that as two transactions
   double-counts the spend. We link them and move the allocation instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.budget.treatment import classify
from app.db.models import Account, PlaidItem, Transaction

log = logging.getLogger("nnf.plaid.sync")


class SyncIncomplete(RuntimeError):
    """Raised instead of advancing the cursor past data we could not store."""


def to_cents(amount: float | int | str | Decimal) -> int:
    """Convert a Plaid decimal amount to integer cents.

    Decimal, never float arithmetic: 19.99 * 100 == 1998.9999... in binary
    floating point, and int() of that is 1998. A cent lost per transaction
    compounds into wrong category balances.
    """
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1")))


def split_amount(plaid_amount: float | int | str | Decimal) -> tuple[int, int]:
    """Plaid amount -> (outflow_cents, inflow_cents), both non-negative.

    Positive Plaid amount == money out.
    """
    cents = to_cents(plaid_amount)
    return (cents, 0) if cents >= 0 else (0, -cents)


@dataclass
class SyncStats:
    added: int = 0
    modified: int = 0
    removed: int = 0
    pending_resolved: int = 0
    ai_queued: int = 0
    skipped_unknown_account: int = 0
    categorised: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        base = (
            f"added={self.added} modified={self.modified} removed={self.removed} "
            f"pending_resolved={self.pending_resolved} ai_queued={self.ai_queued}"
        )
        return f"{base} categorised={self.categorised}" if self.categorised else base


async def refresh_accounts(db: Session, client: Any, item: PlaidItem,
                           access_token: str) -> int:
    """Fetch this Item's accounts from Plaid and upsert them.

    Lives here rather than in the route layer because sync depends on it: a
    transaction whose account is unknown cannot be stored.
    """
    # Live balances, not Plaid's cache. This number becomes "how much
    # money you have to allocate", and the cached one was $560 out on a
    # real account -- which is the difference between a budget that
    # reconciles and one that quietly does not.
    #
    # Falls back to the cached endpoint rather than failing: knowing the
    # accounts with a stale balance beats knowing nothing, and this path
    # also runs mid-sync purely to discover a new account.
    try:
        resp = await client.get_balances(access_token)
    except Exception as exc:  # noqa: BLE001
        log.warning("live balance fetch failed, falling back to cached: %s", exc)
        resp = await client.get_accounts(access_token)
    now = datetime.now(UTC)
    n = 0
    for a in resp.get("accounts", []):
        acct = db.scalars(
            select(Account).where(Account.plaid_account_id == a["account_id"])
        ).first()
        is_new_account = acct is None
        if acct is None:
            acct = Account(
                user_id=item.user_id, item_id=item.id, plaid_account_id=a["account_id"]
            )
            db.add(acct)
        bal = a.get("balances") or {}
        acct.name = a.get("name")
        acct.official_name = a.get("official_name")
        acct.mask = a.get("mask")
        acct.type = str(a.get("type") or "")
        acct.subtype = str(a.get("subtype") or "")
        acct.current_cents = to_cents(bal["current"]) if bal.get("current") is not None else None
        acct.available_cents = (
            to_cents(bal["available"]) if bal.get("available") is not None else None
        )
        acct.balance_as_of = now

        # Linking a bank pulls in everything -- mortgage, 401k, auto loan --
        # and most of it is noise on a budgeting home screen. Default to
        # showing day-to-day accounts only; the user can change it. Only set
        # on first sight, so a later refresh never overrides their choice.
        if is_new_account:
            acct.is_tracked = (acct.type or "") in {"depository", "credit"}

        n += 1
    db.flush()
    return n


def _account_map(db: Session, item: PlaidItem) -> dict[str, Account]:
    rows = db.scalars(select(Account).where(Account.item_id == item.id)).all()
    return {a.plaid_account_id: a for a in rows}


def _upsert(db: Session, item: PlaidItem, accounts: dict[str, Account],
            raw: dict[str, Any], stats: SyncStats) -> Transaction | None:
    acct = accounts.get(raw["account_id"])
    if acct is None:
        # An account we have not stored yet (e.g. added at the bank since the
        # last /accounts/get). Skip rather than orphan the row; the next
        # account refresh picks it up.
        stats.skipped_unknown_account += 1
        return None

    out_c, in_c = split_amount(raw["amount"])
    txn = db.scalars(
        select(Transaction).where(Transaction.plaid_transaction_id == raw["transaction_id"])
    ).first()

    is_new = txn is None
    if txn is None:
        txn = Transaction(
            user_id=item.user_id,
            account_id=acct.id,
            plaid_transaction_id=raw["transaction_id"],
        )
        db.add(txn)

    txn.outflow_cents = out_c
    txn.inflow_cents = in_c
    txn.iso_currency_code = raw.get("iso_currency_code") or "USD"
    txn.date = datetime.strptime(raw["date"], "%Y-%m-%d").date()
    txn.name = raw.get("name") or ""
    txn.merchant_name = raw.get("merchant_name")
    txn.pending = bool(raw.get("pending"))
    pfc = raw.get("personal_finance_category") or {}
    txn.pfc_primary = pfc.get("primary")
    txn.pfc_detailed = pfc.get("detailed")
    txn.removed_at = None

    # Only classify on first sight. Re-classifying would silently undo a
    # user's manual correction every time Plaid re-sends the transaction.
    if is_new:
        txn.treatment = classify(
            pfc_primary=txn.pfc_primary,
            pfc_detailed=txn.pfc_detailed,
            name=txn.name,
            merchant_name=txn.merchant_name,
            account_type=acct.type,
            outflow_cents=out_c,
            inflow_cents=in_c,
        ).value

    # Plaid tells us which pending transaction this posted one replaces.
    ref = raw.get("pending_transaction_id")
    if ref and not txn.pending:
        # Flush before touching txn.id. Transaction.id is a PYTHON-side
        # uuid4 default that SQLAlchemy only applies at flush, so on a
        # newly added row it is still None here. _move_allocations
        # re-points the pending charge's allocations onto this row BY
        # ID, and with None it set every one of them to NULL instead.
        #
        # That detached them from any transaction, and an allocation
        # with neither a transaction nor a paycheck reads as a transfer
        # between envelopes -- so the spending vanished from analytics
        # AND the posted charge returned as an unreviewed item, inviting
        # the user to file the same money a second time. It has to come
        # after the column assignments above, which fill NOT NULL
        # fields like date.
        db.flush()
        prior = db.scalars(
            select(Transaction).where(Transaction.plaid_transaction_id == ref)
        ).first()
        if prior is not None and prior.id != txn.id:
            _move_allocations(db, prior, txn)
            _move_paycheck(db, prior, txn)
            prior.pending_replaced_by = txn.plaid_transaction_id
            prior.removed_at = datetime.now(UTC)
            stats.pending_resolved += 1

    if is_new:
        stats.added += 1
    else:
        stats.modified += 1
    return txn


def _move_allocations(db: Session, old: Transaction, new: Transaction) -> None:
    """Re-point allocations from a pending transaction onto its posted twin.

    Moving rather than re-creating keeps the ledger append-only in spirit and
    avoids double-counting. If the amount changed (a tip added to a restaurant
    charge is the common case), a correction entry reconciles the difference.
    """
    from app.db.models import Allocation

    allocs = db.scalars(select(Allocation).where(Allocation.transaction_id == old.id)).all()
    if not allocs:
        return

    old_total = old.outflow_cents - old.inflow_cents
    new_total = new.outflow_cents - new.inflow_cents

    for a in allocs:
        a.transaction_id = new.id

    delta = new_total - old_total
    if delta != 0:
        # Attribute the change to the largest existing allocation.
        target = max(allocs, key=lambda a: abs(a.amount_cents))
        db.add(
            Allocation(
                user_id=new.user_id,
                category_id=target.category_id,
                transaction_id=new.id,
                amount_cents=-delta,
                source="correction",
                note=f"pending->posted amount changed by {delta} cents",
            )
        )
        log.info("pending->posted delta %s cents on %s", delta, new.plaid_transaction_id)


def _queue_ai_categorisation(db: Session, user_id: uuid.UUID,
                             txns: list[Transaction]) -> int:
    """Ask the model about anything the deterministic pass could not file.

    One job per charge rather than one for the batch: each is a separate
    decision, the queue already defers around the GPU and the quiet
    window per job, and a single failure then costs one charge instead
    of the whole sync.

    Nothing is queued when the user has no AI configured. ADR 0003: the
    app works fully with the model switched off, and queueing jobs that
    can only fail would turn that into a pile of errors.
    """
    from app.ai.jobs import enqueue
    from app.core.config import get_settings
    from app.db.models import ReviewItem, User

    settings = get_settings()
    user = db.get(User, user_id)
    configured = (user and user.ai_base_url) or settings.ai_provider in {"deck", "custom"}
    if not configured:
        return 0

    ids = [t.id for t in txns if t.id is not None]
    if not ids:
        return 0

    open_reviews = db.scalars(
        select(ReviewItem).where(
            ReviewItem.user_id == user_id,
            ReviewItem.transaction_id.in_(ids),
            ReviewItem.resolved_at.is_(None),
        )
    ).all()

    for rv in open_reviews:
        enqueue(db, user_id, "auto_categorise",
                {"transaction_id": str(rv.transaction_id)})
    db.flush()
    return len(open_reviews)


def _move_paycheck(db: Session, old: Transaction, new: Transaction) -> None:
    """Carry a split paycheck onto its posted twin.

    Splitting a deposit while it is pending is allowed, which is only
    safe because of this. A paycheck's allocations hang off the
    PAYCHECK, not the transaction, so _move_allocations does not touch
    them -- and without re-pointing the paycheck itself, the posted
    copy would look like an unrecorded deposit and could be split a
    second time. That is the one double-count that corrupts every
    envelope at once rather than one.

    A payroll amount rarely changes between pending and posted, but it
    can. The difference goes to the Unallocated sink rather than being
    spread back over the user's split: guessing which envelope should
    absorb it would silently rewrite a decision they made, and
    Unallocated is the one place money is SUPPOSED to sit while it has
    no purpose. It is visible there, and it is theirs to place.
    """
    from app.budget.ledger import Entry, post
    from app.budget.reconcile import ensure_unallocated
    from app.db.models import Paycheck

    pc = db.scalars(
        select(Paycheck).where(Paycheck.transaction_id == old.id)
    ).first()
    if pc is None:
        return

    pc.transaction_id = new.id

    old_gross = old.inflow_cents - old.outflow_cents
    new_gross = new.inflow_cents - new.outflow_cents
    delta = new_gross - old_gross
    if delta == 0:
        log.info("paycheck %s followed its posted transaction", pc.id)
        return

    pc.gross_cents = new_gross
    sink = ensure_unallocated(db, pc.user_id)
    post(
        db, pc.user_id,
        [Entry(sink.id, delta, "correction", None,
               f"paycheck posted {delta / 100:+,.2f} against the pending amount")],
        paycheck_id=pc.id,
    )
    log.info("paycheck %s posted %s cents different; delta to Unallocated",
             pc.id, delta)


async def sync_item(db: Session, client: Any, item: PlaidItem,
                    access_token: str, max_pages: int = 50,
                    refresh_balances: bool = True) -> SyncStats:
    """Drain /transactions/sync for one Item, and refresh its balances.

    The cursor is advanced in memory and committed exactly once, together with
    the data it describes. Committing the cursor before the rows would lose
    transactions permanently on a crash -- Plaid will not resend them.

    Balances are refreshed here because transactions alone are not what the
    user is looking at. The budget's headline figure is how much money
    there actually is -- cash minus card debt -- and that comes from
    `accounts`, which only `refresh_accounts` writes. Without this, both
    the webhook and the manual Sync button updated the transaction list
    while the balances stayed frozen at whatever they were when the bank
    was first linked.
    """
    stats = SyncStats()
    cursor = item.sync_cursor
    accounts = _account_map(db, item)
    pages = 0
    refreshed = False
    new_txns: list[Transaction] = []

    # An Item with no accounts stores nothing: every transaction would be
    # skipped as "unknown account" while the cursor advanced past it, losing
    # the data permanently. Happens after a restore, since deleting an Item
    # cascades to its accounts.
    if not accounts:
        log.info("no accounts for %s; fetching before sync", item.plaid_item_id)
        await refresh_accounts(db, client, item, access_token)
        accounts = _account_map(db, item)
        refreshed = True

    while pages < max_pages:
        pages += 1
        page = await client.transactions_sync(access_token, cursor)

        before = stats.skipped_unknown_account
        for raw in page.get("added", []):
            t = _upsert(db, item, accounts, raw, stats)
            if t is not None:
                new_txns.append(t)
        for raw in page.get("modified", []):
            _upsert(db, item, accounts, raw, stats)

        if stats.skipped_unknown_account > before and not refreshed:
            # A new account appeared at the bank mid-sync. Fetch it and
            # reprocess this page rather than dropping the transactions.
            log.info("unknown account during sync; refreshing and retrying page")
            await refresh_accounts(db, client, item, access_token)
            accounts = _account_map(db, item)
            refreshed = True
            stats.skipped_unknown_account = before
            for raw in page.get("added", []):
                t = _upsert(db, item, accounts, raw, stats)
                if t is not None:
                    new_txns.append(t)
            for raw in page.get("modified", []):
                _upsert(db, item, accounts, raw, stats)

        if stats.skipped_unknown_account > before:
            # Still unstorable after a refresh. Advancing the cursor here
            # would discard these transactions for good, so stop instead.
            db.rollback()
            raise SyncIncomplete(
                f"{stats.skipped_unknown_account - before} transactions reference "
                f"accounts Plaid did not return; refusing to advance the cursor"
            )

        for rem in page.get("removed", []):
            txn = db.scalars(
                select(Transaction).where(
                    Transaction.plaid_transaction_id == rem["transaction_id"]
                )
            ).first()
            if txn is not None and txn.removed_at is None:
                txn.removed_at = datetime.now(UTC)
                stats.removed += 1

        cursor = page.get("next_cursor") or cursor
        if not page.get("has_more"):
            break
    else:
        stats.errors.append(f"stopped after {max_pages} pages; more data remains")

    # Categorise what just arrived, before committing, so transactions and
    # their allocations land in the same database transaction. A crash in
    # between would otherwise leave spending recorded but unallocated.
    if new_txns:
        from app.budget.categorize import categorise_batch

        # Flush first. Transaction.id uses a PYTHON-side uuid4 default, which
        # SQLAlchemy only applies at flush time -- reading .id before this
        # yields None, and categorisation silently sees nothing.
        db.flush()
        try:
            stats.categorised = categorise_batch(db, item.user_id, new_txns)
        except Exception as exc:
            # Categorisation is best-effort: never lose transaction data over
            # it. Anything missed shows up unallocated and can be re-run.
            log.warning("categorisation failed during sync: %s", exc)
            stats.errors.append(f"categorisation: {exc}")

        try:
            stats.ai_queued = _queue_ai_categorisation(db, item.user_id, new_txns)
        except Exception as exc:  # noqa: BLE001
            # Queuing is a convenience. Anything not queued simply waits
            # in review, which is where it already is.
            log.warning("could not queue AI categorisation: %s", exc)

    item.sync_cursor = cursor
    item.last_synced_at = datetime.now(UTC)
    item.status = "healthy"
    db.commit()          # rows, allocations and cursor land together

    # After the cursor is safely committed, never before: a failure here
    # must not cost transactions. Stale balances are recoverable, lost
    # transactions are not, because Plaid will not resend them.
    if refresh_balances and not refreshed:
        try:
            await refresh_accounts(db, client, item, access_token)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("balance refresh failed after sync: %s", exc)
            stats.errors.append(f"balance refresh: {exc}")

    log.info("sync %s: %s", item.plaid_item_id, stats)
    return stats
