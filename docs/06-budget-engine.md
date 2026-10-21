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
