# 07 — Security

Threat model: a self-hosted app on a residential connection, holding bank
*read* access for two known users, with a public HTTPS surface.

## What is actually at stake

| Asset | Impact if lost | Impact if leaked |
|---|---|---|
| Plaid `access_token` | **Burns a connection credit** | Read access to bank transactions |
| `TOKEN_ENCRYPTION_KEY` | Equivalent to losing every token | Decrypts all stored tokens |
| Postgres data | Loss of history and tokens | Full financial history |
| User passwords | — | Account takeover |

No component can move money — Plaid access here is read-only. The realistic
worst case is disclosure of transaction history, not theft.

## Secrets

- `.env` on the Pi, mode `600`, never committed. `.gitignore` covers it.
- `.env.example` documents every variable with no values.
- Secrets are never logged. Log `item_id`, never `access_token`.
- The sandbox credentials used in planning were shared in chat — rotate the
  sandbox secret when convenient. The **production** secret must only ever be
  typed into `.env` on the Pi.

## Token encryption

Plaid `access_token`s are encrypted at rest with AES-GCM (via `cryptography`
Fernet or equivalent) using `TOKEN_ENCRYPTION_KEY`, which is **separate from
the database password**. A stolen database dump alone does not yield tokens.

```
plaintext token ──encrypt──► plaid_items.access_token_encrypted (bytea)
                               decrypted in memory only, per request
```

**Back up the key off the Pi.** Losing it means re-linking every bank, which
costs connection credits. Store a copy in a password manager.

## Backups

The thing that makes a token durable is a tested restore, not a backup job.

- Nightly `pg_dump` to `/home/neil/backups/` on the Pi.
- Weekly copy off-box.
- `scripts/export-tokens.py` → encrypted file containing `item_id` +
  `access_token` pairs, stored separately. This is the last-resort recovery
  path that avoids burning credits.
- **Restore must be tested before production linking.** An untested backup is
  a guess.

## Authentication

- Argon2id password hashing (not bcrypt, not SHA-anything).
- Short-lived JWT access tokens (~15 min) + long-lived refresh tokens.
- Refresh tokens stored **hashed** in `sessions`, revocable, rotated on use.
- Rate-limit login: the API is internet-facing.
- No password reset emails in v1 (two known users) — admin reset via CLI.

## Multi-user isolation

With two users who know each other, the temptation is to be casual. Don't:

- **Every query filters by `user_id`.** Enforced at the repository layer, not
  left to individual handlers.
- Consider Postgres row-level security as defence in depth.
- Integration tests must include "user B cannot read user A's data" for every
  endpoint. This is the single most likely serious bug in a two-user app.

## Public surface

| Route | Auth |
|---|---|
| `/api/*` | JWT required |
| `/plaid/webhook` | **JWT signature verification from Plaid** (docs/04), rate-limited |
| `/wol/*` | Bearer token (existing service) |
| `/health` | none; returns nothing sensitive |

Not exposed publicly, ever: Postgres, Ollama (11434), the Pi's SSH.

## Mobile

- Tokens in iOS Keychain / Android Keystore — never `SharedPreferences`.
- SQLite mirror holds transaction history; enable SQLCipher or rely on
  OS-level encryption with `FileProtection`. Decide before shipping.
- Certificate pinning is reasonable later given a fixed domain; not v1.
- The app never holds a Plaid `access_token`. Only the backend does.

## Known accepted risks

- **Residential uptime.** The Pi is a single point of failure. Mitigated by
  backups, not redundancy.
- **DuckDNS dependency.** If DuckDNS is down, the webhook URL fails to
  resolve. Plaid retries, so this degrades rather than loses data.
- **wg-easy admin UI is internet-facing.** Protected by a strong password;
  **2FA should be enabled** — still outstanding.
