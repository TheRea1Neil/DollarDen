# ADR 0003 — AI is an enhancement, never a dependency

**Status:** accepted · **Date:** 2026-10-21

## Context

AI inference depends on a desktop PC that is asleep by default, reached over
the LAN, on an RDNA4 GPU whose ROCm support is unverified. Meanwhile the app's
core job — splitting paychecks and tracking category balances — is
deterministic arithmetic.

## Decision

Every AI feature degrades to a deterministic fallback. `NullProvider` is a
first-class, fully supported configuration, and the product is useful with it.

## Why

- The GPU may not work; that must not sink the project.
- The AI host may be asleep, offline, or mid-game.
- Deterministic rules handle most real spending (repeat merchants) faster and
  more predictably than a model.
- A budgeting app that stops working because an LLM is unreachable is not a
  budgeting app.

## Consequences

- The review queue is load-bearing, not a fallback path — it is where
  everything uncertain lands, with or without AI.
- Deterministic rules are evaluated **before** the model, always.
- The model proposes; validated deterministic code writes to the ledger.
