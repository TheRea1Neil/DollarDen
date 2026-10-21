# no-nonsense-finance

A self-hosted personal budgeting app. Bank data comes from Plaid, lives on
hardware you own, and AI analysis runs on your own GPU — nothing financial is
sent to a third-party model provider.

## The idea

Most budgeting apps track what you *spent*. This one models how you decide to
*allocate* money: every paycheck is split into categories by rules you write,
and every transaction is drawn back out of the right category.

- **Categories with refill rules** — "top food up to $300 each paycheck",
  "keep short-term savings at $2000".
- **Paycheck split instructions** — ordered rules: tithe 10% first, then $350
  rent, then $150 health insurance, then refill buckets, remainder to the car
  fund.
- **Automatic draw-down** — a grocery charge comes out of Food; a payment to
  the church comes out of Tithing.
- **Ask when unsure** — anything ambiguous goes to a review queue instead of
  being silently miscategorised. Receipts can be photographed and split across
  several categories line by line.
- **Goal-aware analysis** — you state what you're trying to achieve; the
  summaries and suggestions are written against those goals.

## Shape of the system

| Piece | Runs on | What it is |
|---|---|---|
| Mobile app | iOS + Android | Flutter, with a local SQLite mirror for offline use |
| API + budget engine | Raspberry Pi (`nando`) | Backend, Postgres, Plaid sync, webhook receiver |
| AI inference | Gaming PC (`steamdeck`) | Ollama + Qwen 3, woken on demand by the Pi |
| Public edge | Pi, via Caddy | HTTPS on `example.org`, already deployed |

The AI host is asleep most of the time. The Pi wakes it with a Wake-on-LAN
call when a job needs it, then lets it idle back to sleep. Users can instead
point the app at their own Ollama instance.

## Planning docs

Read in order:

| Doc | Covers |
|---|---|
| [01 Overview & goals](docs/01-overview-and-goals.md) | What this is, scope, non-goals |
| [02 Architecture](docs/02-architecture.md) | Components, network topology, request flows |
| [03 Data model](docs/03-data-model.md) | Postgres schema, SQLite mirror, sync rules |
| [04 Plaid integration](docs/04-plaid-integration.md) | **Connection-credit conservation**, tokens, webhooks |
| [05 AI architecture](docs/05-ai-architecture.md) | Provider abstraction, wake-on-demand, prompts |
| [06 Budget engine](docs/06-budget-engine.md) | Categories, rules, paycheck splits, categorisation |
| [07 Security](docs/07-security.md) | Secrets, encryption, auth, multi-user |
| [08 Roadmap](docs/08-roadmap.md) | Phased build order and gates |
| [09 Pi operations](docs/09-pi-operations.md) | Resource cleanup, ports, backups, runbook |
| [ADRs](docs/adr/) | Decisions and why |

## Before writing code

Read **[docs/04](docs/04-plaid-integration.md)** first. Production Plaid
connections are a finite resource on this account (10), and the single most
expensive mistake available is losing an `access_token` and having to re-link.
The roadmap gates production access behind a working sandbox build for exactly
this reason.

## Status

Planning. No application code yet.
