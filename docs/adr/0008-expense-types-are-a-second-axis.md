# ADR 0008 — Expense types are a second axis, not a reshaping of categories

**Status:** accepted · **Date:** 2026-10-24

## Context

The ask was for insights: how much on fast food this week, what did
subscriptions cost last month, where is the money going. The obvious
move is to report on the categories that already exist — they are named,
described and already have every transaction filed against them.

That does not work, and it is worth being precise about why.

A category in this app is an **envelope**: a balance, funded by split
rules, shaped entirely by how Neil wants to divide a paycheck. "Date
Fund", "Discretionary", "Ch JesusChrist Donation". The question an
envelope answers is *which pot paid for this*.

"How much did I spend on fast food" is a different question. It asks
*what was bought*. The two come apart in both directions:

- **One kind, many envelopes.** A burrito comes out of Date Fund on a
  Friday and Discretionary on a Tuesday. Reported by envelope, fast food
  is split across three pots and has no total.
- **One envelope, many kinds.** Discretionary holds fast food, films,
  books, a phone case and a haircut. Its total is a number about a pot,
  not about anything Neil did.

So reporting on envelopes cannot answer the question — not because the
category data is poor, but because the axis is wrong. Neil said as much
in the request: *"if it could **also** categorise the type of expense
they are"*. Also, not instead.

The alternative considered and rejected was reshaping categories to be
report-friendly — splitting Discretionary into Discretionary-Food,
Discretionary-Fun and so on. That corrupts the budget to serve the
report: more envelopes to fund, more split rules to maintain, and a
funding plan that is now shaped by reporting convenience. It also does
not fix the first problem, since fast food still comes out of Date Fund
sometimes.

Plaid's `personal_finance_category` already provides a taxonomy of
roughly this shape and is kept on every transaction. It is not used as
the reporting axis because it is fixed, generic, absent on anything not
synced from Plaid, and not editable by the person whose money it is. It
remains useful as a hint when suggesting a type.

## Decision

**A second taxonomy, `expense_types`, over the same money.** Every
charge may carry one envelope and one expense type, and the two are
independent.

**Grain is the allocation, not the transaction.** A Target run split by
the receipt itemiser is already several allocations; each line takes its
own type, so "household" and "groceries" out of one charge both land
correctly. Because spend-by-type and envelope balances are then computed
from the same rows, the insights screen and the budget screen cannot
disagree.

**What counts as spend is a negative allocation attached to a
transaction.** Negative because a category losing money is money going
out; attached to a transaction because moving $50 from Discretionary to
Groceries also writes a negative allocation and is not spending. That
exclusion is the one most likely to be wrong silently — a total inflated
by envelope transfers reads exactly like a correct one — so it is
asserted directly in `test_analytics_queries.py`.

**The model may coin new types, through a guard.** Neil asked for this
explicitly: try the existing kinds first, invent one when nothing fits.
Unchecked, that produces "Fast food", "Fast Food" and "Quick Service
Restaurant" inside a month, each holding a third of the answer. This is
not hypothetical — the "remember this vendor" feature produced 22
matcher rules containing 11 exact duplicates on this account. So:

- every creation path goes through `taxonomy.resolve_or_create`
- names are compared as slugs, which collapse case, punctuation,
  `&`/`and`, filler words and naive plurals
- a coined name that collides is silently redirected to the existing
  type, and the UI says so
- `created_by` and `reviewed_at` mark what the assistant invented, so
  there is a list to confirm rather than a vocabulary that grows behind
  the user's back

**Standard names are offered but not created.** A fresh account with 26
empty types is clutter; an empty one gives the model nothing to anchor
on and invites a fresh phrase per charge. `STARTER_TYPES` is shown in
pickers and prompts marked "available", and becomes a row the first time
something is filed under it.

**Merging relabels history.** The ledger forbids editing history for
money, and this does not weaken that: an expense type is a label on
money, not money. A merge that left old spending under the old name
would defeat the purpose of merging.

## Consequences

- Nothing is labelled retroactively. Spending filed before this change
  has no type, which is why the insights screen shows an explicit "not
  yet labelled" figure — every other card is only as trustworthy as that
  number is small, and hiding it would let the screen look authoritative
  while describing a fraction of the money.
- Two taxonomies is two things to maintain. Mitigated by the type being
  suggested at the point a charge is filed rather than as separate
  admin, and by `/analytics/untyped` being a queue rather than a search.
- Archive and merge mean different things and the distinction only
  matters at the moment of choosing, so the UI states it there: archive
  keeps history where it is, merge moves it.
- Real cash spending must create a `Transaction` row or it will be
  invisible to every figure here. No current path does otherwise.
