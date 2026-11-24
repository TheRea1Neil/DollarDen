# 06 — Budget Engine

The deterministic core. No AI runs here. Given the same inputs this produces
the same outputs, every time, and it is the part that must be correct.

## Model

A **category** is a balance, not a report line.

| Kind | Behaviour | Examples |
|---|---|---|
| `spending` | Refilled to a target each paycheck; drawn down by purchases | Food & Groceries ($300), Gas |
| `savings_bucket` | Topped up to a target; large purchases drawn from it | Short-Term Savings ($2000), New Car Fund |
| `obligation` | Accrues toward a known payment; the payment clears it | Tithing, Rent, Car Insurance |

Tithing is an `obligation`: income *adds* 10%, and a payment to the church
*subtracts* from it. The balance is "what I owe but haven't yet paid".

## Paycheck splits

A **split plan** is an ordered list of rules. Order is semantically
significant — tithing is computed on gross before anything else consumes it.

### Rule types

| Type | Params | Semantics |
|---|---|---|
| `percent` | `rate`, `of` (`gross` \| `remaining`) | Tithing: 10% of **gross** |
| `fixed` | `amount_cents` | Rent: $350 |
| `refill_to` | `target_cents`, `max_contribution_cents?` | Food: bring up to $300; contribute nothing if already there |
| `conditional` | `when` (predicate), `then` (nested rule) | "if Car Insurance < $450, add $50" |
| `remainder` | — | Everything left. Exactly one, always last |

### Worked example

The stated plan, encoded:

```
1. percent      Tithing            rate=0.10, of=gross
2. fixed        Rent               350_00
3. fixed        Health Insurance   150_00
4. conditional  Car Insurance      when: balance < 450_00 → fixed 50_00
5. refill_to    Food & Groceries   target=300_00
6. refill_to    Short-Term Savings target=2000_00
7. remainder    New Car Fund
```

Evaluation:

```
remaining = gross
for rule in plan ordered by order_index:
    amount = rule.compute(gross, remaining, category.balance)
    amount = clamp(amount, 0, remaining)          # never allocate more than is left
    if amount > 0:
        append allocation(category, +amount, source='paycheck_rule')
        remaining -= amount
assert remaining == 0                              # the remainder rule guarantees this
assert sum(allocations) == gross                   # hard invariant
```

### Edge cases that must be handled explicitly

These are where budgeting apps quietly go wrong:

- **Paycheck too small to satisfy every rule.** Rules are satisfied in order
  until the money runs out; later rules get $0. The user is **told** which
  rules went unfunded — silence here is a bug.
- **A `fixed` rule exceeding what remains** is clamped, not skipped, and
  flagged.
- **`remainder` with nothing left** allocates $0. Not an error.
- **No remainder rule defined** → refuse to apply the plan. Unallocated money
  is a correctness hole.
- **Rules edited after a paycheck was applied.** Split plans are **versioned**;
  a historical paycheck keeps pointing at the version that produced it, so the
  past still explains itself.
- **Two paychecks the same day** (e.g. a bonus). Each applies independently.
- **Reversed/returned paycheck.** Produces inverse allocations via a
  `correction` entry; never deletes history.

## Detecting a paycheck

A deposit is a paycheck candidate if it is an inflow to a tracked depository
account, above a floor, and either matches a known employer pattern or
recurs at a ~14-day cadence. Candidates are **confirmed by the user the first
time**; thereafter matching deposits auto-apply the active plan, with a
notification saying what was done and offering undo.

Never auto-apply an unrecognised large deposit. A tax refund split as if it
were a paycheck is a bad surprise.

## Drawing down

For each new non-pending transaction:

```
1. deterministic rules (category_rules, by priority) → match? apply. Done.
2. a prior user decision for the same merchant         → apply. Done.
3. AI provider available?  → categorise job (batched)
       confidence ≥ 0.85 and invariants hold → apply
       otherwise                             → review queue
4. no AI → review queue
```

Deterministic rules run first deliberately: they are free, instant, and
predictable, and most spending is repeat merchants. The model only sees the
genuinely novel cases.

### Pending transactions

Plaid emits a pending transaction, later **removes** it and adds a posted one
with a **different `transaction_id`**. Naive handling double-counts.

Rule: **allocate on pending** (so the balance reflects reality immediately),
and when the posted version arrives, link it via `pending_replaced_by`,
**move** the allocation rather than creating a second one, and adjust for any
amount change (restaurant tips are the common case).

### Receipt splitting

One Costco charge is rarely one category.

```
photo → qwen2.5vl:7b (reads the image directly) → line items + categories
                                   │
                                   ├─ sum(lines) == transaction total?  → propose
                                   └─ mismatch (tax, discounts)         → user reconciles
```

No separate OCR stage: the vision model reads the receipt image directly,
which also lets it use layout (columns, totals, strikethroughs) that flat OCR
text throws away. See docs/05.

The user always confirms before allocations are written. Tax and fees are
apportioned across lines proportionally by default, with manual override.

## Reconciling against the bank

The budget must agree with reality. The invariant:

```
allocated  ==  cash  -  card_debt
```

Why it holds continuously:

| | cash | card debt | allocated | check |
|---|---|---|---|---|
| start | 1000 | 0 | 1000 | 1000 − 0 = 1000 ✓ |
| $42 on the card | 1000 | 42 | 958 | 1000 − 42 = 958 ✓ |
| pay the card | 958 | 0 | 958 | 958 − 0 = 958 ✓ |

A card purchase draws its category down before the cash leaves, so the two
sides diverge by exactly the card balance and converge again when the card
is paid. This is the budgeting equivalent of reconciling a bank statement:
if they disagree by anything else, the user is being shown a comfortable
number that is wrong, and should be told.

`cash` and `card_debt` count **tracked accounts only**, so choosing which
accounts to track is also choosing what the budget is measured against.

### The unallocated sink

Exactly one category per user carries `is_unallocated`, enforced by a
partial unique index rather than trusted to application code. It holds
money with no purpose yet, which is what keeps the total matching the bank.

`POST /budget/reconcile` sweeps any difference into it. **It works in both
directions**: if more is allocated than exists — spending outran income —
the sink goes negative rather than the shortfall being hidden. A budgeting
app that quietly absorbs a shortfall is worse than one that shows a scary
number.

### Starting from money you already have

`POST /budget/opening-balance` takes the current balances of tracked
accounts and distributes them across categories, with the remainder landing
in the sink. Guard rails:

- refused once categories already hold money — opening balances are for
  starting out, not for correcting an existing budget
- refused if the allocations exceed what is available, unless
  `allow_over_allocation` is passed explicitly

## Alerts

| Alert | Trigger |
|---|---|
| Threshold | `balance < alert_at_cents` |
| Burn rate | Projected to hit zero before the predicted next paycheck |
| Unfunded rule | A paycheck could not satisfy every split rule |
| Item broken | Plaid `ITEM_LOGIN_REQUIRED` — needs update-mode re-auth |

**Next-paycheck prediction:** median gap between the last N confirmed
paychecks (default 14 days), with a confidence band. Shown as a date range,
not a false-precision single date. An alert reading "Food is at $180 and your
next paycheck is ~6 days out" is actionable; "Food is at $180" is not.

## Invariants (enforce in code and tests)

1. `category.balance == sum(allocations for that category)`. Always. The
   balance column is a cache of the ledger, never an independent source.
2. `sum(paycheck allocations) == paycheck.gross_cents`. Exactly.
3. Allocations are append-only. Corrections are new rows.
4. No allocation references an archived category.
5. A transaction's allocations sum to its amount, or it is in the review queue.
6. All arithmetic in integer cents.

These belong in a property-based test suite. The rule engine is the one place
where a subtle bug silently produces wrong numbers for months.

## The two orders

Categories carry two independent orderings, and conflating them is the
easiest way to give a user a false impression about where their money is
going.

| | Column | Changed by | Effect |
|---|---|---|---|
| **Display order** | `categories.sort_order` | `POST /budget/categories/reorder` | Cosmetic. What order the budget list renders in. Moves no money. |
| **Funding order** | `split_rules.order_index` | `POST /budget/plans/{id}/reorder`, or any plan edit | Which category gets filled first out of a paycheck. Moves money. |

Both are exposed as drag-to-reorder, so the app has to say which one the
user is dragging. The category list shows a notice in reorder mode stating
plainly that it changes display only.

A funding reorder is a **plan revision**: it bumps `version`, deactivates
the old plan, and is validated like any other edit. In particular a drag
that leaves the `remainder` rule somewhere other than last is **rejected**,
not silently repaired — if the user dragged the catch-all into the middle,
the right response is to tell them, because any automatic fix would be a
guess about intent. A rejected drag leaves the live plan active and
untouched.

## Category descriptions

`categories.description` is **not** read by the allocation engine. Nothing
about where money goes depends on it; that is entirely `split_rules`
(`rule_type` + `params`, evaluated in `order_index` order).

What it *is* for is context for the model. It is passed to every AI task
that reasons about categories — designing them, categorising a
transaction, writing a summary — via `_describe_category()` in
`app/ai/tasks.py`. "Groceries only, not restaurants" is the kind of thing
that cannot be inferred from a name and a balance.

This is worth stating because the field looked decorative for a while: the
model was writing descriptions that were stored and then never read by
anything. If you add a new AI task that mentions categories, send the
description.

## Adding one category

`POST /budget/apply-category` creates a single category and, optionally,
inserts one funding rule into the active plan.

It exists separately from `/budget/apply-proposal` because regenerating a
whole plan to add one envelope risks changing rules the user has already
tuned. The single-category path:

- **inserts** rather than replaces — every other rule keeps its relative
  funding priority
- defaults the new rule to **last before the remainder**, so a new category
  cannot starve rent
- refuses `rule_type: remainder` outright: one category has no business
  claiming whatever is left
- resolves the plan **before** creating the category, so a missing plan is a
  clean 409 rather than something that depends on session teardown

The `matcher` field turns the model's `matcher_hint` into a real
`category_rules` row. It is **opt-in in the app and off by default**: a
too-broad match quietly files real spending into the wrong envelope, and
the user has no reason to go looking for it. The suggested word is editable
before it is accepted, because the model's guess tends to be narrow
("dog food" never matches `VCA ANIMAL HOSPITAL`).

## Two engines, held together by golden tests

`app/lib/core/split_engine.dart` is a port of `backend/app/budget/split.py`.

It exists because the paycheck review is interactive: dragging a category
has to recalculate as fast as the finger moves. A round trip per drag was
both slow and a source of desync — the list on screen and the rules the
next request assumed could disagree, and after one drag nothing could be
moved again.

The server remains the source of truth (ADR 0006). The Dart engine is a
PREVIEW only; applying a paycheck recomputes server-side.

Two implementations of money logic will drift unless something stops
them. `app/test/split_golden.json` is generated by running the real
Python engine over a set of fixtures — odd-cent rounding, the exact
half-up boundary, caps, conditionals, running out mid-plan, and the four
structural failures the engine must refuse. `app/test/split_engine_test.dart`
asserts the port matches all of them to the cent.

Regenerate the goldens after any change to the Python engine:

```bash
cd dollarden
backend/.venv/bin/python experiments/gen_split_golden.py > app/test/split_golden.json
cd app && flutter test test/split_engine_test.dart
```

If the port and the server ever disagree, the server wins and the port is
the bug.
