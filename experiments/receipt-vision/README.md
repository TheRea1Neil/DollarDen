# Can a vision model read a receipt, so we can drop the OCR + regex?

**Run 2026-10-26**, after the SteamOS update on the deck.

## Why re-test

ADR 0007 sent receipts through on-device OCR because the vision path
was **broken**: `qwen2.5vl:7b` returned invented items ("Bread, Milk,
Eggs") for every image, and `moondream` returned an empty string. Two
models failing identically on a host whose text inference was perfect
pointed at ollama's ROCm/RDNA4 build, not at the models.

Neil asked whether that still holds, and whether a Gemma variant could
read receipts directly so the deterministic line filter would not be
needed.

Worth re-testing rather than assuming: the premise was an
infrastructure fault, and infrastructure gets fixed.

## It is fixed

Same ollama (0.40.7), same model, different answer:

> *"This receipt is from Walmart, and the TOTAL is $22.25."* — correct,
> in 8.6s, from a photograph of curled thermal paper.

The SteamOS update is the only thing that changed. **ADR 0007's stated
reason is obsolete.**

## The real test

One real Walmart receipt, photographed on a Galaxy S21 — the same image
the OCR pipeline was tested against. Production prompt and JSON schema,
imported rather than retyped. Ground truth from the paper:

```
COTTAGE CH 2.88   HF SMK SGE 4.24   HF SMK SGE 4.24   GV MXDVG 0.98
GV BFLOR  1.16    GV BRUSPR  1.52   MANGO CHUNK 3.42  GV WHOLE GAL 3.16
                  SUBTOTAL 21.60    TAX 0.65          TOTAL 22.25
```

### First attempt was not a fair test

Three runs each, no temperature set — which is what the app itself was
doing. The results looked decisive and were partly an artefact:

| Model | Items | Items sum | subtotal / tax / total | Time |
|---|---|---|---|---|
| truth | 8 | **$21.60** | 2160 / 65 / 2225 | — |
| qwen2.5vl:7b | 8, 8, 8 | $50.08 ×3 | **2160 / 65 / 2225** ✓ | 6s |
| gemma3:12b | 8, 9, 9 | $29.48, $26.42, $25.77 | tax ✗ | 11–14s |

Neil asked whether temperature was pinned. It was not, and "unset" does
not mean zero — ollama falls back to each model's Modelfile:

    qwen2.5vl:7b    temperature 0.0001
    gemma3:12b      temperature 1, top_k 64, top_p 0.95

So qwen was running greedy and gemma was being heavily sampled. The
"gemma returns a different total every run" finding measured its
Modelfile, not the model. **Retracted.**

### Re-run with temperature pinned to 0

| Model | Items | Items sum | subtotal / tax / total | Time |
|---|---|---|---|---|
| truth | 8 | **$21.60** | 2160 / 65 / 2225 | — |
| qwen2.5vl:7b | 8, 8, 8 | $50.08 ×3 | **2160 / 65 / 2225** ✓ | 5.7s |
| gemma3:12b | 8, 8, 8 | **$25.77 ×3** | 2160 / **2160** ✗ / 2225 | 11s |

Both are now perfectly deterministic. Per item:

| Printed | Truth | qwen | gemma |
|---|---|---|---|
| COTTAGE CH | 2.88 | **2.88** ✓ | **2.88** ✓ |
| HF SMK SGE | 4.24 | 2.40 | **4.24** ✓ |
| HF SMK SGE | 4.24 | 2.40 | **4.24** ✓ |
| GV MXDVG 12Z | 0.98 | 9.80 | 4.99 |
| GV BFLOR 12 | 1.16 | 11.60 | 1.52 |
| GV BRUSPR 12 | 1.52 | 15.20 | **1.52** ✓ |
| MANGO CHUNK | 3.42 | 4.20 | 3.22 |
| GV WHOLE GAL | 3.16 | 1.60 | **3.16** ✓ |
| | **correct** | **1 / 8** | **4 / 8** |

**gemma3:12b is four times better on prices.** qwen's errors are
systematic decimal placement (`0.98 → 9.80`, `1.16 → 11.60`); gemma
reads the column far more often and fails differently — it garbles item
NAMES (`HF SNK SGE`, `GV MNG`, `GV BELUR`) and puts the subtotal in the
tax field.

Neither sums to $21.60, so neither is usable on its own. But the honest
summary is: gemma reads prices better, qwen reads names and stated
totals better, and the earlier claim that gemma was unstable was my
measurement error.

## Decision

**ADR 0007 stands — new reason.** Not "vision is broken" but "vision
cannot read a price column accurately enough for a ledger". OCR reads
digits reliably and cannot hallucinate an item that is not printed.

What changed is that vision is now worth something. No single reader
is good at everything:

| | reads digits | reads names | reads stated totals |
|---|---|---|---|
| ML Kit OCR | **reliable** | poor — scrambles rows on curled paper | n/a |
| qwen2.5vl:7b | 1/8 | **good** — got `COTTAGE CH` where OCR failed | **exact** |
| gemma3:12b | **4/8** | poor — `HF SNK SGE`, `GV BELUR` | tax wrong |

That is complementary rather than redundant, and suggests a use:
vision as a **second opinion** when `reconcile()` reports the lines do
not add up — qwen for names and row structure, gemma for a disputed
price. Not built; noted.

Worth saying plainly: a second reader is only useful because the
subtotal gives something to arbitrate against. Two readers disagreeing
with no checksum would just be two guesses.

What already works regardless: every run above, at either temperature
and from either model, would be caught by `receipt_text.reconcile()` —
items summing to $50.08 or $25.77 against a stated subtotal of $21.60
is arithmetically impossible. The checksum does its job whichever
reader produced the numbers, which is the whole argument for having it.

## The bug this experiment found

Pinning temperature was not just an experimental control. The app was
sending **no `options` at all**, so every call ran at whatever the
model's Modelfile specified — 0.0001 for qwen, 1 for gemma. The same
code was effectively greedy on one model and heavily sampled on
another, and nothing in the request said which.

It also meant production did not match its own measurements: every
experiment in `experiments/` pins `temperature: 0`, so the figures
ADR 0006 quotes described a configuration that never shipped. In
practice the text model was already at 0.0001 so little changed, but it
was luck, and swapping models would have silently changed behaviour.

Fixed in `app/ai/provider.py`: temperature 0 by default on every call,
overridable per call.

## Reproducing

Needs the deck awake and the split-tunnel VPN up.

    python3 experiments/receipt-vision/run.py [model]

Defaults to comparing `qwen2.5vl:7b` and `gemma3:12b` over three runs
each against `backend/tests/fixtures/`.
