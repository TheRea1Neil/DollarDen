# Can the local model compile a question into a query?

## The question

Asking "how much did I spend on fast food last month" requires two
different abilities:

1. Work out that this means **sum / expense type = Fast food / previous
   calendar month**.
2. Add up the transactions that match.

(2) is arithmetic over dozens of rows, which is what a 7B model is
worst at and what SQL is perfect at. ADR 0006 already settled the
equivalent case for paycheck splitting: the engine computes, the model
describes. So the money path here is SQL, and `app/analytics/queries.py`
is the only thing that produces a figure.

That leaves (1) as the open question, and it is a real one. If the model
reads "last month" as "this month", the answer is fluent, specific,
internally consistent and about the wrong thirty days. Nothing in the
sentence gives it away. That is a worse failure than refusing.

## What this measures

Precision on the compile step only, against hand-written expected
specs. Each case fixes `today` so "last month" has one correct answer.

Graded per field, because the failure modes are not equally bad:

| Field | If wrong |
|---|---|
| `period` | **Silent.** Right-looking answer, wrong dates. The one that matters. |
| `metric` | Usually visible — a count where money was expected looks odd. |
| `expense_types` | Caught: unresolvable names are reported to the user. |
| `group_by` | Cosmetic. A breakdown where a total was wanted is still correct. |

## Running it

Needs the deck awake and on the VPN.

    python3 experiments/ask-compile/run.py

Writes `results.json` next to this file and prints a per-field table.

## Mitigations already in the code, whatever this scores

- The resolved period and filters are shown on every answer
  (`SpendingAnswer.filterSummary`), so a misread question is visible
  rather than invisible.
- A filter naming something that does not exist comes back in
  `unresolved_filters` instead of silently matching nothing — otherwise
  a zero from a bad filter is indistinguishable from a real zero.
- An unparseable period sets `period_not_understood`, and the UI says
  which range it fell back to.
- `_PERIOD_CHOICES` is a closed enum. The model cannot invent
  "since_easter"; it can only pick the wrong option from a list, which
  is a smaller space to be wrong in.

The point of the display mitigations is that they work at any accuracy.
This experiment decides whether the feature is good enough to put in
front of someone, not whether it is safe — that part is structural.
