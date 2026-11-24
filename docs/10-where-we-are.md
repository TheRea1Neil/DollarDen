# Where we are

A running status note: what exists, what is known-broken, what is next,
and the things that would be expensive to rediscover. Written
2026-10-23, updated 2026-10-24.

## Shape of the thing

| | |
|---|---|
| Phone app | Flutter, iOS + Android. Installed on Neil's iPhone 11 as `org.dollarden.app` |
| Backend | FastAPI + Postgres, in Docker on the Pi (the server) |
| Public API | `https://budget.example.org` — real cert, works on cellular, no VPN needed |
| AI | Ollama on the gaming PC (`deck`), woken on demand over Wake-on-LAN |
| Model | `qwen2.5vl:7b`. `qwen3:14b` was tried and removed — see ADR 0006 |

413 backend tests, 36 Dart tests.

## What works today

**Budget**
- Categories as balances, with kind, target, alert threshold, display
  order (drag), and archive/restore. The Unallocated sink cannot be
  archived.
- Per-category screen with two tabs: **Activity** (balance, funding rule
  in plain English, what has actually moved) and **Settings** (name,
  kind, both descriptions, target, alert, the funding rule editor, and
  the matchers that file charges here).
- Split plans: `percent`, `fixed`, `refill_to` (with an optional
  per-paycheck cap), `conditional`, `remainder`. Versioned — every edit
  makes a new version, so a historical paycheck still explains itself.

**Paychecks**
- A prompt on the budget screen when a deposit looks like income.
- A review screen that previews the split before anything moves,
  computed **locally** by a Dart port of the engine so dragging
  recalculates instantly.
- Drag to reorder funding priority — **one-off, this paycheck only**.
- Tap a row to change its rule — **saved permanently**. These two being
  different is deliberate and was Neil's call.
- Set money aside before the rules run, so a refund is not tithed on.
- Starvation is named, with the culprit: "Savings is last to be funded
  and took $817, leaving nothing for the ones below it."
- Undo from History, refused when it would leave categories overspent
  unless confirmed.

**Transactions**
- History: everything, filterable by category, treatment and merchant.
  Three outcomes kept visually distinct — filed, deliberately excluded
  (card payments and transfers), and not yet reviewed.
- Direction is unmistakable everywhere: spending `−$14.50`, refunds
  `+$20.00` in green with an arrow.
- Review queue, with deterministic matchers and prior decisions filing
  what they can.

**AI** (all optional — ADR 0003)
- Design a whole budget, or one category, from prose.
- Refine one existing category from an instruction.
- Write a category's description.
- **Assisted review**: walk the backlog, one charge at a time, with a
  suggested category and what backed it.

**Analytics** (added 2026-10-24)
- A second axis: `expense_types` records what a charge WAS, independent
  of which envelope paid for it, so "how much on fast food" is
  answerable across pots. See ADR 0008.
- Insights screen: in/out/net, spend by kind against last period,
  biggest movers, repeating charges, a type against its own median,
  funded-vs-spent per envelope, month pace, top merchants, and an
  explicit "not yet labelled" figure. Every total opens into the rows
  behind it.
- Ask a question in plain English. The model compiles the question into
  a query; SQL computes the figure; the model phrases it. Every answer
  shows the period and filters it was read as. See ADR 0009.
- Expense types screen: the vocabulary (rename, merge, archive) plus a
  labelling queue with AI suggestion per charge.
- Duplicate guard on type creation: names compare as slugs, so "Fast
  Foods" resolves to an existing "Fast food" instead of forking the
  taxonomy.
- Subscription detection is deterministic — three charges, a regular
  gap, a stable amount. No model involved.
- Works entirely with AI switched off; the model only adds prose.

## Known broken or missing

- **`experiments/ask-compile/` has never been run.** The deck was
  unreachable on 2026-10-24, so the accuracy of the question-compile
  step is unmeasured. The display mitigations (showing the resolved
  period and filters, reporting unresolved filters) hold at any
  accuracy, but the feature's quality is unknown.
- **Nothing is labelled retroactively.** All spending from before
  2026-10-24 has no expense type, so the insights breakdown starts
  mostly "Untyped" until the labelling queue is worked through.
- **Expense type is not suggested during paycheck/transaction review
  yet.** The classifier works and is wired into the labelling queue
  only; a charge should get a category and a kind in one pass.

- **Nothing re-queues a stranded AI categorisation.** Jobs are queued in
  one place only -- inside `sync_item`, for transactions CREATED in that
  sync run that ended up with an open review item. One pass, one chance,
  and nothing ever goes back to look.

  A transaction is therefore stranded if it arrived before the feature
  existed (2026-11-04), if its job ended `failed` rather than `deferred`
  (a deferred job retries hourly and eventually runs anyway; a failed one
  is gone), if no AI provider was configured at that moment, or if it
  reached the review queue by a route other than sync -- un-ignoring a
  transaction does exactly that.

  Distinct from the outcomes that are working correctly: `unsure` (the
  model declined, which it does honestly when a merchant string says
  nothing) and `suggested` (it hedged and left the decision). Those are
  waiting for the user. A stranded one is waiting for nothing.

  Since 2026-11-07 they no longer look identical: the review queue
  reports an `ai_state` per item (working / deferred / unsure /
  suggested / failed / none) with the model's own reason, so a
  stranded item is at least visible as one. The sweep that would
  re-queue it is still not built. The fix is a periodic sweep
  queueing `auto_categorise` for anything unreviewed with no job still
  coming; detectable as an open review item with no `ai_jobs` row whose
  `request->>'transaction_id'` matches, or only terminal failures.

  Deferred 2026-11-06 at Neil's call. Not yet measured whether his
  account currently has any.

- The deck can now be powered on from the Pi through a relay across its
  power switch, for the cases Wake-on-LAN cannot reach (power cut, soft
  off with WoL disabled, hard freeze). Last resort only, once, never
  against a machine that answers. `docs/16-deck-power.md`.

- **No push notifications.** The paycheck prompt is in-app only.
  Deferred deliberately, with the reasoning and the plan written up in
  `docs/15-notifications.md` — the short version is that local
  notifications cannot work here (the trigger is server-side, not the
  clock) and the threshold alerts need FCM rather than a 15-minute
  WorkManager poll.
- **No receipt itemisation.** Phase 5d, untouched.
- **No OAuth for Bank of America.** Needs a redirect URI and Universal
  Links. Sandbox institutions link fine.
- **No offline mirror.** The SQLite cache was planned and never built;
  the app is online-only.
- **No real-time sync.** Mutations invalidate local state, so this
  device is always right. A change made elsewhere is not seen until a
  refetch.
- **No overspend cascade.** Neil wants "when Discretionary is empty,
  pull from Short-Term Savings". The engine cannot express it: rules
  fund from a paycheck, and this would move money *between* paydays.
  A design decision, not a patch.
- **Two matcher problems on Neil's live data**, left alone because they
  are his to decide: a `"fun"` matcher that catches `FUNDRAISER`, and
  `madison bicycle center` pointing at two different categories.
- **One unlinked paycheck**: a $500 record not tied to its Oct 9
  deposit, so that deposit can still be double-split.

## Things that cost a lot to learn

**The engine is the source of truth, and the model does not divide
money.** ADR 0006. Asked to split a paycheck, `qwen2.5vl:7b` dropped the
remainder in all 24 runs — 85% of its total error was one category.
Given a scratchpad it allocated $18.4 trillion on a $1,837 paycheck.
`qwen3:14b` was accurate but took 27–87s and did not terminate at all on
one edge case.

**The model is good at picking a category.** 83% precision, ~1.2s,
and it declines when a merchant string says nothing. A different task
from dividing: one label, no arithmetic, nothing to conserve.
`experiments/categorise-arms`.

**Descriptions help only if they discriminate.** Naming a payee
("Always goes to Church of Jesus Christ Donation") or stating a boundary
("Uber and Lyft come out of Date Fund") is what works. Descriptions that
restate the name are worse than none.

**A model omits any property a schema does not require.** Three times
this session: conditional `when`/`then`, then `value_cents`, then
`matched_phrase`. Check `required` before blaming the model.

**Self-reported reasoning cannot be trusted.** Asked to cite the
description that decided its answer, the model quotes when the merchant
appears verbatim and stays silent exactly when a description quietly
changed its mind. Attribution is computed by string match instead.

**Two sign conventions exist.** The review queue is budget-relative
(positive = money left the budget); the ledger is category-relative
(positive = the category gained). `Activity.budgetSignedCents` is the
single translation point. Do not add a second.

## Operating it

- Deploy: rsync to the Pi, then compose build + up. Exact commands in
  `docs/09-pi-operations.md` — read them rather than guessing. Compose
  needs `--env-file ../.env`, and the service is **`nnf-api`**: the
  rename did not touch the server, because the Postgres volume is named
  from the compose project (`nnf_nnf_pgdata`), so renaming the project
  or the volume key starts the database on a new empty volume. Rsync
  `backend` only — the `.env` on the Pi carries settings the local one
  does not, and syncing it over reverts them.
- Run migrations **after** rebuilding the image. Running before uses the
  old image, logs what looks like success, and does nothing.
- Tests: `./scripts/test.sh`, which builds from `pyproject.toml` alone
  in a container so a missing dependency fails locally.
- The phone needs a **release** build. A Flutter debug build is JIT and
  dies instantly when launched without a debugger attached.
- The deck sleeps. Wake it with the `wake-gaming-pc` skill before
  anything that needs the model.

## Next

UI/UX work on the app: making it simple and clear. Tooling for driving
the iOS simulator is being built for that — see
`docs/11-simulator-tooling.md`.
