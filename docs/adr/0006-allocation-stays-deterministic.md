# ADR 0006 — Allocation stays deterministic; the model compiles, it does not divide

**Status:** accepted · **Date:** 2026-10-22

## Context

Today a paycheck is split by `app/budget/split.py`: ordered JSON rules
executed by a deterministic engine. The AI writes those rules once, during
setup.

The question was whether that is the right shape. Each category already
carries a `description` in the user's own words. Could the description
*be* the logic — the model reads "top this up to $300, groceries only" and
decides the amounts itself, with no rule engine? And is that more or less
accurate than having the model emit JSON rules?

Worth testing rather than assuming. Descriptions are more expressive than
any rule vocabulary, and "write what you mean" is a better authoring
experience than filling in `refill_to` forms.

`experiments/allocation-arms/` holds the harness.

## What was measured

One budget — tithing 10% first, $350 rent, $150 health, $50 car insurance
only below $450, Food to $300, Savings to $2,000, remainder to Car Fund —
stated twice: as prose descriptions and as JSON rules, expressing the same
intent. Eight scenarios with ground truth from the real engine, chosen to
include the hard cases: a conditional that must *not* fire, refill targets
already met, a paycheck too small to cover rent, a gross that does not
divide evenly, an exact-fit cheque. Temperature 0, JSON-schema-constrained,
against Ollama on the gaming PC.

| Arm | Who decides the amounts | Exact | Conserved the paycheck | Latency |
|---|---|---|---|---|
| **A** engine | deterministic rules | 8/8 | **8/8 guaranteed** | microseconds |
| **B** pure AI, qwen2.5vl:7b | the model, every payday | 0/24 | 3/24 | 3s |
| **B+** same, with a scratchpad | the model, every payday | 0/8 | 1/8 | 4s |
| **B** pure AI, qwen3:14b | the model, every payday | 7/8 | 7/7 | 27–87s, **1 timeout** |
| **C** arm B + deterministic repair | model, then clamp and sweep | 12/24 | 24/24 | 3s |
| **D** prose compiled to rules | the model once, engine after | 4/8 | **8/8 guaranteed** | 11–37s, once |

## What the numbers actually say

**The small model is fast and confidently wrong.** qwen2.5vl:7b was
excellent at every *local* rule — it computed 10% of $1,837.33 as exactly
$183.73, and got tithing and rent right in 24 of 24 runs. It failed on one
thing, every single time: it gave the catch-all category $0.00 and
silently dropped the leftover. **85% of all its error is that one
category.** Local amounts it can do; the global "what is left" it never
computed once.

**A scratchpad made it worse, not better.** Given a field to show its
working first, the same model lost its grip on units entirely and
allocated $18,373,300,000,000 to tithing on a $1,837.33 paycheck. Valid
JSON, valid integers, schema-conformant, nine orders of magnitude wrong.
This is the shape of the risk: a model in the money path fails in ways
that do not look like failures.

**The big model is accurate and does not reliably terminate.** qwen3:14b
got 7 of 8 exact including the odd-cents case, and conserved the paycheck
every time it answered. On `very_tight` — $400 that cannot cover rent — it
generated for **ten minutes and hit the timeout**. Recording a paycheck is
an interactive action.

**Arm D's errors are few, fixed, and visible.** Compiling prose to rules,
both models picked all seven rule types correctly, every attempt. Every
failure was one field of one rule: the car-insurance conditional. They
even failed in complementary ways — qwen3 got the threshold right
(`45000`) and inverted the operator (`>` for "less than"); qwen2.5vl got
the operator right (`<`) and the threshold wrong (`0`). Neither got both.

That is the important asymmetry. In arm D a mistake is **one line, seen
once, before any money moves, and fixed permanently**. In arm B the same
class of mistake recurs every payday, invisibly, and conservation is not
guaranteed at all.

### Arm E: the model as a checker does not work either

The obvious safety net — engine allocates, model independently reads the
prose and flags anything that disagrees — was measured separately, because
a checker is allowed to be wrong in a way an allocator is not. A missed
fault costs what we have today; a false alarm costs one glance.

Against 8 correct splits and 26 deliberately broken ones (money vanished,
a conditional fired wrongly, a refill overshot, priority inverted):

| | qwen2.5vl:7b |
|---|---|
| faults caught | **26/26** |
| false alarms on correct splits | **8/8** |
| latency | 3.3s |

Catching everything looks excellent until you see it also flagged every
correct split. The model says "wrong" unconditionally, which carries no
information at all. A warning that fires on every paycheck is one the user
stops reading by the second payday, and then it is worse than nothing —
it is a disabled alarm that looks enabled.

Rejected. Revisit only with a model that can stay quiet when nothing is
wrong.

### The conditional is the consistent weak spot

Worth naming, because it shows up in every arm independently:

- **allocating** — qwen2.5vl funded car insurance at a $460 balance when
  the rule says "only below $450"
- **compiling** — qwen3 inverted the operator; qwen2.5vl zeroed the
  threshold; neither got both fields right in any attempt
- **checking** — the false alarms are mostly the model insisting car
  insurance should have been funded when it correctly was not

Percent, fixed, refill_to and remainder are handled reliably. Thresholded
rules are not. That is a bounded, specific limitation to design around
rather than a general one.

## Decision

**The split engine stays in the money path. The model compiles intent into
rules; it never divides a paycheck.**

Descriptions remain the authoring surface — that part of the idea is good
and is kept. What changes is *when* the model runs: once, at setup,
producing rules a human reviews, rather than every payday producing
numbers nobody checks.

Arm C — letting the model allocate and then deterministically repairing it
— is explicitly rejected despite lifting 0/24 to 12/24. So is arm E, the
review-after-the-fact safety net, which flagged all 8 correct splits. Repair can only
fix what is unambiguous (money that does not sum, negative amounts). It
cannot fix a wrong judgement, and it produces a plausible-looking answer
that hides one. A silent correction of a wrong split is worse than a loud
failure.

## Why this is the right trade

- **Conservation is not negotiable.** The engine cannot lose a cent;
  `apply_split` asserts it. No model run can promise that, and the one
  property a budget must have is that the money adds up.
- **A paycheck must be explainable later.** Paychecks reference the plan
  version that produced them (ADR 0006 predecessor: docs/06). "The model
  decided" is not an audit trail.
- **Reproducibility.** Same input, same output, forever — including when
  the AI host is asleep, mid-game, or replaced by a different model.
- **AI is optional** (ADR 0003). Allocation is the core feature; it cannot
  depend on a GPU being awake.
- **The model is genuinely good at the compile step.** 7 of 7 non-
  conditional rules correct on every attempt from both models is a real
  result, and it is the step where being wrong is cheap.

## Consequences

- Conditional rules are the weak spot everywhere — compiling them,
  allocating against them, and reviewing them. The apply screen should
  show compiled rules back in plain English ("funded only while this holds
  **less than** $450") so an inverted comparison or a zeroed threshold is
  obvious before it is accepted. This is the single highest-value piece of
  UI the experiment argues for.
- This experiment found a live bug: a predicate missing its threshold
  passed validation and raised `KeyError` when a paycheck was recorded.
  Fixed in the same change — see the commit for the three layers.
- `qwen3:14b` is not adopted for text. It is more accurate than
  `qwen2.5vl:7b` on reasoning, but 10× slower, it hangs on edge cases, and
  swapping models evicts the vision model from a 16 GB card. Revisit if
  allocation ever needs judgement the engine cannot express.
- Re-run `experiments/allocation-arms/` before changing models or the rule
  vocabulary.
