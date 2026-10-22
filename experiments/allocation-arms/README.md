# Who decides where the paycheck goes?

An experiment comparing four ways to split a paycheck into envelopes,
prompted by a real question: *could the allocation logic be AI-based,
driven by each category's description, instead of JSON rules — and is that
more or less accurate than having the AI write the rules?*

## The arms

| | What decides the amounts | When the model runs |
|---|---|---|
| **A** | The deterministic split engine, executing JSON rules | never, at payday |
| **B** | The model, reading prose descriptions | every payday |
| **B+** | The model, with a scratchpad to show its working | every payday |
| **C** | Arm B, then a deterministic repair pass | every payday |
| **D** | The engine — but the rules were *compiled from prose by the model* | once, at setup |

Arm A is ground truth by construction. That is the point rather than a
flaw: the engine provably conserves money (integer cents, exact remainder)
and provably honours funding order, and those two properties are what is
under test. Arm D is the interesting one — it keeps prose as the spec, the
thing the question is really about, without putting a language model in
the monthly money path.

## Method

`scenarios.py` states one budget three ways — prose descriptions, JSON
rules, and eight scenarios with ground truth from the real engine. The
prose and the rules express the *same* intent; if they did not, the
comparison would be rigged.

The eight scenarios deliberately include the cases where a split is hard:
a conditional that must **not** fire, refill targets already met, a
paycheck too small to cover rent, a gross that does not divide evenly, and
an exact-fit cheque with zero remainder.

Everything runs at `temperature: 0` against Ollama on the gaming PC, with
JSON-schema-constrained output — the same path the app uses.

```bash
python scenarios.py                              # ground truth table
python run.py    --model qwen3:14b --repeats 3   # arms B and C
python run.py    --model qwen2.5vl:7b --scratchpad   # arm B+
python arm_d.py  --model qwen3:14b               # arm D
python report.py                                 # summarise
```

## Scoring

- **exact** — every category matches the engine to the cent
- **conserved** — the amounts sum to the paycheck exactly; the single
  property that matters most, since a miss here is money invented or
  destroyed
- **priority inversion** — something lower in the order got funded while
  something above it was left short. This is the failure that actually
  hurts: rent losing to savings
- **determinism** — same input, same output across repeats

Arm E (engine allocates, model reviews) is in `arm_e.py`. See
`docs/adr/0006-allocation-stays-deterministic.md` for the findings and
the decision.
