# ADR 0004 — Qwen2.5-VL-7B as the single model

**Status:** accepted · **Date:** 2026-10-21 · **Supersedes part of ADR 0003's assumptions**

## Context

The AI host has an RX 9060 XT with **16 GB VRAM**. The workload is five job
types: transaction categorisation, receipt parsing, pay-period summaries,
goal-aware advice, and category design. Receipt parsing was originally specced
as local OCR feeding a text model.

## Decision

Use **`qwen2.5vl:7b`** (5.97 GB, confirmed present in the Ollama registry) as
the single model for all job types. Constrain outputs with Ollama's `format`
JSON-schema parameter rather than relying on the tool-calling API.

## Why

- **It removes the OCR stage entirely.** The vision model reads receipt images
  directly, eliminating a dependency (Tesseract/PaddleOCR) and a failure mode
  (OCR garbling line items before the model sees them). It can also use
  layout — columns, totals, strikethroughs — that flat OCR text discards.
- **One model is simpler to operate** than a text model plus a vision model
  plus an OCR engine: one pull, one warm-up, one thing to debug.
- **It fits comfortably.** ~6 GB of 16 GB leaves ~10 GB headroom, enough to
  add `qwen3:8b` later if measurement shows the text-only path is faster for
  high-volume categorisation.
- **Structured output over tool calling:** every task here is "given this
  input, return this shape". A JSON schema does that more reliably than tool
  calling, and vision-model tool support in Ollama is less proven than
  text-only. No multi-step agentic behaviour is required.

## Alternatives

- **`qwen3:14b` + separate OCR** — stronger text reasoning, but reintroduces
  the OCR dependency and two models to manage.
- **`qwen2.5vl:3b`** (3.20 GB) — the fallback if 7B is too slow on this GPU.
- **`qwen2.5vl:32b/72b`** — exceeds 16 GB VRAM; would spill to CPU.

## Consequences

- Receipt quality now depends on the model's OCR ability. Validate against
  real receipts during Phase 6; if it struggles, OCR can be reintroduced as a
  pre-pass without changing the architecture.
- The GPU backend remains unresolved (ROCm vs Vulkan vs CPU) — Phase 0 spike.
- If 7B proves too slow, step down to 3B before abandoning AI features.
