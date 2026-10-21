# ADR 0001 — Python + FastAPI for the backend

**Status:** accepted · **Date:** 2026-10-21

## Context

The backend runs on a Raspberry Pi (arm64, ~3.7 GB RAM, Debian 12, Python
3.11.2 already present, Docker 27.4.1). It must call Plaid, receive webhooks,
run a rule engine, and dispatch slow AI jobs.

## Decision

Python 3.11+ with FastAPI, in a container in the existing compose stack.

## Why

- Plaid ships a maintained first-party Python SDK.
- The budget rule engine is business logic where readability beats throughput;
  it will be read and modified far more often than it is optimised.
- `async` suits the webhook-plus-slow-AI pattern without extra machinery.
- Python 3.11 is already on the Pi; the arm64 story is painless.
- Load is two users. Raw performance is irrelevant.

## Alternatives

- **Node/TypeScript** — equally good Plaid SDK, and type sharing with a future
  web client. Rejected only because Python's data/OCR ecosystem is stronger
  and the Pi's toolchain already favours it. A reasonable person could pick
  this instead.
- **Go** — best resource profile, weakest ecosystem fit for the AI/OCR side.

## Consequences

- Need disciplined typing (`mypy`) since Python won't catch money-handling
  mistakes for us.
- The arm64 container build must be verified early.
