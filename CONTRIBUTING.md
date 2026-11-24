# Contributing

Thanks for looking. This is a small project with one maintainer, so the
most useful thing you can do is open an issue before writing much code —
not for permission, but so you do not spend an evening on something I
have already half-built or decided against.

Bug reports are always welcome. **Security issues go to
[SECURITY.md](SECURITY.md), not to a public issue.**

## Running the tests

Backend, from the repo root:

```bash
./scripts/test.sh                 # all of it
./scripts/test.sh tests/test_split_engine.py
KEEP_PG=1 ./scripts/test.sh       # keep Postgres between runs, much faster
```

This builds a container from `pyproject.toml` **alone** and starts its
own Postgres on `:55433`. That is deliberate: a local venv accumulates
packages from earlier work and hides a missing dependency declaration
until someone else installs the project. It has already caught exactly
that once.

App:

```bash
cd app
flutter test
flutter analyze
```

Please have both suites green before opening a PR, and say in the
description if something is failing for a reason you could not resolve —
that is much better than silence.

## Things that will get a PR sent back

Not style nits. These are the invariants the project is built on, and
breaking one quietly is how a budgeting app starts lying to people.

**Money is integer cents. Always.** No floats, no `Decimal` in storage,
no "it's only a rounding error". See
[ADR 0002](docs/adr/0002-money-as-integer-cents.md).

**The ledger is append-only.** A mistake is corrected with an opposing
entry, never by editing or deleting the original. Balances are derived,
never typed. If your change makes a correction look like an edit, it is
the wrong shape.

**The engine divides money; the model does not.** AI picks labels and
composes queries. Every amount a user sees came from code or a database
query, not from a model. See
[ADR 0006](docs/adr/0006-allocation-stays-deterministic.md) and
[ADR 0009](docs/adr/0009-questions-compile-to-queries.md).

**AI is optional and the app is whole without it.** With no model
configured nothing may break or hide — transactions wait in a review
queue instead of being filed automatically. Any new AI feature needs a
sensible no-model path. See
[ADR 0003](docs/adr/0003-ai-is-optional.md).

**A fix that could be deterministic must be deterministic.** Do not
reach for the model to paper over a bug in the engine.

## The two split engines

The split engine exists twice: `backend/app/budget/split.py` and
`app/lib/core/split_engine.dart`. The Dart copy is there so a paycheck
preview can recalculate instantly as the user drags categories around,
and **a preview that disagrees with what applying actually does is worse
than no preview at all.**

They are held together by a golden file generated from the real Python
engine. If you touch either engine:

```bash
python3 experiments/gen_split_golden.py > app/test/split_golden.json
cd app && flutter test test/split_engine_test.dart
```

Add a fixture in `experiments/gen_split_golden.py` for whatever you
changed, including cases the engine is supposed to **refuse** —
refusing identically matters as much as the arithmetic. See
`docs/06-budget-engine.md`.

## Database changes

Alembic, in `backend/migrations/versions/`, named
`00NN_short_description.py` following the existing files. Migrations run
automatically on container start via `entrypoint.sh`, so a change
without one works on your machine and breaks on a fresh install.

Give every new column a server-side default that backfills existing rows
correctly, and widen rather than truncate — a `varchar` one character
too short once failed a commit *after* the work had succeeded, which
looked exactly like the AI having gone down.

## Tests

Test the path that runs, not only the path that errors. Two real bugs
here survived a green suite because the tests only ever exercised the
error branch, and a third because nothing ever wrote a realistic value
into a column.

Test names are sentences about behaviour, and a comment explaining *why*
a case exists is worth more than the assertion. Grep the suite for
examples — the style is consistent and borrowing it is welcome.

## Decisions worth writing down

Consequential or non-obvious choices get an ADR in `docs/adr/`,
following the existing numbering. One supersedes another rather than
being edited, and the measurements in a superseded ADR stay — see
[ADR 0005](docs/adr/0005-rocm-works-on-rdna4.md) and
[ADR 0010](docs/adr/0010-vulkan-not-rocm.md), where the numbers held and
one conclusion did not.

## Code style

Match the file you are editing: its naming, its idiom, its comment
density. Comments here explain *why*, especially where something looks
odd — most of the odd-looking code is odd because the obvious version
was wrong, and the comment is the evidence.

Keep commits focused, and write a message that explains the reasoning,
not just the diff.

## What not to commit

- `.env`, keys, tokens, or anything from a real bank or Plaid account
- real transaction data, even anonymised
- `docs/09-pi-operations.md` and `docs/13-test-account.md` — these are
  one deployment's private notes and are gitignored on purpose
- generated artifacts: `app/build/`, `.dart_tool/`, `backend/.venv/`

If you are unsure whether something is sensitive, ask in the issue first.
