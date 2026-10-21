# 08 — Roadmap

Ordered to retire risk early and to keep production Plaid behind a hard gate.
Each phase ends in something demonstrable.

---

## Phase 0 — Spikes (do first, they can invalidate the plan)

Two unknowns can change the architecture. Resolve them before building on top.

- [x] **GPU inference spike — DONE, ROCm works.** See ADR 0005. Ollama 0.40.7
      in a rootless podman container reports `library=ROCm compute=gfx1200`,
      15.8 GiB usable. No Vulkan or CPU fallback needed.
- [ ] ~~Install Ollama in a `podman` container on the~~
      deck (rootfs is immutable — do **not** install to `/usr`; models under
      `/home`). Try the ROCm image first; if RDNA4/gfx1200 is unsupported, try
      a Vulkan build (Vulkan is confirmed present); if both fail, measure
      CPU-only throughput on 16 cores.
      Pull **`qwen2.5vl:7b` (5.97 GB, confirmed in the registry)** and measure
      tokens/sec on each backend, for both a text prompt and a receipt image.
      **Decision point:** if no path gives usable speed, drop to
      `qwen2.5vl:3b` (3.20 GB); if that still fails, `NullProvider` becomes the
      default and AI features become optional.
- [ ] **End-to-end wake → infer → sleep.** Prove the Pi can wake the deck,
      get a completion, and let it idle back down. The wake half is already
      verified (~10 s).
- [ ] Record both outcomes as ADRs.

## Phase 1 — Backend skeleton

- [ ] FastAPI app, health endpoint, structured logging
- [ ] Postgres container on the Pi — **its own instance**, not the existing
      CasaOS Postgres, on a non-conflicting host port
- [ ] Alembic migration 0001: the full multi-user schema (docs/03)
- [ ] Auth: register, login, refresh, argon2id
- [ ] Repository layer that makes `user_id` filtering structural
- [ ] Caddy route added for `/api/*` and `/plaid/webhook`
- [ ] CI: lint, type-check, tests

**Done when:** two users can register and log in over HTTPS, and user B
provably cannot read user A's rows.

## Phase 2 — Plaid in sandbox

- [ ] Plaid client wrapper, `PLAID_ENV` switch, `ALLOW_PRODUCTION` guard
- [ ] `POST /plaid/link-token`, `POST /plaid/exchange`
- [ ] Token encryption at rest
- [ ] `/transactions/sync` loop — `added`/`modified`/`removed`, cursor
      persisted **in the same DB transaction** as the data
- [ ] Sign-convention normalisation + a test asserting it
- [ ] Webhook endpoint **with JWT verification**
- [ ] `/sandbox/item/fire_webhook` used to test the webhook path
- [ ] **Update mode** implemented and tested via `/sandbox/item/reset_login`
- [ ] Pending → posted transition handled without double-counting

**DONE — 2026-10-21.** `scripts/sandbox_e2e.py` passes 25/25 against the live
deployment. Verified against the real database afterwards:

| Check | Result |
|---|---|
| Sandbox bank linked | First Platypus Bank, 14 accounts |
| Transactions synced | 49 added |
| Re-sync idempotent | 49 modified, **0 duplicate transaction ids** |
| Sign convention | 0 rows with both outflow and inflow; 0 negative |
| Cursor persisted | `CAESJXd3ZzlYVmpCalJmNk...`, status healthy |
| access_token at rest | encrypted; no plaintext in the column |
| Token never in API output | confirmed absent from `/plaid/items` |
| **Update-mode re-auth** | **same Item re-authenticated, no new credit** |
| Unsigned webhook | rejected 401 |
| Unauthenticated access | rejected 401 |

## Phase 2.5 — Token redundancy  ✅ DONE 2026-10-21

- [x] `GET /api/backup/tokens` exports encrypted blobs for on-device storage
- [x] `POST /api/backup/tokens` restores them, verifying against Plaid first
- [x] Key fingerprint travels with each blob; mismatch reports `wrong_key`
- [x] Restore repopulates accounts (see the bug below)
- [x] Sync refuses to advance the cursor past unstorable transactions
- [x] `scripts/token_backup_e2e.py` — 18/18, including a simulated total
      database loss recovered from blobs alone

Third off-site copy is **planned, not implemented**.

### Bug this phase caught

Deleting a `plaid_items` row cascades to its `accounts`. The restore did not
repopulate them, so every synced transaction hit the "unknown account" branch
and was **silently skipped while the cursor advanced** — permanent data loss,
since Plaid does not redeliver consumed transactions.

It was not restore-specific. The same path would fire in normal operation
whenever a new account appears at the bank.

Fixed in two places: restore refreshes accounts, and `sync_item` now refreshes
and reprocesses the page on an unknown account, raising `SyncIncomplete`
rather than advancing the cursor if the data still cannot be stored.

## Phase 3 — Budget engine

- [ ] Categories CRUD with archive-not-delete
- [ ] Allocation ledger; `balance` derived, never set directly
- [ ] `category_rules` matcher
- [ ] Split plans + the five rule types, versioned
- [ ] Paycheck detection and confirmation
- [ ] Review queue
- [ ] Property-based tests for every invariant in docs/06

**Done when:** a simulated paycheck splits exactly per the worked example in
docs/06, and sandbox spending draws the right categories down.

## Phase 4 — Flutter app

- [ ] Project setup, theme, navigation, auth screens
- [ ] Home: category balances, next-paycheck estimate
- [ ] Transactions list, detail, manual re-categorisation
- [ ] Review queue UI — the highest-traffic screen; make it fast
- [ ] Category and split-plan editors
- [ ] Plaid Link SDK integration (`plaid_flutter`)
- [ ] `drift` SQLite mirror + outbox sync
- [ ] Offline state clearly surfaced

**Done when:** the full sandbox loop works from the phone, including offline
edits that reconcile on reconnect.

## Phase 5 — AI features

- [ ] Provider abstraction + settings (deck / custom URL / off)
- [ ] Job queue, wake orchestration, job-state UI
- [ ] `categorise` with JSON schema + invariant validation
- [ ] `summary` and `advise`, conditioned on `user_goals`
- [ ] Goals editor
- [ ] `design_categories` producing editable proposals

**Done when:** every AI feature degrades cleanly to `NullProvider`.

## Phase 6 — Receipts

Simplified by the move to a vision model: no OCR dependency.

- [ ] Camera capture + upload
- [ ] `receipt_parse` job — image straight to `qwen2.5vl:7b`
- [ ] Line-item extraction and category assignment, schema-validated
- [ ] Reconciliation UI for tax/discount mismatches
- [ ] User confirmation before any allocation is written

## Phase 7 — Notifications & polish

- [ ] Push notifications
- [ ] Threshold, burn-rate, unfunded-rule, item-broken alerts
- [ ] Pay-period summary view

---

## Phase 8 — PRODUCTION PLAID  ⚠️ hard gate

**Do not start until the docs/04 cutover checklist is fully ticked**, including
a *tested restore*.

- [ ] Production secret in `.env`; `ALLOW_PRODUCTION=true`
- [ ] Separate database from sandbox data
- [ ] Link **one** institution — costs 1 of 10
- [ ] Observe a full pay cycle before linking anything else
- [ ] Verify backups contain the real token and a restore works

## Phase 9 — Second user

- [ ] Brother registers, links his own bank (another credit)
- [ ] Re-run cross-user isolation tests against real data
- [ ] Per-user AI provider settings confirmed independent

## Phase 10 — Operational hardening

- [ ] `/wol/suspend` endpoint so the Pi can idle the deck down
- [ ] Backup monitoring (alert if `pg_dump` hasn't run)
- [ ] Item-health dashboard
- [ ] 2FA on wg-easy (outstanding from the infra work)

---

## Sequencing rationale

**Why spikes first:** the GPU question can invalidate Phase 5 entirely. Better
to know in a day than after building a job queue around it.

**Why sandbox fully before production:** every production link is 1 of 10 and
unrecoverable if the token is lost. The sandbox is free and unlimited. Update
mode in particular *must* be proven in sandbox, because the first time it is
needed in production will be the worst time to discover it is broken.

**Why the budget engine before the app:** it is the part with real
correctness risk and it is far easier to test headless.

**Why AI last among features:** it is the only optional part. Everything else
must work without it.
