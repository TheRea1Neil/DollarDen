# 04 — Plaid Integration

> **Read this before touching production.** The account has **10 production
> connections**. They are consumed by linking, and the expensive failure is not
> a bug — it is losing an `access_token` and having to link the same bank again.

## Vocabulary

| Term | Meaning |
|---|---|
| **Item** | One connection between one user and one institution. This is the unit that consumes a credit. |
| `public_token` | Short-lived, returned by Link in the app. Useless after exchange. |
| `access_token` | **Long-lived, does not expire.** Grants ongoing access to the Item. The thing to protect. |
| `item_id` | Stable identifier for the Item. Safe to log. |
| `cursor` | Opaque position marker for `/transactions/sync`. Persist per Item. |

One Item can expose many accounts. Linking a bank that holds a checking
account, a savings account and a credit card is **one** connection, not three.
This matters for planning: 10 connections is 10 *institutions*, which is
plenty.

## Verified sandbox behaviour

The following was run against the real sandbox during planning and is
reproducible:

```
/institutions/get          → 200, 10194 institutions   (credentials valid)
/sandbox/public_token/create (ins_109508, transactions) → public-sandbox-...
/item/public_token/exchange → access-sandbox-...        (the token to persist)
/accounts/get              → 14 accounts (checking, savings, credit card, …)
/transactions/sync         → added: 0, has_more: false
```

**That last line is the important one.** A freshly linked Item returns **zero
transactions** on an immediate sync, because Plaid is still performing the
initial pull. Code that links an account and then immediately syncs will
conclude there is no data. The correct trigger is the webhook.

## Account tier: Trial plan

| | |
|---|---|
| Cost | **Free** — production API access, no commitment |
| Production Items | **10**, a hard lifetime cap (see rule 1 below) |
| OAuth institutions | **Included**, Bank of America named explicitly, along with Chase, Wells Fargo, Capital One, Citi, PNC, US Bank, Amex |
| Products | Auth, Transactions (+ Recurring, Refresh), Balance, Identity, Assets, Liabilities, Investments, Statements |
| Availability | US/Canada teams created on or after 2026-04-15 |

**Trial skips the full OAuth registration.** Plaid gathers what it needs at
signup, so the MSA addendum, security questionnaire, company details and app
profile are not required. Those become mandatory only on upgrading to a paid
plan — at which point OAuth access must be re-established through the full
registration flow.

Individual developers are explicitly supported: you may use your own name as
the legal entity, and Plaid does not expect a solo developer to have the
security posture of a public company.

## OAuth institutions

Most large US banks are OAuth, **including Bank of America** (`ins_127989`,
`oauth: true`). OAuth hands the user to the bank's own site to authenticate,
then needs a **registered redirect URI** to return them to the app.

With `PLAID_REDIRECT_URI` empty, an OAuth link dead-ends: the user picks
their accounts and is dumped back at the bank's login, no `public_token` is
ever produced, and no Item is created. Observed exactly this during
development against `ins_127989`.

To support OAuth in production:

- [x] register the Android package name `org.nonsensefinance.nnf`
      (Build → API → Allowed Android package names). A redirect URI is
      still needed for iOS only
- [ ] iOS Universal Link / Android App Link — an
      `/.well-known/apple-app-site-association` served from
      `budget.example.org` (the HTTPS host already exists)
- [x] pass `redirect_uri` (iOS) / `android_package_name` (Android) on
      `/link/token/create` — mutually exclusive, chosen by platform
- [x] handle the resumed Link session on return — verified end to end on
      a Pixel 9 Pro against sandbox Bank of America, 2026-11-03: OAuth
      handed off to the bank, returned via `plaid-link-oauth://handoff`,
      and the Item was created

**In sandbox, every institution is a simulation.** `ins_127989` is a fake
Bank of America that only accepts `user_good` / `pass_good`; real
credentials cannot work there by design. For sandbox testing use one of the
282 non-OAuth institutions — `First Platypus Bank` is the canonical one.

## Sandbox webhooks 401 after the production cutover

Expected, and not a fault. Plaid keeps sending webhooks for Items
created in sandbox, but verification keys are environment-specific:

> *key_ids are specific to Plaid environments, and verification
> requests must be made to the same environment that the webhook was
> sent from*

With `PLAID_ENV=production` the server can only resolve production
keys, so a sandbox-originated webhook can never verify and is rejected
with 401. Observed 2026-11-05: production webhooks for the real Bank
of America Item returned 200, while webhooks for a leftover sandbox
Item from Android OAuth testing returned 401 from a different Plaid
IP.

`plaid_items.last_webhook_code` records `rejected:<reason>` against the
Item, so the two cases can be told apart after the fact rather than
guessed at from Caddy's access log.

Do not "fix" this by relaxing verification.

## Rules for not burning credits

These are requirements, not suggestions.

1. **Never call `/item/remove`. Ever.**

   This was documented incorrectly at first. On the **Trial plan**, removing
   an Item does **not** free a slot against the 10-Item cap:

   > calling `/item/remove` on the Trial plan does not free up slots against
   > your 10-Item cap

   So the 10 is a **hard lifetime cap**, not a recyclable pool. Removing a
   bank loses the connection *and* permanently burns the slot. There is no
   situation in which calling it helps, which is why the app exposes no
   disconnect action at all.

2. **Re-authentication uses update mode, not a new link.** When an Item goes
   bad (`ITEM_LOGIN_REQUIRED`, expired MFA, changed password), the fix is to
   create a link token *with the existing `access_token` attached*:

   ```
   POST /link/token/create  { ..., access_token: "<existing>" }
   ```

   Launching Link with that token re-authenticates **the same Item in place**.
   No new Item, no credit consumed. Building this path is mandatory before
   production, because needing it is routine, not exceptional.

3. **The `access_token` must survive hardware failure.** It is a string in a
   Postgres row on an SD-card-adjacent device. Losing it costs a credit.
   Therefore:
   - encrypted at rest with `TOKEN_ENCRYPTION_KEY` (never the DB password)
   - nightly `pg_dump`, retained off-box
   - a documented `scripts/export-tokens.py` producing an encrypted file to
     store somewhere durable
   - **restore tested before production linking**, not after

4. **The encryption key is as precious as the tokens.** Losing it is equivalent
   to losing every token. Keep a copy off the Pi, in a password manager.

5. **Sandbox and production are separate secrets.** Same `client_id`, different
   `secret`, different base URL. One `PLAID_ENV` variable controls both, and
   the backend refuses to start if `PLAID_ENV=production` while
   `ALLOW_PRODUCTION=false`. Production is opt-in and loud.

6. **One environment per database.** Never mix sandbox and production Items in
   the same Postgres instance. A sandbox token in a production row, or vice
   versa, produces confusing failures at the worst time.

## Three-copy token redundancy

The `access_token` is the asset whose loss costs a connection credit, so it
lives in more than one place.

| Copy | Where | Form | Status |
|---|---|---|---|
| 1 | Postgres on the Pi | encrypted (`access_token_encrypted`) | live |
| 2 | Phone, OS keychain | **encrypted blob, identical ciphertext** | live |
| 3 | Off-site | encrypted blob | *planned, not implemented* |

### The phone copy is ciphertext, never a usable token

`GET /api/backup/tokens` returns the exact `access_token_encrypted` bytes,
base64-encoded, still sealed with `TOKEN_ENCRYPTION_KEY` — which never leaves
the server. The phone stores an opaque string it cannot open.

This preserves the rule in docs/07 that the app never holds a usable Plaid
token, while still giving a real second copy. Recovery needs both halves:

```
phone blob  +  TOKEN_ENCRYPTION_KEY (password manager)  =  restore
```

Either alone is useless. A lost or compromised handset does not leak bank
access; a stolen database dump does not either.

**The key is now the single point of failure.** Both the database column and
the phone blob are sealed with it. It must be backed up somewhere that is not
the Pi and not the phone — a password manager is the intended home.

### Restoring after losing the Pi

`POST /api/backup/tokens` with the stored blobs. Each one is:

1. checked against the server's current key fingerprint — a mismatch reports
   `wrong_key` rather than a confusing decryption failure;
2. decrypted;
3. **verified against Plaid with `/item/get` before anything is written**, so
   a restore cannot quietly populate the database with dead tokens;
4. written with a **null `sync_cursor`**, deliberately. A fresh cursor re-pulls
   full history, which is the safe direction. Restoring a stale cursor would
   silently skip every transaction that happened in between.

Overwriting an existing healthy Item requires `overwrite_existing=true`.

### Why this does not weaken the security model

The phone already holds transaction history in its local SQLite mirror. Adding
an *encrypted* blob it cannot read does not widen what an attacker gains from
the device. Storing a *usable* token there would have — which is why this
design stores ciphertext instead.

## Token lifecycle

```
Link (user authenticates)
      │
      ▼  public_token  (minutes)
/item/public_token/exchange
      │
      ▼  access_token  (indefinite)
  encrypt → plaid_items.access_token_encrypted
      │
      ├── /transactions/sync  (cursor, incremental, forever)
      │
      ├── ITEM_LOGIN_REQUIRED?
      │      └─► update mode  ─► same Item re-authenticated, SAME token
      │
      └── /item/remove  ◄── DO NOT CALL (burns the connection)
```

## Transactions: use `/transactions/sync`

Not `/transactions/get`. `sync` is cursor-based and gives explicit
`added` / `modified` / `removed` arrays, which maps directly onto idempotent
upserts.

```
cursor = stored cursor for this item   (null on first call)
loop:
    r = /transactions/sync(access_token, cursor)
    apply r.added, r.modified, r.removed
    cursor = r.next_cursor
    until not r.has_more
persist cursor atomically with the applied changes
```

Notes:
- Persist the cursor **in the same transaction** as the data it describes.
  Saving the cursor first and crashing loses transactions permanently.
- `removed` is real — pending transactions get replaced by posted ones with a
  different `transaction_id`. Handle it or you will double-count.
- A pending transaction becoming posted is a remove + add, not a modify. The
  budget engine must not draw down twice.

## Webhooks

Endpoint: `https://example.org/plaid/webhook` — already has valid TLS
via the deployed Caddy.

Webhooks that matter:

| Webhook | Meaning | Action |
|---|---|---|
| `SYNC_UPDATES_AVAILABLE` | New transaction data | Run the sync loop |
| `INITIAL_UPDATE` / `HISTORICAL_UPDATE` | First pull finished | Run the sync loop |
| `ITEM_LOGIN_REQUIRED` | Item broken | Flag it; prompt update-mode re-auth |
| `PENDING_EXPIRATION` | Consent expiring | Prompt update-mode re-auth |
| `NEW_ACCOUNTS_AVAILABLE` | User added an account at the bank | Offer update mode |
| `ERROR` | Item-level error | Surface it; do not retry blindly |

**Verification is mandatory.** Plaid signs each webhook with a JWT in the
`Plaid-Verification` header. The endpoint is public, so an unverified handler
is an open door for forged sync triggers. Verify by fetching the key via
`/webhook_verification_key/get` (cache it), checking the JWT's `ES256`
signature, and confirming the body's SHA-256 matches the `request_body_sha256`
claim. Reject anything that fails, and rate-limit the route.

Webhooks are at-least-once. Handlers must be idempotent.

## Sandbox testing without burning anything

- `/sandbox/public_token/create` mints an Item with no UI at all — ideal for
  automated tests.
- `/sandbox/item/fire_webhook` triggers webhooks on demand, so the webhook path
  can be tested end to end before any real bank is involved.
- `/sandbox/item/reset_login` forces `ITEM_LOGIN_REQUIRED`, which is how the
  **update-mode path gets tested** — do this before production, not after.
- Credentials for the Link UI in sandbox: `user_good` / `pass_good`.
- Sandbox Items are free and disposable. Create and destroy them freely.

## Production cutover checklist

Do not start until every box is ticked.

- [ ] Sandbox link flow works from the real Flutter app, not just curl
- [ ] Webhook received, JWT-verified, and triggers a sync
- [ ] `/transactions/sync` loop handles `added`/`modified`/`removed` idempotently
- [ ] Update-mode re-auth tested via `/sandbox/item/reset_login`
- [ ] Tokens encrypted at rest; `TOKEN_ENCRYPTION_KEY` backed up off-box
- [x] `pg_dump` backup runs nightly **and a restore has been performed** —
      `deploy/backup.sh` + `nnf-backup.timer`; it restores into a scratch
      database and compares row counts on every run (docs/09)
- [ ] Token export script written and its output stored safely
- [ ] Production secret obtained and set; `PLAID_ENV=production`,
      `ALLOW_PRODUCTION=true` — use `deploy/go-production.sh`, which proves
      the secret against Plaid before writing it (docs/09)
- [ ] A separate database (or at minimum schema) from sandbox data
- [ ] Decided which institution to link first — and accepted it costs 1 of 10

Then link **one** bank. Verify the full loop for several days before linking
another.

## Credential hygiene

The sandbox `client_id` and `secret` used during planning were shared in a
chat transcript. Sandbox secrets carry little risk — they reach only fake data
— but the `client_id` is shared with production. Rotate the sandbox secret in
the Plaid dashboard when convenient, and make sure the **production** secret is
never pasted anywhere, only placed in `.env` on the Pi.
