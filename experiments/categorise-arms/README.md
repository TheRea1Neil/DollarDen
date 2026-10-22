# Can the model pick the category — and do descriptions help?

Two questions, one harness.

**Picking a category is not what ADR 0006 tested.** That experiment had
the model *dividing a paycheck*: arithmetic under a global conservation
constraint, which it could not do. This is classification — one label
from a list, no sums, nothing to conserve.

**Descriptions were tested twice, and the first test was wrong.** The
first version used descriptions that restated the name ("Tithing: ten
percent of gross pay"). They did not help, which is unsurprising: they
carried nothing the word "Tithing" did not. The real question is whether
nuance a name *cannot* hold — who the payee is, where the boundary with
a neighbouring category falls — earns its place.

## Arms

| | Categories shown as |
|---|---|
| `plain` | name and kind only |
| `generic` | descriptions that restate the name — the straw man |
| `nuanced` | descriptions naming real payees and real boundaries |

## Cases

20 charges against a real budget, grouped by what they test:

- **clear** — answerable from the merchant name alone
- **needs_desc** — only answerable if a description names the payee
  (`CHURCH OF JESUS CHRIST DONATION`, `LAKEVIEW PROPERTY MGMT ACH`)
- **conflict** — the obvious guess is *wrong* and the description says
  so (`UBER TRIP` when Gas reads "fuel at a pump, nothing else — Uber
  and Lyft come out of Date Fund"). Tests whether a description actually
  steers the model or is decoration.
- **ambiguous** — no answer available. Declining is correct; guessing is
  the failure.

Scored the way auto-filing actually matters: a wrong answer is money in
the wrong envelope that nobody notices, while declining costs one tap.
"Wrong" and "declined" are counted separately and never averaged.

## Results — qwen2.5vl:7b, temperature 0

| | clear | needs_desc | conflict | ambiguous | wrong | precision |
|---|---|---|---|---|---|---|
| plain | 6/6 | 2/4 | **0/4** | 4/6 | 4 | 75% |
| generic | 6/6 | 3/4 | **0/4** | 5/6 | 5 | 74% |
| **nuanced** | 5/6 | **4/4** | **2/4** | 4/6 | **3** | **83%** |

~1.2s per charge.

**Nuanced descriptions work, and they are the only thing that does.**
Naming the payee takes `needs_desc` from 2/4 to 4/4. Stating a boundary
is the only way the model ever overrides an obvious-but-wrong guess —
both other arms score 0/4 there, and generic descriptions make it
actively worse (4 wrong).

**Generic descriptions are worse than none.** Same coverage, more wrong
answers. Restating the name adds tokens and no signal.

**The cost:** a description naming a payee gives the model something to
latch onto for opaque strings. `ACH DEBIT 88291002` was filed as Tithing
because the Tithing description mentions a donation — it pattern-matched
the *kind* of payment, not the payee. This is why the feature should
suggest rather than auto-file.

## A prompt change that backfired

Adding "a named payee only applies when that payee actually appears in
the charge" to fix exactly that failure dropped precision from 83% to
70% and halved the correct declines. The extra paragraph diluted the
instruction that was doing the work. Reverted — recorded because the
reasoning was sound and the result was not.

## Running it

```bash
python experiments/categorise-arms/run.py --model qwen2.5vl:7b --arm nuanced
```

Needs Ollama reachable on the gaming PC.
