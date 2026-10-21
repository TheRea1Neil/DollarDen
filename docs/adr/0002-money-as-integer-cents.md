# ADR 0002 — Money as integer cents

**Status:** accepted · **Date:** 2026-10-21

## Context

Money crosses Postgres, JSON, Dart, SQLite and a model's output. Floating
point is wrong for currency, and representations that are safe in one layer
degrade in another.

## Decision

All monetary values are `BIGINT` cents. Named `*_cents` everywhere, in every
layer, with no exceptions. Conversion to a display string happens only at the
UI edge.

## Why

- Binary floats cannot represent `0.10`; errors accumulate across the
  thousands of operations a split engine performs.
- Postgres `NUMERIC` is exact but becomes a float in JSON and in Dart unless
  carefully boxed — the weak link is the wire format, not the database.
- Integers survive every hop unchanged.

## Consequences

- Percentage rules (tithing) must define rounding explicitly. **Round half up
  to the nearest cent**, and the `remainder` rule absorbs any residual so
  allocations always sum exactly to gross.
- The `*_cents` suffix is mandatory; an unsuffixed money field is a bug.
