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

Three runs each:

| Model | Items | Items sum | subtotal / tax / total | Time |
|---|---|---|---|---|
| truth | 8 | **$21.60** | 2160 / 65 / 2225 | — |
| qwen2.5vl:7b | 8, 8, 8 | $50.08 ×3 | **2160 / 65 / 2225** ✓ | 6s |
| gemma3:12b | 8, 9, 9 | $29.48, $26.42, $25.77 | 2160 / **2225 or 2160** ✗ / 2225 | 11–14s |

## What this says

**Both read the receipt. Neither reads the price column.**

- **qwen2.5vl** nails every *stated* figure — subtotal, tax and total
  exact on all three runs — and reads all eight item names in order,
  including `COTTAGE CH`, which the OCR mangled into `J22.88`. Its item
  prices are wrong by decimal placement: `0.98 → 9.80`, `1.16 → 11.60`,
  `1.52 → 15.20`. Wrong the same way every run.
- **gemma3:12b** gets some prices right that qwen gets wrong (4.24 and
  3.16 both correct), and is otherwise worse: it invents duplicate rows
  (`GV MNG 12Z` twice at different prices), returns 8 or 9 items
  depending on the run, and puts the TOTAL in the tax field.

The deciding difference is consistency. qwen is **deterministically
wrong** — same answer every time, so a correction could in principle be
learned. gemma is **non-deterministically wrong**: three runs, three
different totals, none right. For money, an unstable reader is worse
than a stable one with a known flaw.

Both also returned SUBTOTAL, TAX, TOTAL, VISA TEND and CHANGE DUE as
things that were bought — the same judgement failure the text path had.
**So vision does not remove the need for the line filter.** That was
Neil's hope for dropping the regex, and it does not survive the test.

## Decision

**ADR 0007 stands — new reason.** Not "vision is broken" but "vision
cannot read a price column accurately enough for a ledger". OCR reads
digits reliably and cannot hallucinate an item that is not printed.

What changed is that vision is now worth something. Comparing the two:

| | reads digits | reads names and layout |
|---|---|---|
| ML Kit OCR | **reliable** | poor — scrambles rows on curled paper |
| vision model | poor | **good** — got `COTTAGE CH` where OCR failed |

That is complementary, not redundant, and suggests a use: vision as a
**second opinion on names and row structure** when the deterministic
reconciler says the lines do not add up. Not built; noted.

What already works regardless: every one of the six runs above would be
caught by `receipt_text.reconcile()`, because items summing to $50.08
against a stated subtotal of $21.60 is arithmetically impossible. The
checksum does its job whichever reader produced the numbers.

## Reproducing

Needs the deck awake and the split-tunnel VPN up.

    python3 experiments/receipt-vision/run.py [model]

Defaults to comparing `qwen2.5vl:7b` and `gemma3:12b` over three runs
each against `backend/tests/fixtures/`.
