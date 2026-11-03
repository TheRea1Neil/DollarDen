"""Describe every table and the confusing columns, in the database.

docs/schema/schema.sql is regenerated from the live database by
scripts/dump-schema.sh, so comments written into that file by hand
would be wiped by the next run -- silently, which is the worst way to
lose documentation. Putting them in the database instead means pg_dump
emits them, the dump stays authoritative, and `\\d+ transactions` in
psql explains itself too.

Column comments are deliberately not exhaustive. They cover the places
where a reader would otherwise guess wrong: the three-way meaning of an
allocations row, the two sign conventions, the fields that must never
be edited, and amounts that are cents rather than dollars.

Revision ID: 0010_schema_comments
Revises: 0009_deferred_jobs
"""
from __future__ import annotations

from alembic import op

revision = "0010_schema_comments"
down_revision = "0009_deferred_jobs"
branch_labels = None
depends_on = None


TABLES: dict[str, str] = {
    "users": (
        "One person with a login. Everything else in this database hangs off "
        "a user_id and cascades on delete, so removing a user removes their "
        "entire budget. Also holds that user's own AI provider settings, so "
        "two users on the same server need not share a model host."
    ),
    "sessions": (
        "A refresh-token session, one row per signed-in device. Only a HASH "
        "of the refresh token is stored, never the token. Signing in on a "
        "second phone adds a row; it does not move anything, which is why "
        "both devices see the same budget."
    ),
    "plaid_items": (
        "One connection to one financial institution. THIS is the row that "
        "consumes a Plaid credit, and on the Trial plan the cap is 10 for "
        "the lifetime of the account -- /item/remove does NOT give a slot "
        "back. Never delete a row here to 'clean up'. The access_token is "
        "encrypted at rest with TOKEN_ENCRYPTION_KEY, which lives in .env "
        "and not in this database, so a stolen dump yields nothing usable. "
        "Re-authentication goes through Link's update mode, which reuses "
        "this row and costs nothing."
    ),
    "accounts": (
        "One account inside a connection: a checking account, a credit card. "
        "is_tracked is the user's choice of which accounts this budget "
        "covers -- loans, pensions and investments are usually left out, and "
        "untracked accounts contribute nothing to the allocatable total."
    ),
    "transactions": (
        "An immutable fact from the bank, mirrored from Plaid and never "
        "edited. What it MEANS for the budget is not stored here -- that "
        "lives in allocations, so a charge can be refiled without rewriting "
        "history. Direction is stored as two unsigned columns, "
        "outflow_cents and inflow_cents, rather than one signed amount."
    ),
    "categories": (
        "An envelope: a balance, not a report line. 'Groceries' holds a "
        "number of dollars that goes up when funded and down when spent. "
        "Archived rather than deleted, so historical allocations still name "
        "something. Every user has exactly one system 'Unallocated' sink "
        "that cannot be archived."
    ),
    "category_rules": (
        "Deterministic merchant matchers that file a charge automatically, "
        "skipping the review queue. No model involved: a matcher either "
        "matches the merchant string or it does not. priority breaks ties "
        "when two rules both match."
    ),
    "split_plans": (
        "A named, VERSIONED set of funding rules for dividing a paycheck. "
        "Editing a plan writes a new version rather than mutating the old "
        "one, so a paycheck recorded six months ago can still explain "
        "exactly how it was split. Exactly one version is active."
    ),
    "split_rules": (
        "One rule inside a plan: percent, fixed, refill_to, conditional or "
        "remainder. order_index IS the funding order, and it matters -- a "
        "large refill_to placed above rent will take the whole paycheck and "
        "starve everything below it. Exactly one remainder rule, and it must "
        "be last. params is rule-type specific and its required keys are "
        "enforced by the engine, not by the column."
    ),
    "paychecks": (
        "An income event that has been divided across envelopes. Links to "
        "the transaction it came from (null for a hand-entered one) and to "
        "the exact split_plan VERSION that was applied, which is what lets a "
        "historical paycheck still explain itself. The money it moved is in "
        "allocations, not here."
    ),
    "allocations": (
        "The ledger, and the single source of truth for every category "
        "balance -- a category's balance is the SUM of its rows here, never "
        "a stored number. APPEND-ONLY: a mistake is corrected with an "
        "opposing entry, never by editing or deleting, so history always "
        "explains itself. Which pair of foreign keys is set determines what "
        "the row MEANS; see the comments on paycheck_id and transaction_id."
    ),
    "expense_types": (
        "The second, orthogonal taxonomy: what a charge WAS, independent of "
        "which envelope paid for it. Lunch from 'Personal Discretionary' and "
        "lunch from 'Date Fund' are different envelopes and the same expense "
        "type, which is how 'how much on fast food this month' is answerable "
        "across pots. Merged rather than deleted, via merged_into_id, so old "
        "allocations keep resolving."
    ),
    "item_memory": (
        "Where one line item off a receipt was filed last time, so the next "
        "receipt can pre-fill it. Keyed per shop with a global fallback: "
        "cottage cheese at one supermarket may belong somewhere different "
        "from cottage cheese at another."
    ),
    "review_items": (
        "The review queue: transactions that no deterministic matcher could "
        "file, waiting on a decision. reason says why it landed here and "
        "suggestions holds any AI proposal, which is advisory only -- "
        "nothing is filed without a matcher hit or an explicit choice. "
        "Resolved rows are kept, not deleted, so a past decision can teach "
        "the matcher later."
    ),
    "receipts": (
        "An itemisation of a single transaction: one bank charge split "
        "across several envelopes. Entirely optional -- the whole charge is "
        "categorised first and itemising is a later refinement. line_items "
        "holds the extracted lines; they are reconciled against the bank's "
        "amount so every cent on the receipt is accounted for."
    ),
    "ai_jobs": (
        "The queue. Every AI call is a persisted job rather than a blocking "
        "request, because the model host is a gaming PC that sleeps and may "
        "be busy. A job carries its own request and result, so what was "
        "asked and what came back are both still inspectable afterwards. "
        "status 'deferred' plus not_before is how a job waits for the GPU "
        "instead of failing."
    ),
    "user_goals": (
        "What the user says they are trying to do with their money, in their "
        "own words. Free prose, read as context by five AI tasks -- the "
        "budget summary, the advice agent, and the three that design or "
        "refine categories."
    ),
    "alembic_version": (
        "Alembic's own bookkeeping: which migration this database is at. "
        "Not application data."
    ),
}


COLUMNS: dict[str, str] = {
    "allocations.amount_cents": (
        "CATEGORY-RELATIVE and signed: positive means this envelope GAINED "
        "money, negative means it lost it. This is the opposite convention "
        "from the review queue, which is budget-relative. Exactly one place "
        "translates between the two -- Activity.budgetSignedCents in the app. "
        "Do not add a second."
    ),
    "allocations.paycheck_id": (
        "Set, with transaction_id null: this row is FUNDING, money arriving "
        "in the envelope from a paycheck."
    ),
    "allocations.transaction_id": (
        "Set, with paycheck_id null: this row is SPENDING, a real charge "
        "filed against the envelope. Both null means a TRANSFER between "
        "envelopes -- which is movement, not spending, and analytics must "
        "not count it as such."
    ),
    "allocations.expense_type_id": (
        "Optional second axis: what the charge was, as opposed to which "
        "envelope paid. Null means not yet labelled, which is reported "
        "honestly as 'Untyped' rather than hidden."
    ),
    "plaid_items.access_token_encrypted": (
        "Fernet ciphertext, never a usable token. The key is "
        "TOKEN_ENCRYPTION_KEY in .env, deliberately NOT in this database, so "
        "restoring a dump without that key gives back a budget and zero "
        "working bank connections."
    ),
    "plaid_items.sync_cursor": (
        "Opaque Plaid cursor for /transactions/sync. Clearing it replays "
        "history; the sync is idempotent, so that is recoverable but slow."
    ),
    "transactions.outflow_cents": (
        "Money leaving, unsigned. Direction is two columns rather than one "
        "signed amount so a refund is never a negative expense by accident; "
        "exactly one of outflow_cents and inflow_cents is non-zero."
    ),
    "transactions.inflow_cents": (
        "Money arriving, unsigned: a deposit, a refund, a paycheck."
    ),
    "transactions.treatment": (
        "What this charge MEANS for the budget. 'spend' allocates against a "
        "category; a credit-card payment or an internal transfer must NOT, "
        "because the card purchase already drew the category down and "
        "counting the payment too would double-count the same money. The "
        "consequence worth stating plainly: category balances are COMMITTED "
        "money, not cash in the bank."
    ),
    "transactions.pending_replaced_by": (
        "A pending charge is later replaced by a settled one with a "
        "different id. This points at the replacement so the pair is not "
        "counted twice."
    ),
    "split_rules.params": (
        "Rule-type specific JSON. percent needs rate; fixed needs "
        "amount_cents; refill_to needs target_cents; conditional needs when "
        "and then. Missing keys are rejected by the split engine with a "
        "named error -- a model will omit any property a schema does not "
        "mark required, so this is expected input rather than an impossible "
        "state."
    ),
    "split_rules.order_index": (
        "The funding order, and it is load-bearing. Rules run top to bottom "
        "against a finite paycheck; whatever sits last may get nothing."
    ),
    "categories.target_cents": (
        "What this envelope is meant to hold, in cents. Used by refill_to "
        "rules and by the alert threshold, not enforced as a limit."
    ),
    "ai_jobs.not_before": (
        "Set when a job is deferred because the GPU was busy or it is inside "
        "the 01:00-08:00 America/Denver quiet window. The worker that "
        "already polls for queued work picks it up again after this time."
    ),
    "ai_jobs.attempts": (
        "Counts deferrals so a job cannot wait forever. Past the deadline it "
        "runs anyway: a stuttered frame costs less than a transaction that "
        "never gets categorised."
    ),
    "sessions.refresh_token_hash": (
        "A hash. The refresh token itself is never stored, so this table "
        "leaking does not let anyone sign in."
    ),
    "accounts.is_tracked": (
        "Whether this account counts toward the budget. Untracked accounts "
        "are still synced and visible but contribute nothing to the "
        "allocatable total."
    ),
    "receipts.line_items": (
        "Extracted lines as JSON. Reconciled against the transaction's own "
        "amount, with any residue attached to the dominant category and "
        "flagged, so the split is right even when a line is misread."
    ),
}


def _quote(text: str) -> str:
    return text.replace("'", "''")


def upgrade() -> None:
    for table, body in TABLES.items():
        op.execute(f"COMMENT ON TABLE public.{table} IS '{_quote(body)}'")
    for path, body in COLUMNS.items():
        table, column = path.split(".")
        op.execute(f"COMMENT ON COLUMN public.{table}.{column} IS '{_quote(body)}'")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"COMMENT ON TABLE public.{table} IS NULL")
    for path in COLUMNS:
        table, column = path.split(".")
        op.execute(f"COMMENT ON COLUMN public.{table}.{column} IS NULL")
