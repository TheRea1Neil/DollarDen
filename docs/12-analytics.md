# Analytics

How the insights work, which insights were chosen, and which were
deliberately left out.

See also: ADR 0008 (why expense types are a second axis) and ADR 0009
(why the model compiles questions instead of answering them).

## The two axes

| | Category / envelope | Expense type |
|---|---|---|
| Answers | which pot paid for this | what was bought |
| Shaped by | how a paycheck is divided | how questions are asked |
| Example | Date Fund, Discretionary | Fast food, Subscriptions |
| Lives in | `categories` | `expense_types` |
| Carried on | `allocations.category_id` | `allocations.expense_type_id` |

A charge has one of each. They are independent: fast food comes out of
Date Fund some weeks and Discretionary others, and Discretionary holds a
dozen unrelated kinds of spending.

## What counts as spend

**A negative allocation attached to a transaction.**

- *Negative*, because a category losing money is money going out.
- *Attached to a transaction*, because moving $50 between envelopes also
  writes a negative allocation and is not spending.

Consequence: real cash spending must create a `Transaction` row or it is
invisible here.

Income is positive allocations with a transaction (refunds, deposits)
plus `paychecks.gross_cents`. Funding an envelope is deliberately not
income — the money already arrived, and counting both would double every
payday.

## Which insights, and why those

The test applied to each candidate: **does it tell you something you
could not have guessed, and can you act on it?** That rules out most of
what finance apps show.

| Card | What it says | Why it survived |
|---|---|---|
| `flow` | in, out, net, against last period | The one number everyone wants first |
| `by_type` | where it went, with last period beside it | Answers the actual question, across envelopes |
| `movers` | what **changed** most | The part you did not already know |
| `recurring` | repeating charges, monthly and annual cost | Nobody can list their subscriptions from memory, and the forgotten ones are still billing |
| `unusual` | a type against **its own** median | "$180 on fast food" means nothing without knowing $70 is usual |
| `envelopes` | funded against spent, per envelope | No generic app can do this — it needs a budget that says in advance what each pot was meant to get |
| `pace` | days elapsed, projected month total | Actionable while the month is still changeable |
| `top_merchants` | biggest shops, with share | Finds the one place quietly taking 20% |
| `untyped` | how much has no label | Every other card is only as good as this is small |

### Left out on purpose

- **A pie chart of the month.** Twelve slices carry no label, no figure
  and no comparison. Bars carry all three.
- **Net worth projections.** Two accounts and no investment data; a
  projection would be a decorated guess.
- **Peer comparison** ("you spend more on dining than 68% of users").
  One user, and it is a judgement dressed as a statistic.
- **A credit score widget.** Not in the data and not a budgeting tool.
- **Automatic advice on every card.** The figures are the product.
  Advice is a button.

### Why `unusual` compares against the user's own history

Six months of the same type, median, flag at 1.5×, minimum three months
of history before saying anything. Two data points make every month
either a record high or a record low. A comparison against *other
people* or against a generic guideline is an opinion; a comparison
against your own eight previous Septembers is information.

## Recurring detection

Deliberately conservative: at least three charges, a regular gap, and a
stable amount (±15%, so price rises pass and a weekly grocery shop does
not).

A false positive tells someone they have a subscription they do not
have, which sends them hunting through statements for nothing. A false
negative just leaves one line off a list they can still read. So the
thresholds favour missing things.

Merchant names are normalised first (`SQ *BLUE BOTTLE 4821` and
`SQ *BLUE BOTTLE 9930` are one coffee shop) or each variant falls under
the three-charge floor and the subscription disappears.

Cadences: weekly, fortnightly, monthly, two-monthly, quarterly,
half-yearly, yearly. A gap nothing bills on (45 days) is rejected rather
than rounded to the nearest cadence.

## Asking a question

```
question → model compiles a spec → SQL computes → model phrases it
```

The model picks filters. It never adds anything up. Every answer shows
the resolved period and filters, because the dangerous failure is not a
wrong number — it is a correct answer to the wrong question.

## Files

| Path | What |
|---|---|
| `backend/app/analytics/period.py` | "last month" → two dates. Pure, heavily tested |
| `backend/app/analytics/queries.py` | The only thing that produces a figure |
| `backend/app/analytics/recurring.py` | Subscription detection |
| `backend/app/analytics/insights.py` | The cards |
| `backend/app/analytics/taxonomy.py` | Creating and merging expense types |
| `backend/app/api/routes/analytics.py` | HTTP surface |
| `app/lib/ui/screens/insights_screen.dart` | The screen |
| `app/lib/ui/screens/ask_screen.dart` | Questions |
| `app/lib/ui/screens/expense_types_screen.dart` | Vocabulary + labelling queue |
| `experiments/ask-compile/` | Grades the compile step. **Not yet run** |

## Still open

- **`experiments/ask-compile/` has not been run** — the deck was
  unreachable. The compile accuracy is unmeasured.
- **Nothing is labelled retroactively.** Existing spending has no type
  until it goes through the labelling queue, which is why `untyped` is
  shown prominently.
- **Expense type is not suggested automatically at filing time yet.**
  The classifier exists and is wired into the labelling queue; hooking
  it into the review queue so a charge gets both a category and a kind
  in one pass is the obvious next step.
- **No budget per expense type.** "Keep fast food under $120 a month" is
  a natural thing to want next and does not exist. It would be a cap on
  the reporting axis, deliberately separate from envelope funding.
