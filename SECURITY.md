# Security

DollarDen is software you run yourself. There is no DollarDen service,
no hosted database, and no account with us — every instance belongs to
whoever installed it. That shapes everything below: a vulnerability here
is a vulnerability in software other people are running on their own
hardware, holding read access to their own bank accounts.

## Reporting a vulnerability

**Please do not open a public issue.**

Use GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/TheRea1Neil/DollarDen/security/advisories/new)
(the *Security* tab → *Report a vulnerability*). It is a private channel
between you and the maintainer, and it does not require my email
address or yours.

Helpful to include, as much as you have:

- what an attacker can do, and what they need to start with
  (network access? an account on the instance? a stolen refresh token?)
- the smallest reproduction you can manage, or the request/response pair
- the version — a commit SHA is ideal
- whether it needs AI enabled, Plaid in production, or multiple users

If you only have a hunch and no reproduction, report it anyway and say
so. A vague report about token handling is worth more than silence.

## What I can promise

This is a side project with one maintainer. Being straight with you
about that is more useful than a policy I will not meet:

- I will acknowledge a report within **7 days**.
- I will tell you plainly whether I think it is a real issue, and if I
  disagree I will say why rather than go quiet.
- Fixes land on `main` and I will credit you in the commit unless you
  would rather I did not.
- **There is no bounty**, and no guaranteed fix timeline.
- If something is serious and I cannot fix it quickly, I would rather
  document it loudly in the README than leave people unaware.

## Supported versions

Only the current `main`. There are no release branches and no
backports — if you are running an older commit, the fix is to update.
This will change if the project ever has enough users to need it.

## What is actually at stake

Worth stating, because it sets the severity bar:

| Asset | Why it matters |
|---|---|
| Plaid `access_token` | Read access to the operator's real bank accounts |
| `TOKEN_ENCRYPTION_KEY` | Decrypts every stored Plaid token |
| `JWT_SIGNING_KEY` | Forges sessions for any user on the instance |
| Transaction history | A complete record of someone's spending |
| Database backups | All of the above, at rest |

Anything that discloses a Plaid token, the encryption key, or the JWT
signing key is the most serious class of bug in this project. Anything that
lets one user on an instance read another's data is next.

## How the system tries to defend itself

So you know what to probe, and what is deliberate:

- **Plaid tokens are encrypted at rest** with `cryptography`'s Fernet
  (AES-128-CBC, authenticated with HMAC-SHA256, encrypt-then-MAC).
  Ciphertext lives in `plaid_items.access_token_encrypted`; the key comes
  from `TOKEN_ENCRYPTION_KEY` and is never stored in the database, so a
  stolen dump alone does not yield tokens
- **Argon2id** password hashing — not bcrypt, not SHA-anything
- **Short-lived JWT access tokens** (15 min by default) with refresh
  tokens valid for 30 days
- **The Plaid webhook verifies Plaid's JWT signature** and is rate
  limited; verification is environment-specific (sandbox keys will not
  validate production webhooks, and vice versa)
- **Login, registration and refresh are rate limited** per client IP:
  8 logins per 5 minutes, 3 registrations per hour, 30 refreshes per 5
  minutes. This is mandatory, not optional — the endpoint is
  internet-facing on most deployments.
  Known limitation, so you need not report it: the limiter is a sliding
  window held **in process memory**, so counters reset when the API
  restarts and are not shared across replicas. Fine for the
  single-container deployment this is built for; it is not a defence
  against a distributed attacker
- **Per-user isolation** is enforced in queries, not in the UI
- **Registration is closed by default** (`ALLOW_OPEN_REGISTRATION=false`)
- **AI is optional** and never performs arithmetic (see ADR 0003 and
  ADR 0006), so a compromised or lying model cannot move money

See `docs/07-security.md` for the detail behind these.

## Out of scope

- **Any live instance you do not operate.** Do not test against someone
  else's server, mine included. Run your own — the sandbox is free and
  unlimited, and `deploy/docker-compose.selfhost.yml` will get you
  there.
- **Plaid's own infrastructure.** Report those to
  [Plaid](https://plaid.com/legal/#responsible-disclosure-policy).
- **Operator configuration.** An instance with a weak password, no TLS,
  a committed `.env`, a reused `TOKEN_ENCRYPTION_KEY`, or its admin UI
  exposed to the internet is misconfigured, not vulnerable. If the
  **documentation** led someone there, though, that *is* a bug worth
  reporting — bad defaults and misleading install instructions are in
  scope.
- Missing hardening headers, version disclosure, or scanner output with
  no described impact.

## If you run an instance

The software cannot protect you from these; they are yours:

- Generate `TOKEN_ENCRYPTION_KEY` and `JWT_SIGNING_KEY` fresh, per instance.
  Never reuse the examples, and never commit `.env`.
- Keep `ALLOW_OPEN_REGISTRATION=false` unless you genuinely want
  strangers signing up.
- Put TLS in front of the API. Plaid webhooks require HTTPS anyway.
- Your database backups contain everything. Encrypt them, and store
  them somewhere you would be comfortable losing control of a laptop
  near.
- Losing `TOKEN_ENCRYPTION_KEY` means re-linking every bank. On Plaid's
  Trial plan, real-bank connections are a **lifetime** allowance and
  re-linking spends them. Back the key up before you need it.
