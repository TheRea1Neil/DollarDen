# ADR 0009 — A question compiles to a query; the model never computes a figure

**Status:** accepted · **Date:** 2026-10-24

## Context

Neil asked to be able to ask questions in plain English — "how much did
I spend on fast food this week", "how much did I pay in subscriptions
last month" — and get answers about his own data.

There was already a version of this: `run_advise` puts category
balances into a prompt and asks the model to answer. That is fine for
"am I on track" and useless for "how much on fast food", because the
figure does not exist in the prompt. The straightforward extension is to
put the transactions in the prompt too and let the model add them up.

That is the thing not to do, and ADR 0006 already established why in the
neighbouring case. Asked to divide a paycheck, `qwen2.5vl:7b` produced
valid JSON, schema-conformant integers, and a catch-all category of
$0.00 with the remainder silently dropped — in 24 of 24 runs. Given a
scratchpad to work in, the same model allocated $18,373,300,000,000 on a
$1,837.33 paycheck. The failure mode is not "sometimes wrong"; it is
*confidently, plausibly wrong in a way that does not look like
failure*.

Summing forty transactions is the same class of task. And a wrong total
here is worse than a wrong split, because a split lands in the ledger
where reconciliation catches it, while a sentence saying "$184.32 on
fast food" is checked by nobody.

## Decision

**The model compiles the question into a query. SQL computes the figure.
The model then writes a sentence around a number it was handed.**

    "how much on fast food last month?"
       ↓  model, JSON-schema-constrained
    { metric: spend, period: last_month,
      expense_types: ["Fast food"], group_by: none }
       ↓  app/analytics/queries.py — plain SQL
    { total_cents: 18432, count: 14, previous: 14201 }
       ↓  model, given only those figures
    "$184.32 across 14 visits, up $42.31 from September."

Three properties follow from the shape rather than from the model
behaving well:

1. **Every figure the user reads came out of the database.** The
   phrasing call receives the computed numbers and is told that any
   amount it writes must appear in them. It has nothing to add up.
2. **The compile step is a closed choice, not free generation.** Period
   is an enum; metric is an enum; expense types and categories are enums
   built from the user's actual rows. The model cannot invent
   "since_easter" or a type that does not exist. It can pick the wrong
   option from a list, which is a much smaller space to be wrong in.
3. **The query is shown back.** Every answer carries the resolved
   period, the filters used, and the transaction count, rendered as a
   "Read as: Fast food · September 2026 · 14 transactions" line. Any
   total opens into the rows behind it.

Point 3 is the one that matters most, and it exists because of how this
fails. The dangerous failure is not a wrong number — SQL does not
produce those. It is a **correctly computed answer to the wrong
question**: the model reads "last month" as "this month" and returns a
fluent, specific, internally consistent figure about the wrong thirty
days. Nothing in the sentence gives it away. Showing the resolved
query makes that visible, and it stays visible at any model accuracy.

Three smaller decisions follow the same principle:

- **An unresolvable filter is reported, not dropped.** If the model
  asks for a type that does not exist, `unresolved_filters` says so and
  the UI prints it. Otherwise a zero caused by a bad filter looks
  identical to a genuine zero.
- **An unparseable period is a 400 at the HTTP layer**, never a silent
  fallback to this month.
- **`today` comes from the phone.** The Pi runs UTC. Asked at 22:00 on
  the 31st, a server-side `date.today()` answers for next month, and
  "this month" comes back empty for no visible reason.

## Status of the measurement

The compile step has **not been measured yet** — the deck was
unreachable when this was written. `experiments/ask-compile/` holds 16
cases with hand-written expected specs and a grader that imports the
shipped prompt and schema rather than copying them. It grades per field,
because the failure modes are not equally bad: a wrong `period` is
silent, a wrong `metric` is usually visible, a bad filter is caught and
reported, and a wrong `group_by` is cosmetic.

Run it before trusting the feature, and again before changing models.

This ADR is accepted ahead of that number because it is a decision about
**where arithmetic lives**, and that does not depend on how well the
model compiles. A better model would not make it safe to let it add up
transactions; a worse one does not change where the SQL goes.

## Consequences

- The insights screen works with AI switched off (ADR 0003). The cards
  are computed server-side and are the primary surface; the plain-words
  read is a button, and asking a question is a separate screen.
- Two model calls per question, so latency is roughly double a single
  call plus wake time. Acceptable for an explicit question; it is why
  the insights screen does not narrate automatically.
- If the phrasing call fails, the numbers are already correct, so a
  plain generated sentence is used and the answer still returns
  (`_fallback_sentence`). Prose is the optional half.
- `app/analytics/period.py` is pure and separately tested. It is the
  highest-risk small piece of code in the feature: an off-by-one at a
  month boundary moves real money between two answers, and "last 3
  months" including a half-built current month makes every comparison
  look like a decline.

## Amendment, 2026-11-17: facts and a calculator, not pre-computed answers

The principle above — the model picks, the engine computes — held for
sums over transactions. It did not cover the arithmetic *on top* of
them: "how much can I spend a day on food until payday" needs a
balance, a day count and a division, and only the first came from a
tool.

The first attempt had the `payday` tool pre-compute it: every envelope
balance divided by the days remaining. Run against the real budget it
produced

```
Rent          $3,076.59 -> $1,025.53 /day
New Car Fund  $1,933.05 ->   $644.35 /day
```

which is not a thing. Rent is one committed payment and a car fund is
being accumulated, not drawn down. Pre-computing had answered one
scenario, badly, for two thirds of the envelopes — and a figure like
that sitting in a tool result is an invitation to repeat it.

**So the tools supply facts and the model composes them.** `payday`
returns dates and day counts only. `calculate` does one sum, exactly,
whatever sum the question turns out to need. The model still never does
arithmetic — it decides *which* arithmetic, which is the judgement it
is actually good at. Observed end to end:

```
budget {} → payday {} → calculate {"expression": "162.56 / 3"}
"...you can spend approximately $54.19 per day on food."
```

This generalises where pre-computing could not. "What if I skip eating
out" and "how much is left if I spend $30 today" need no new tool.

### Evaluating a string the model wrote

`calculate` is the one place in the app that evaluates an expression
written by a language model, so it does not use `eval`. The expression
is parsed to an AST and walked: numeric literals, the seven arithmetic
operators, and `round`/`abs`/`min`/`max`. Names, attributes, strings,
subscripts and every other call are refused.

Two limits are about cost rather than access: `**` is capped, because
`9**9**9` is a perfectly valid expression and an unbounded wait, and
the expression length is capped.

Failures come back as `{"error": ...}` rather than raising. An
exception kills the job and the question goes unanswered; an error
string lets the model read what went wrong and try a different sum.
`tests/test_calculator_tool.py` covers the refusals explicitly — they
are the reason for the design, not a side effect of it.
