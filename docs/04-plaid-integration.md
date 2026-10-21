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

## Rules for not burning credits

These are requirements, not suggestions.

1. **Never call `/item/remove`** except deliberately retiring a bank forever.
   It frees the Item; re-linking consumes a new credit.

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
- [ ] `pg_dump` backup runs nightly **and a restore has been performed**
- [ ] Token export script written and its output stored safely
- [ ] Production secret obtained and set; `PLAID_ENV=production`, `ALLOW_PRODUCTION=true`
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
