# no-nonsense-finance

An envelope budget you run yourself. Your bank data stays on your
hardware, in your house, in a database you own.

It connects to your bank through Plaid, sorts transactions into
envelopes, and keeps an append-only ledger of every movement of money.
Optionally it will categorise transactions for you using a language
model — one you host yourself, or one you pay for, or none at all.

**Status: working, and used daily by its author. Not yet packaged for
strangers.** Installation is being smoothed out; see `docs/17`.

## What makes it different

**Money is divided, not forecast.** Every pound or dollar sits in
exactly one envelope. The sum of the envelopes equals the money you
actually have. There is no projection and no hypothetical.

**Corrections are entries, never edits.** The ledger is append-only. A
mistake is fixed by an opposing entry, so the history of how money
moved stays true. Balances are derived, not typed.

**The engine divides money. The model does not.** Where AI is used it
chooses labels and composes queries; the arithmetic is done in code
and tested. An amount that appears in an answer came from a database
query, not from a model's recollection.

**AI is optional, and the app is whole without it.** With no model
configured, nothing is hidden or broken — transactions simply wait in
a review queue for you instead of being filed automatically.

## What you need

| | |
|---|---|
| **A machine to run it on** | Anything that runs Docker. A Raspberry Pi is plenty |
| **A Plaid account** | Free sandbox to start. Their Trial plan for real banks — see below |
| **The app** | Android and iOS clients connect to your server. Build from `app/`, or install the published client when available |
| **A model (optional)** | Your own Ollama host, or an API key for a hosted one, or neither |

## Install

```bash
git clone <this repo> && cd no-nonsense-finance/deploy
cp ../.env.example .env
```

Fill in `.env`. The three that must be set:

```bash
POSTGRES_PASSWORD=...
JWT_SIGNING_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
TOKEN_ENCRYPTION_KEY=$(python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")
```

> **Keep `TOKEN_ENCRYPTION_KEY` somewhere safe.** It encrypts your
> stored bank connections. Lose it and every bank must be linked again,
> and re-linking spends one of a small, permanent allowance of Plaid
> connections. Back it up before you connect anything.

Then:

```bash
docker compose -f docker-compose.selfhost.yml up -d
```

It migrates its own database on start. Open
`http://your-server:21821/admin/` and the setup wizard will ask you to
create the owner account. **There are no default credentials**, and
nobody can sign themselves up afterwards — the owner creates accounts
from the admin portal.

## Connecting a bank

Plaid is how this reaches your bank. Two things are worth knowing
before you start, because one of them is irreversible.

**Do the whole thing in sandbox first.** Sandbox is free and
unlimited, with fake banks that behave like real ones. Link one,
watch transactions arrive, categorise a few, confirm webhooks reach
you. Only move on when the entire loop works.

**Then apply for Plaid's Trial plan — not production.** Trial gives a
small number of real bank connections. That number is a *lifetime*
allowance: every attempt against a real bank spends one, and removing
a connection does **not** give it back. A typo in the Plaid dashboard
costs one permanently.

Sandbox and real banking live side by side on one server, because
**the environment belongs to the account, not the instance**:

1. Create a **sandbox** account, break things freely, learn the app
2. Sign up for Trial, put the credentials in the admin portal
3. Create a **production** account and connect your real bank there
4. Keep the sandbox account — it stays free, for trying anything risky

An account's environment is fixed when it is created. A sandbox access
token means nothing in production, so an account that could switch
would strand every bank it had already linked.

### Webhooks

Transactions arrive fastest when Plaid can call your server, which
needs an HTTPS address reachable from the internet. Without one, the
server **pulls on a schedule instead** — hourly by default. Plaid only
reads most banks a few times a day, so an hourly pull is close to what
webhooks would have told you anyway.

Adding HTTPS later costs nothing: change the address and press
**re-point webhooks** in the admin portal, which moves existing
connections without re-linking them.

## HTTPS

The app speaks plain HTTP on a port. Certificates belong to a reverse
proxy — Caddy, nginx, a Cloudflare tunnel, Tailscale. That is not a
dodge: Let's Encrypt needs port 80 reachable or DNS API credentials,
neither of which this container has, and binding 443 inside it would
fight whatever else runs on your host.

On your own network, plain HTTP is fine and needs nothing.

## Choosing an AI, or not

Three options, and the first is a real one:

**None.** Everything works. Transactions wait in the review queue for
you to file. Nothing is hidden or degraded.

**Your own model.** An [Ollama](https://ollama.com) host on your own
network — a spare desktop with a GPU is ideal. Your transactions never
leave your house. If that machine sleeps, the server can wake it on
demand: give it a URL to call, and it will.

**A hosted model.** An API key for a provider. Easiest to set up, and
the trade is worth stating plainly: **merchant names, amounts and the
goals you write are sent to that company, and you pay per use.** Both
choices are legitimate; a budgeting app is the wrong place to be
vague about which one you made.

All of it is configured in the admin portal, and can be changed later.

## The phone app

The app has no server built into it. On first run it asks for your
server's address, then signs you in — the way a Jellyfin client does.
One build works against any instance.

Build it yourself from `app/` with Flutter, or install the published
client and point it at your server.

## Documentation

`docs/` holds the design in full, and `docs/adr/` the decisions with
the reasoning that produced them — why money is integer cents, why
corrections are entries, why the engine divides money and the model
does not. They are written to explain *why*, not just what.

`docs/17-open-source-plan.md` is the live plan for making this
installable by people who are not its author.

## Licence

MIT. See `LICENSE`.
