# 02 — Architecture

## Topology

```
          ┌──────────────┐
          │  Flutter app │  iOS / Android
          │  + SQLite    │  offline mirror
          └──────┬───────┘
                 │ HTTPS (public) or WireGuard (private)
                 ▼
   ┌─────────────────────────────┐
   │ server — Raspberry Pi        │   your-server
   │                             │
   │  Caddy  :443  TLS edge ─────┼──► already deployed, Let's Encrypt via DuckDNS DNS-01
   │  API    :8080 backend       │
   │  Postgres :5432             │   source of truth
   │  wol-api :51822             │   already deployed
   └──────┬──────────────┬───────┘
          │              │ Wake-on-LAN magic packet
          │              ▼
          │   ┌─────────────────────────┐
          │   │ AI host — a spare PC   │  your-ai-host
          │   │  Ollama :11434          │  asleep unless needed
          │   │  Qwen 3 on RX 9060 XT   │
          │   └─────────────────────────┘
          ▼
    Plaid API  ◄── webhooks ──► https://budget.example.org/plaid/webhook
```

Everything in the Pi box already exists except the API and Postgres. Caddy, the
TLS certificate, DuckDNS updating, and the Wake-on-LAN service are deployed and
verified.

## Why this split

**Postgres on the Pi, not the phone.** The Pi is always on, has a stable
network identity, and is the only component that can receive Plaid webhooks.
It is the source of truth.

**Inference on the gaming PC.** The Pi has ~3.7 GB RAM and no usable GPU; it
cannot run a model worth using. The PC has an RX 9060 XT. It is asleep most of
the time, which is fine because AI work here is inherently asynchronous — a
spending summary does not need to return in 200 ms.

**Caddy already terminates TLS.** The Plaid webhook needs a public HTTPS
endpoint with a valid certificate. That exists. The new API slots in behind the
same Caddy as another route.

## Components

### Backend API (new)

**Language: Python 3.11+ / FastAPI.** Reasons: Plaid publishes a first-party
Python SDK; the budget rule engine is ordinary business logic where
readability matters more than throughput; the Pi already runs Python 3.11; and
async FastAPI handles the webhook-plus-slow-AI-call pattern naturally. Node
would also be fine — the deciding factor is the Plaid SDK and the Pi's existing
toolchain.

Responsibilities:
- Auth (registration, login, refresh tokens)
- Plaid link token minting, public-token exchange, **update mode**
- Webhook receipt and verification
- Transaction sync (cursor-based) and persistence
- The budget engine: categories, rules, paycheck splits, draw-down
- AI job queue and provider dispatch
- Sync endpoints for the mobile mirror

Deployed as a container in the existing `docker compose` stack on the Pi.

### Postgres (new)

Container on the Pi, **its own instance** on host port **5433**, not the
CasaOS Postgres. Nightly `pg_dump` to disk plus off-box copies.

The CasaOS Postgres that previously occupied 5432 has been stopped (see
"Pi capacity" below), but 5433 is used anyway so that re-enabling it later
cannot collide with the finance database.

### Flutter app (new)

One codebase, iOS + Android. SQLite mirror via `drift`. The app is a client:
it never talks to Plaid directly except through the Plaid Link SDK, and never
holds a Plaid `access_token`.

### AI layer

See docs/05. Three providers behind one interface: the Pi-managed deck, a
user-supplied Ollama URL, or disabled.

## Pi capacity

The Pi is the tightest resource in the system: 3.7 GB RAM total. Services not
needed for this project were stopped on 2026-10-21 to make room.

| | Before | After |
|---|---|---|
| RAM used | 1763 MB | **863 MB** |
| RAM available | 2027 MB | **2927 MB** |

Stopped (via `docker compose down` — containers removed, **all data
preserved**):

| Service | RAM reclaimed | Data |
|---|---|---|
| Formbricks stack (app, hub, cube, postgres, redis) | ~870 MB | volumes kept; `pg_dumpall` taken |
| PhotoPrism + MariaDB | ~300 MB | **487 photos / 3.8 GB untouched** in `/DATA/AppData/big-bear-photoprism/originals` |
| CasaOS Postgres | ~30 MB | 109 MB data dir kept; `pg_dumpall` taken |

Still running: the WireGuard stack (Caddy, wg-easy, duckdns), Jellyfin,
Portainer, and the `wol-api` service.

Backups live in `/var/backups/dollarden/pre-dollarden-cleanup-2026-10-21/`. Nothing was
deleted — every service can be restarted with `docker compose up -d` against
its original compose file.

Disk is **not** a constraint: 583 GB free. The cleanup was purely about RAM.

~2.9 GB available is comfortable for Postgres (~200 MB) plus the FastAPI
backend (~150 MB) with substantial headroom.

## Request flows

### Linking a bank

```
app                backend                 Plaid
 │  POST /plaid/link-token                   │
 │ ─────────────────►  /link/token/create ──►│
 │ ◄───── link_token ◄──────────────────────│
 │                                           │
 │  [Plaid Link SDK UI, user authenticates]  │
 │ ◄─────────── public_token ───────────────│
 │                                           │
 │  POST /plaid/exchange {public_token}      │
 │ ─────────────────►  /item/public_token/exchange
 │                  ◄── access_token, item_id
 │                  [encrypt, persist]       │
 │ ◄──── {item_id, institution} ─────────────│
```

The `access_token` never leaves the backend.

### Transactions arriving

```
Plaid ── webhook SYNC_UPDATES_AVAILABLE ──► Caddy ──► backend
                                                       │ verify JWT signature
                                                       │ /transactions/sync (cursor)
                                                       │ upsert transactions
                                                       │ run categorisation
                                                       │ confident → apply to category
                                                       │ unsure   → review queue
                                                       │ evaluate alert thresholds
                                                       ▼
                                                  push notification
```

Polling is a fallback, not the primary path. A freshly linked Item returns zero
transactions on an immediate sync — verified against the sandbox — because
Plaid is still performing the initial pull. The webhook is what tells you data
is ready.

### An AI job

```
app → backend: POST /ai/summary  → job queued, 202 returned
                   │
                   ├─ provider = custom? → call user's Ollama directly
                   └─ provider = deck?
                          │ is it awake?  (TCP probe)
                          │ no → POST /wol/wake?wait=90   [existing service]
                          │ wait for :11434 to answer
                          ├─ POST /api/chat to Ollama
                          └─ persist result, push notification
app ← GET /ai/jobs/{id} → result
```

Jobs are asynchronous with a persisted result. A cold start is ~10 s for wake
plus model load; the UI must never block on it.

## Network access model

### Hostname split

Two hostnames on one IP and one forwarded port. Caddy routes by `Host`.

| Hostname | Serves |
|---|---|
| `example.org` | wg-easy admin UI, `/wol/*` Wake-on-LAN API |
| **`budget.example.org`** | **this app's API and the Plaid webhook** |

This exists because wg-easy already owns `/api/*` on the apex domain; a second
hostname removes the collision rather than renaming the finance API around it.

Three things make it nearly free, all verified:

* **DNS:** DuckDNS wildcards every subdomain to the account IP. No records to
  create — `budget.example.org` already resolves.
* **Port forwarding:** none needed. TCP 443 is already forwarded, and Caddy
  serves unlimited hostnames on it via SNI.
* **Certificates:** DuckDNS answers `_acme-challenge.<sub>.<yourname>.duckdns.org`
  with the account-level TXT, so DNS-01 issues a normal per-subdomain cert.
  Caveat: DuckDNS has a single TXT slot, so certificates for different
  subdomains must be issued sequentially.

| Path | Exposure | Used for |
|---|---|---|
| `https://budget.example.org/plaid/webhook` | public | Plaid only; JWT-verified |
| `https://budget.example.org/*` | public | the mobile app API (JWT-authenticated) |
| `https://wake.example.org/wol/*` | public | existing WoL API, token-auth |
| `your-ai-host:11434` | **LAN only** | Ollama. Never exposed publicly |
| Postgres | **container network only** | never published to the LAN |

Ollama has no authentication. It must never be port-forwarded. The backend
reaches it over the LAN; a remote user reaches it only by being on the
WireGuard VPN.

## Repository layout (planned)

```
dollarden/
├── docs/                  planning (this)
├── backend/
│   ├── app/
│   │   ├── api/           route handlers
│   │   ├── core/          config, security, logging
│   │   ├── db/            models, migrations (alembic)
│   │   ├── plaid/         client, webhook verification, sync
│   │   ├── budget/        rule engine, paycheck splits, draw-down
│   │   └── ai/            provider abstraction, prompts, job queue
│   ├── tests/
│   └── Dockerfile
├── app/                   Flutter
│   └── lib/
│       ├── data/          drift schema, repositories, api client
│       ├── domain/        models, rule types
│       └── ui/            screens, widgets, theme
├── deploy/
│   ├── docker-compose.yml
│   └── Caddyfile.fragment
└── scripts/               backup, restore, token export
```
