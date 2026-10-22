"""One-off backfill: classify transactions that predate the treatment column.

Classification normally runs once, when a transaction is first seen, so that
re-syncing cannot silently undo a user's manual correction. Rows that existed
before migration 0003 therefore all default to `spend` and need a pass.

Only touches rows still marked `spend` with no manual history, so running it
twice is harmless.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.budget.ledger import allocations_for_transaction, reverse
from app.budget.treatment import Treatment, classify
from app.db.models import Account, ReviewItem, Transaction
from app.db.session import SessionLocal

dry_run = "--apply" not in sys.argv

with SessionLocal() as db:
    accounts = {a.id: a for a in db.scalars(select(Account)).all()}
    rows = db.scalars(
        select(Transaction).where(
            Transaction.treatment == Treatment.SPEND.value,
            Transaction.removed_at.is_(None),
        )
    ).all()

    changed = 0
    for t in rows:
        acct = accounts.get(t.account_id)
        new = classify(
            pfc_primary=t.pfc_primary,
            pfc_detailed=t.pfc_detailed,
            name=t.name,
            merchant_name=t.merchant_name,
            account_type=acct.type if acct else None,
            outflow_cents=t.outflow_cents,
            inflow_cents=t.inflow_cents,
        )
        if new is Treatment.SPEND:
            continue

        changed += 1
        print(f"  {t.date}  {t.name[:38]:<40} {t.outflow_cents:>8} -> {new.value}")
        if dry_run:
            continue

        t.treatment = new.value
        # Reverse anything it already allocated, so balances stop reflecting
        # a transaction that no longer counts.
        existing = allocations_for_transaction(db, t)
        if existing:
            reverse(db, t.user_id, existing, f"backfill: reclassified as {new.value}")
        open_review = db.scalars(
            select(ReviewItem).where(
                ReviewItem.transaction_id == t.id, ReviewItem.resolved_at.is_(None)
            )
        ).first()
        if open_review is not None:
            db.delete(open_review)

    print(f"\n  {changed} of {len(rows)} transactions reclassified"
          f"{' (dry run — pass --apply)' if dry_run else ''}")
    if not dry_run:
        db.commit()
        print("  committed")
