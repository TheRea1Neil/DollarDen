# ADR 0007 — Receipts are read on the phone, not by the vision model

**Status:** accepted · **Date:** 2026-10-23 · **Re-tested 2026-10-26, decision unchanged, reason replaced**

## Context

Receipt itemisation was always planned to work by photographing a
receipt and having `qwen2.5vl:7b` — the vision model already configured
and already used for nothing else — read the line items.

Testing it first produced two results.

**Reading a receipt from TEXT works, and works well.** A pasted receipt
came back in 9 seconds with all eight items, both quantities, the
negative coupon line, tax and total, reconciling to the cent:

```
BANANAS                 0.87  x3    SPARKLING WATER 12PK    3.99
OAT MILK UNSWEET        7.98  x2    COUPON - MEMBER        -2.00
SOURDOUGH LOAF          3.49        tax                     2.49
GREEK YOGURT 32OZ       4.99        TOTAL                  31.79
FROZEN MANDARIN CHICKEN 5.49
DARK CHOCOLATE PB CUPS  4.49
```

**Reading one from an IMAGE does not work at all.** `qwen2.5vl:7b`
returned "Bread, Milk, Eggs" — invented items — for every image sent,
including a trivial one reading "ZEBRA 7719", for which it answered
`"system"`. Re-pulling the model changed nothing. A second vision model,
`moondream`, returned an empty string for the same image.

Two different models failing identically on a host whose text inference
is perfect points at the backend, not the model: ollama 0.40.7's vision
path does not work on this ROCm/RDNA4 build. The models report
`capabilities: ['vision']` and silently produce nothing useful.

## Decision

**The photo is read on the phone, and the text is sent.**

`google_mlkit_text_recognition` runs OCR on-device on both iOS and
Android. The extracted text then goes through the receipt parser that
already works.

Pasting receipt text stays as a first-class input, because it is the
same path and sometimes it is simply what you have.

## Why this is better than the plan it replaces

Even if the vision model worked, this would be the right shape:

- **Faster.** 9 seconds against a text prompt, with no 6 GB vision model
  to load and no waiting for the gaming PC to wake.
- **It cannot hallucinate items.** OCR either reads the characters or it
  does not. A vision model asked for line items will happily invent
  plausible ones, which is precisely what it did here — and a wrong
  item in a receipt is money filed against something never bought.
- **The photo never leaves the phone.** A receipt carries a card mask, a
  location and a timestamp.
- **It works with the AI host asleep or absent** (ADR 0003).

## Consequences

- An extra dependency and the platform OCR models it ships.
- OCR output is messier than a clean paste — columns interleave, and
  `0.29 x3  0.87` can arrive as separate fragments. The parser already
  handles untidy text, since that is what a pasted receipt usually is,
  but this needs testing against real photographs rather than rendered
  ones.
- The vision model stays configured and unused. It is still the right
  choice the day ollama's vision path works here; nothing about the
  model was wrong.
- **Do not reach for the vision path for anything else on this host
  without testing it first.** It fails silently and plausibly, which is
  the worst way for a thing to be broken.


---

## Addendum, 2026-10-26 — the premise above is obsolete; the decision is not

The reason recorded above was that ollama's vision path did not work on
this ROCm/RDNA4 build. **It works now.** Same ollama (0.40.7), same
model, after a SteamOS update:

> *"This receipt is from Walmart, and the TOTAL is $22.25."*

Correct, in 8.6 seconds, from a photograph of curled thermal paper. So
the decision had to be re-earned rather than inherited.

Re-tested against the real Galaxy S21 receipt photo, production prompt
and schema, **temperature pinned to 0**, three runs each
(`experiments/receipt-vision/`):

| Model | Items | Items sum (truth $21.60) | subtotal/tax/total | Time |
|---|---|---|---|---|
| qwen2.5vl:7b | 8, 8, 8 | $50.08 ×3 | 2160/65/2225 ✓ | 5.7s |
| gemma3:12b | 8, 8, 8 | $25.77 ×3 | 2160/**2160** ✗/2225 | 11s |

**Both read the receipt. Neither reads the price column well enough.**
Per item, qwen gets **1 of 8** prices right and gemma **4 of 8**. qwen's
errors are systematic decimal placement (`0.98 → 9.80`); gemma reads
prices far better and garbles item names instead (`HF SNK SGE`,
`GV BELUR`) while putting the subtotal in the tax field.

A first pass without pinning temperature appeared to show gemma was
unstable — three runs, three totals. That was wrong, and it was a
measurement error rather than a property of the model: ollama was
falling back to each model's Modelfile, which sets 0.0001 for qwen and
**1** for gemma. Pinned at 0 both are perfectly deterministic. The
claim is retracted; the experiment README records it.

**Revised reason:** the photo is read on the phone not because vision is
broken, but because **a vision model cannot read a price column
accurately enough for money**. OCR reads digits reliably and cannot
hallucinate a line that was never printed.

Two further findings worth keeping:

- **Vision does not remove the need for the deterministic line filter.**
  Both models returned SUBTOTAL, TAX, TOTAL, VISA TEND and CHANGE DUE as
  purchases — the same judgement failure the text path had, and the
  reason `app/ai/receipt_text.py` exists.
- **The two readers are complementary, not redundant.** OCR reads digits
  and scrambles rows; vision reads names and rows and scrambles digits.
  qwen read `COTTAGE CH` correctly where OCR produced `J22.88`. A
  plausible future use is vision as a second opinion on names when
  `reconcile()` reports the lines do not add up. Not built; noted.

Every run, at either temperature and from either model, would have been
caught by the existing checksum: items summing to $50.08 or $25.77
against a stated subtotal of $21.60 is impossible. That is the
architecture working as intended, whichever reader produced the
numbers.

The re-test also surfaced a production bug unrelated to receipts: the
provider sent no sampling options at all, so every AI call ran at the
model's Modelfile temperature rather than the `temperature: 0` every
experiment in `experiments/` was measured at. Fixed in
`app/ai/provider.py`.
