# 05 — AI Architecture

## Principle

Financial transaction history never leaves hardware the user controls. No
hosted inference. This is why the AI host exists, and it constrains every
choice below.

## Surveyed hardware (measured, not assumed)

`steamdeck` — 192.168.86.196:

| | |
|---|---|
| GPU | AMD Radeon RX 9060 XT (Navi 44, RDNA4) |
| **VRAM** | **16304 MB (~16 GB)** |
| CPU / RAM | 16 cores, 31 GB total, 26 GB available |
| Disk | 734 GB free on `/home` |
| OS | SteamOS (holo), **immutable rootfs** |
| Ollama | **not installed yet** |
| Container runtime | `podman` ✅ `distrobox` ✅ `flatpak` ✅ docker ✗ |
| GPU compute | `vulkaninfo` ✅ present · `rocminfo` ✗ absent |

## Model: Qwen2.5-VL-7B

One multimodal model handles vision, reasoning, and structured output.
Verified present in the Ollama registry:

| Model | Download | Role |
|---|---|---|
| **`qwen2.5vl:7b`** | **5.97 GB** | **primary — all job types** |
| `qwen2.5vl:3b` | 3.20 GB | fallback if 7B is too slow on this GPU |
| `qwen3:8b` | 5.23 GB | optional text-only companion |

At ~6 GB in 16 GB VRAM there is roughly 10 GB of headroom — enough to keep
`qwen3:8b` resident simultaneously if the text-only path turns out faster for
high-volume categorisation. Start with one model; add the second only if
measurement justifies it.

### Why a vision model changes the design

The original plan had receipts going through a separate OCR stage before a
text model could reason about them. **Qwen2.5-VL reads the receipt image
directly.** That removes an entire dependency (Tesseract/PaddleOCR), removes
a failure mode (OCR garbling line items before the model ever sees them), and
lets the model use layout — column alignment, totals, strikethroughs — that
plain OCR text discards.

### Tool calling: use structured output instead

Qwen2.5-VL supports tool calling, but **tool-calling support for vision models
in Ollama is less battle-tested than for text-only models**, and this app does
not actually need it. Every AI task here is "look at this input, return this
shape" — which is exactly what Ollama's `format` parameter with a JSON schema
does, more reliably and with fewer round trips.

**Decision: constrain output with a JSON schema; do not depend on the
tool-calling API.** Revisit only if a genuine multi-step agentic need appears.

## Install plan

The rootfs is immutable, so **do not install Ollama into `/usr`** — a SteamOS
update would wipe it. Use `podman` (already present):

- Container image, models stored under `/home` (734 GB free, own partition,
  survives OS updates).
- Managed by a **systemd user unit / quadlet** so it starts on boot and after
  wake-from-suspend.
- Bind `0.0.0.0:11434` on the LAN only. **Never port-forward it** — Ollama has
  no authentication.

### GPU backend is the open question

RDNA4 is new, ROCm is not installed, Vulkan is. In order:

1. Ollama's **ROCm** image — if it claims gfx1200-class support, best case.
2. A **Vulkan** backend build — Vulkan is confirmed present.
3. **CPU only** — 16 cores and 31 GB RAM. Slow but functional, and every job
   here is async anyway.

**Spike this before building on it** (roadmap Phase 0). Measure tokens/sec for
each path and record the result as an ADR. If all three are unusably slow,
`NullProvider` becomes the default and the app still ships — see ADR 0003.

## Provider abstraction

```
AIProvider
  ├─ DeckProvider    Pi wakes the gaming PC, calls Ollama over the LAN
  ├─ CustomProvider  user-supplied Ollama URL (e.g. their own Mac)
  └─ NullProvider    AI disabled; deterministic rules only
```

The app must be **fully usable with `NullProvider`**. Category balances,
paycheck splits, rules, thresholds and alerts are all deterministic code. AI
adds suggestions, summaries and receipt parsing — an enhancement, never a
dependency.

`CustomProvider` covers "point it at my Mac": the user enters
`http://192.168.1.50:11434` in settings. Validate reachability *and* that the
named model is present when they save it, not at first use.

## Wake-on-demand

The AI host sleeps by default. The Wake-on-LAN API is **already deployed and
verified** (`https://example.org/wol`, measured ~10 s cold wake).

```
job dispatched
   ├─ probe 192.168.86.196:11434 → awake? go
   ├─ not awake:
   │     POST {WOL_API_URL}/wake?wait=90   (Bearer {WOL_API_TOKEN})
   │     poll :11434 until it answers
   │     mark job 'waking' so the UI can say so
   ├─ POST /api/chat   (format = JSON schema)
   └─ persist result, push notification
```

- **Every AI call is a queued job with a persisted result.** Cold start is
  ~10 s wake + model load; nothing in the UI blocks on it.
- **Batch aggressively** — 40 new transactions is one job and one wake, not 40.
- Surface job state (`queued → waking → running → done`) so the UI can say
  "waking the AI server…" rather than spin opaquely.
- Idle shutdown after `AI_IDLE_SHUTDOWN_MINUTES` needs a `/wol/suspend`
  counterpart — not built yet (roadmap Phase 10). Until then the deck's own
  idle-suspend handles it.

## Job types — all on one model

| Kind | Input | Output | Notes |
|---|---|---|---|
| `categorise` | text | strict JSON | Batched; only for transactions deterministic rules missed |
| `receipt_parse` | **image** | line items JSON | Direct from photo — no OCR stage |
| `summary` | text | prose | Per pay period, conditioned on `user_goals` |
| `advise` | text | prose | Goal-aware suggestions |
| `design_categories` | text | JSON proposal | Editable proposal, never auto-applied |

## Structured output and validation

Every structured call sends a JSON schema via `format`. Example for
categorisation:

```json
{
  "transaction_id": "...",
  "allocations": [
    {"category": "Groceries", "amount_cents": 4210, "confidence": 0.93}
  ],
  "rationale": "Merchant 'SMITHS FOOD' matches grocery spending",
  "needs_review": false
}
```

A schema-valid response is still not trusted. Enforce before it touches the
ledger:

1. Allocation amounts sum **exactly** to the transaction amount.
2. Every category named exists and belongs to this user.
3. No negative amounts unless the transaction is a refund.
4. `confidence < AI_CONFIDENCE_THRESHOLD` (0.85) → review queue.

Any failure routes to review. **The model proposes; deterministic code
disposes.** It never writes to the ledger directly.

### Receipt parsing specifics

```
photo → qwen2.5vl:7b (image + category list + transaction total)
          │
          ├─ sum(line items) == transaction total → propose split
          └─ mismatch (tax, discounts, deposits)  → user reconciles
```

Tax and fees are apportioned proportionally across lines by default, with
manual override. **The user always confirms before allocations are written.**
A misread receipt silently rewriting six category balances would be worse than
no feature at all.

Store the image on disk (path in `receipts.image_path`), never in the DB, and
keep the model's raw response in `ai_jobs` for debugging.

## Prompting

- For `categorise`, send the user's category list **with descriptions and
  current balances**, the merchant, amount, date, and Plaid's own
  `personal_finance_category` as a hint. Plaid's classifier is decent and free;
  the model's job is mapping it onto *this user's* categories, which Plaid
  cannot know.
- For `summary` / `advise`, include `user_goals` verbatim. Generic advice
  versus useful advice is entirely this context.
- Keep prompts in version-controlled files (`backend/app/ai/prompts/`), not
  inline strings.
- Log prompt + response in `ai_jobs` for every call.

## Privacy boundaries

| Data | Leaves the LAN? |
|---|---|
| Transactions, balances, categories, goals | **Never** |
| Receipt images | **Never** — the VL model is local |
| Plaid `access_token` | Never leaves the backend process |
| Prompts and responses | **Never** |

Ollama has **no authentication**. Port 11434 must never be forwarded; remote
access is via the WireGuard VPN only. If a user points `CustomProvider` at a
public URL, warn them explicitly that financial data will go to that host.
