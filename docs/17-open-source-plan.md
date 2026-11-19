# 17 — Making this open source

Plan of record for turning a working personal deployment into something
another household can install. Written 2026-11-17.

## Decisions already taken

| Question | Decision |
|---|---|
| Who runs an instance | **One household per instance.** Self-hosted, one Plaid account, a few users who trust each other. Not multi-tenant, not a hosted service. |
| Bank access | **Sandbox first, then Plaid's Trial plan.** Never production. |
| Licence | **MIT.** |
| Repo strategy | **Adapt this repo.** Private operations move to a separate private repo. |
| Choosing a server | **The app picks a backend first, then logs in**, as Jellyfin does. |
| Accounts | **Created by the owner in the admin portal.** No public registration. |
| First run | **A wizard creates the owner account.** No default credentials. |
| Sandbox vs real | **A property of the account, not the instance.** Both live side by side. |
| Secrets | **Encrypted in the database**, with the root key in the environment. |

### Why adapt rather than start clean

The usual reason to start a fresh repo is that the history has secrets
in it. This one does not: `.env` was never committed and 106 commits
contain no secret-shaped strings. The only credential anywhere is one
throwaway test-account password, which is rotated rather than scrubbed.

With that gone, the argument reverses. The 26 documents and 10 ADRs are
the most valuable thing in this repository — they record *why* money is
integer cents, why the engine divides and the model does not, why
corrections are entries instead of edits. A fresh repo keeps the code
and throws away the reasoning. Two repos also drift, and the private
fork always ends up the neglected one.

## What the audit found

| | Total | Specific to this house |
|---|---|---|
| Backend | 14,663 lines | **~419 (2.9%)** — `ai/hardpower.py`, `ai/gpu.py`, `DeckProvider` |
| Flutter app | 15,925 lines | **2 lines** — the hardcoded base URL |
| `deploy/` | 526 lines | 100% — but these are deployment artifacts, not the app |
| Docs | 26 files | ~5 are personal runbooks |

Every hardware-specific subsystem is already behind a setting that
defaults to empty: `wol_api_url`, `gpu_probe_url`, `deck_power_url`.
ADR 0003 — AI is optional, the app must work fully without it — forced
that shape years before anyone thought about publishing, and it means
another instance simply never sets them. **Portability was mostly paid
for in advance.**

The budget engine, the ledger, the two taxonomies, the analytics and
the Plaid integration are already general. None of them know where they
are running.

## What actually blocks another household

Not the hardware. These:

1. **The app cannot point at another server.** `defaultBaseUrl` in
   `app/lib/data/api_client.dart` is a compile-time constant. Without
   fixing this nobody can use the app without rebuilding it themselves.
   Single biggest blocker.
2. **There is no install guide.** All 16 documents describe *this*
   deployment. None of them says how to make a new one.
3. **Registration is open.** `POST /auth/register` is rate limited and
   nothing else. On a public URL, anyone who finds it can register and
   spend the owner's finite Plaid connections.
4. **Plaid credentials are instance-wide env vars**, set by editing
   `.env` and restarting. Fine for one person who wrote the thing;
   not a setup experience.
5. **Only Ollama-shaped AI.** `build_provider` understands `deck`,
   `custom` (also Ollama) and `none`. No OpenAI-compatible option,
   which is what most people would reach for first.

## The admin portal

### Why it is a web page served by the backend, not a screen in the app

Chicken and egg. The admin portal is where Plaid credentials and the
server's identity are configured — and the phone app cannot be used to
configure the server it has not been told how to reach yet. The first
thing a new operator needs is a URL they can open in a browser on the
same machine they just installed.

So: served by FastAPI at `/admin`, owner-authenticated, no Flutter
involved.

### What it is for

**Plaid** — client ID, secret, environment, webhook URL, Android
package name / iOS redirect URI. A *Test credentials* button that calls
Plaid and reports back. A count of connections used against the Trial
allowance, because that number is finite, irreversible and currently
invisible.

**AI** — provider type, base URL, model names, and a *Test* button that
reports what actually answered. Per-user overrides already exist on the
`User` model (`ai_provider`, `ai_base_url`); this sets the instance
default.

**Users** — create, list, revoke. Public registration is removed
entirely rather than gated: a household instance has no reason to let
strangers sign up, and an open endpoint on a public URL is how someone
else ends up spending your finite Plaid connections. Each account is
created as either a sandbox or a real account (see below).

**Health** — when Plaid last reached the webhook and whether it was
accepted, job queue depth and recent failures, whether the AI host
answers, current migration version.

**Encryption key** — status and a backup reminder. Losing
`TOKEN_ENCRYPTION_KEY` means every stored bank connection is
unrecoverable, and each re-link costs one of the finite Trial
connections. This deserves to be stated somewhere the operator will
actually read it.

### One security note

Moving Plaid credentials out of `.env` and into the database means they
sit at rest in Postgres. They must be encrypted with the same
`TOKEN_ENCRYPTION_KEY` already protecting Plaid access tokens — not
stored in plain columns.

## First run

The wizard creates the owner account. There are no default credentials.

`admin` / `admin` is a recognisable pattern, but it is the wrong one
here: this instance holds Plaid access tokens for real bank accounts,
and anything with a well-known default on a public URL is found by
scanners within hours. Jellyfin — the model for the server picker and
for admin-created accounts — does not do it either; it runs a first-run
wizard, for the same reason.

The wizard also needs **protecting from the land grab**. An instance
that is reachable before setup is finished belongs to whoever opens it
first. Setup should therefore be completable only from localhost, or
require a one-time token the container prints to its own logs.

Steps: create the owner, enter the Plaid credentials, optionally create
a sandbox account to try the whole thing safely.

## Sandbox and real money, side by side

**The environment is a property of the account, not the instance.** An
account is created as either `sandbox` or `production` and cannot be
changed afterwards.

This falls out of something the codebase already has. Every table is
scoped by `user_id` and that isolation is covered by
`tests/test_auth_and_isolation.py`, so a sandbox account is *already* a
completely separate budget: its own categories, ledger, transactions
and balances. Nothing has to be partitioned, because it already is.

The alternative — one environment per instance, flipped when you are
ready — looked simpler and is worse. Sandbox access tokens are not
valid in production, so flipping strands every existing connection; and
worse, anyone who used the sandbox *properly* (categorising, splitting
a paycheck, allocating money) has a budget full of imaginary money that
switching credentials does not clean up. It would need a destructive
"go live" step that wipes test data. Making the account the boundary
removes the problem rather than cleaning up after it.

So the recommended path for a new operator is:

1. Finish the wizard; it offers to create a **sandbox account**.
2. Use it properly — link a fake bank, let transactions arrive, let the
   webhook fire, categorise a few. Everything is free and unlimited.
3. When satisfied, sign up for Plaid's **Trial** plan and enter those
   credentials.
4. Create a **second account**, marked as real, and connect the actual
   bank there.

The sandbox account stays. It is where to try anything risky later,
and it costs nothing.

### What this requires

- `User.plaid_env`, set at creation by the owner, immutable afterwards.
  Immutability is what makes "never mix" structural: a flippable flag
  would strand that account's existing Items.
- Both credential sets stored. Plaid issues one `client_id` and a
  **separate secret per environment**, so it is one ID and two secrets.
- `PlaidClient` built from the account's environment rather than from
  instance settings, which is where it reads them today.
- **One webhook path per environment** (see below).
- The connection counter in the admin portal counts **real Items
  only**. Sandbox connections are free and unlimited; counting them
  would make the number that matters meaningless.

### Webhooks, with two environments

Both kinds of account must receive webhooks, and the obstacle is that
Plaid's verification key is fetched from the environment's own host
(`client.webhook_verification_key(key_id)`). To verify a request you
must already know which environment it came from. Today that is free,
because there is only ever one.

**The solution is a separate path per environment:**

```
/api/plaid/webhook/sandbox
/api/plaid/webhook/production
```

The webhook URL is set per link token, not globally — `create_link_token`
already takes a `webhook` argument and only falls back to
`settings.plaid_webhook_url`. So an account's Items are registered
against the path matching their environment when they are linked, and
the path tells the handler everything it needs before it touches the
request. No guessing, and no untrusted input read before verification.

Two alternatives were considered and are worse:

- **Read `item_id` from the unverified body to decide.** Actually safe
  — the signature must still pass and the body hash is checked, so
  nothing forged gets accepted — but it means parsing untrusted input
  before verification. That is a pattern worth not establishing in a
  publicly reachable handler.
- **Try both environments.** Works, but doubles key fetches on every
  miss and turns one genuine failure into two confusing log lines.

**One bug to fix while doing this.** `_key_cache` in `plaid/webhook.py`
is keyed by `key_id` alone. With two environments in one process, a
`kid` that appeared in both would serve the wrong environment's key.
Key the cache by `(environment, key_id)`. One line, and the kind of
thing that otherwise surfaces months later as webhooks that mysteriously
stop verifying.

**And the reverse proxy.** The Caddy fragment rewrites the exact path
`/plaid/webhook` to `/api/plaid/webhook`; with two paths that becomes a
prefix rewrite. A webhook that 404s is invisible from Plaid's side —
the Item stays healthy and transactions just never arrive in real time
(this already happened once; see `deploy/Caddyfile.fragment`).

## Getting to it, and HTTPS

The API is published on **port 21821** (`NNF_PORT`), so a new install is
reachable at `http://your-server:21821` with nothing in front of it.

**The app does not terminate TLS, and should not.** Certificates are
the reverse proxy's job in every comparable self-hosted project, for
good reasons: Let's Encrypt needs either port 80 reachable or DNS API
credentials, both outside the app's control; and binding 80/443 inside
this container would fight whatever else is on the host. A wizard
cannot honestly offer "click here for HTTPS".

So the setup is tiered, and the first tier requires nothing:

1. **Plain HTTP on your own network.** Works immediately. Plaid will
   not send webhooks to an unencrypted URL, so the server pulls on a
   schedule instead — hourly by default, adjustable, floored at two
   minutes (`PLAID_SYNC_INTERVAL_MINUTES`, **built 2026-11-17**). Plaid
   itself only reads a bank a few times a day, so hourly is close to
   the ceiling of what webhooks would have told you anyway.
2. **A reverse proxy with a domain.** Caddy gets a certificate on its
   own given a domain pointing at the host — genuinely a couple of
   lines. Webhooks start working, so transactions arrive in real time.
3. **Tailscale or a Cloudflare tunnel**, for anyone who cannot forward
   ports. Worth documenting; neither is required.

Shipping an optional `docker-compose.https.yml` with Caddy and one
domain variable makes step 2 about as small as it can be made.

The schedule stays on with webhooks working: it is the backstop for a
missed one. It does **not** pause during quiet hours, which exist to
leave the inference machine asleep — a pull makes a network call and
wakes nothing, and anything it queues for the AI is already held by the
job gate until morning.

Running it against the real database immediately earned its keep. The
first pass tried six connections: one real one synced, and five were
sandbox leftovers on old test accounts being called against production,
where their tokens mean nothing. Left alone the schedule would have
retried all five hourly forever. A Plaid error is now sorted into three
kinds — needs re-authentication (the user can fix it), **dead** (the
token can never work: revoked, deleted, or issued in another
environment), and ordinary failure — and the schedule skips the dead
ones. A person pressing refresh still tries everything, because that is
an explicit ask. Second pass: five skipped, zero errors.

### Moving later must be free

Going from (1) to (2) means the server's address changes, and two
things have to follow it.

The app is fine: pointing it somewhere else is now the server picker,
on each device, once.

**The connections are not fine by default.** Plaid fixes an Item's
webhook URL at link time, so the old connections keep calling the old
address after a move. Nothing errors — the Item stays healthy,
`/item/get` still reports a webhook — and transactions simply stop
arriving in real time for the accounts held longest. The obvious
remedy, re-linking, spends one of a small lifetime allowance that
removing an Item does not give back.

So `POST /plaid/rewebhook` repoints every existing Item at the
server's current webhook URL (`client.update_item_webhook`), skipping
and reporting any that fail rather than stopping. The admin portal
should offer it as a button next to the webhook URL field, and the
setup guide should say plainly: **change the address, then press this.**

## Plan

### Where it stands, 2026-11-17

Done:

- Test-account password rotated; the credential is out of the repo.
- **Server picker** in the app — choose a backend, then sign in. No
  server is baked into the source any more.
- **Scheduled Plaid pull** — hourly by default, floored at two minutes.
  Not originally in this plan; added because a plain-HTTP install
  receives no webhooks at all, and it doubles as the backstop for a
  missed one.
- **Quiet hours configurable** and switchable off, defaulting to
  midnight–08:00.
- **`POST /plaid/rewebhook`** — moving the server no longer strands
  existing connections, so adding HTTPS later costs nothing.
- API published on **port 21821**.

Also done, 2026-11-18:

- **Accounts have roles**, and public registration is closed. Two ways
  in remain: the first account on an empty instance, which becomes the
  owner, and an owner creating one.
- **The admin API** — state, accounts, settings, health. Owner-gated,
  secrets never returned, bootstrap secrets refused.
- **Owner-editable settings**, stored encrypted, overriding `.env`.
- **A self-contained compose**, `deploy/docker-compose.selfhost.yml`,
  standing on nothing that was already there. The old one declared
  `networks: wg: external: true` — a Caddy network that exists on one
  Pi — so a fresh `docker compose up` failed before starting anything.
- **Migrations run on start**, in the entrypoint.
- **The settings portal**, served at `/admin`. Plain HTML, CSS and
  JavaScript — no build step, nothing to keep current, and it works
  before anything else on the instance is configured. First-run wizard,
  bank and AI settings, accounts, and an overview that says in English
  what is wrong. Verified in a browser against the clean instance.

The portal holds no rules of its own. Which settings exist, which
values are valid, who may change them — all answered by the API and
merely rendered. That is what makes the MCP work later a matter of
pointing something else at the same endpoints rather than
reimplementing the logic behind a second interface. `plaid_env` and
`ai_provider` became menus by having the API declare their choices,
not by listing them in JavaScript.

### Proven on a genuinely empty instance

`nnf-test` on port 21822: its own database, volumes and project,
sandbox Plaid, AI off, scheduled pull off. It exists because the one
question that matters — *does a fresh install work?* — cannot be asked
of a server with a hand-built database, a hand-edited `.env` and a
Caddy network that already exists.

It earned itself immediately. A clean `docker compose up` answered
**`Internal Server Error` on every endpoint**, because nothing ran the
migrations: the container started uvicorn and that was all. Nothing
hinted at why. Fixed in the entrypoint; a fresh install is now serving
in eighteen seconds with no manual step.

The first-run flow then checked out end to end, which could not have
been tested anywhere else:

```
empty instance → first signup      201  → role: owner
stranger signs up                  403
owner creates an account           201
```

### Phase 0 — progress, 2026-11-18

Done:

- **LICENSE** (MIT).
- **`deck` renamed out of the settings**: `AI_HOST_URL`,
  `POWER_RELAY_*`, `AI_PROVIDER=managed`, `ManagedOllamaProvider`. Old
  environment names still resolve via `AliasChoices`, because pydantic
  does not error on an unknown variable — it uses the default, so an
  un-updated `.env` would have left the relay silently dead.
- **`trusted_proxies`** no longer defaults to one particular Docker
  bridge address. Loopback only; set it to your proxy.
- **`.env.example` contains no personal data at all** — no LAN
  addresses, no hostnames, no `America/Denver`, and `AI_PROVIDER=none`
  since AI is optional.
- **`deploy/pi` and `deploy/deck` sanitised** so they can stay public
  as the reference implementation of the wake contract.

Sanitising the relay turned up a hazard worth recording. Removing the
hardcoded `DECK_HOST` made `host_is_up()` answer "not reachable" for
everything — which inverts the guard that stops `on` pressing the
button on a machine that is already running, straight into an ACPI
shutdown. The address is now required, and every command refuses
without it. The same change exposed that the CLI never read
`/etc/deck-power.env` at all: the systemd unit got it through
`EnvironmentFile=`, and the command line worked by luck on the
hardcoded default. It reads the file itself now, so there is one
source of configuration rather than two that drift.

### Phase 0 — make it safe to publish

- Rotate the test-account password; delete `docs/13-test-account.md`.
- Move to a private `nnf-homelab` repo: `deploy/pi/`, `deploy/deck/`,
  `docs/09-pi-operations.md`, `docs/16-deck-power.md`, the Caddyfile
  fragment. Referenced from the public repo as a worked example.
- Add `LICENSE` (MIT).
- De-personalise config defaults: `ai_deck_url`, `ai_quiet_timezone`
  (`America/Denver`), `trusted_proxies`.
- Editing pass over the docs: they are written in the first person
  about one person's budget.
- Rewrite `README.md` for someone who has never seen this.

### Phase 1 — make it usable by someone else

- ~~**Server picker in the app.**~~ **Done, 2026-11-17.** Choose the
  backend, then log in. `ApiClient.defaultBaseUrl` no longer names
  anyone's server: it reads `NNF_SERVER_URL` from the build and is
  empty unless given, so a household can prefill their own address with
  `--dart-define` without it being in the source. `baseUrlProvider` is
  now tri-state (null = still reading the preference, '' = none chosen,
  else chosen), which is what stops the picker flashing at someone who
  already has a server on every cold start. Switching servers clears
  the stored tokens: they are issued by one backend and mean nothing to
  another, and `hasSession()` only checks that a token exists locally —
  so without clearing, pointing at a second instance opens a budget
  screen that 401s on every request. An explicit `http://` is allowed
  and warned about rather than refused, because a LAN install with no
  certificate is a reasonable setup.
- **Admin portal**, as specified above.
- **First-run wizard**: creates the owner, takes the Plaid
  credentials, offers a sandbox account. Gated against the land grab.
- **Remove public registration.**
- **Per-account Plaid environment**, with the webhook fix above.
- **A choice of AI, with self-hosting as the encouraged path.** See
  below — this grew into its own design, not a one-line item.
- Rename `deck` in code and settings to something generic — it is the
  inference host, not a Steam Deck. `DeckProvider`, `ai_deck_url`,
  `deck_power_*`. User-facing strings already say "the AI computer".

### The AI choice: self-hosted first, managed allowed

Two paths, both first class, neither forced.

**Self-hosted** is the one the project is for. Your transaction
history never leaves your house, and a spare machine that is asleep
most of the day is enough — which is the whole point of the wake
machinery. It is the default and what the documentation leads with.

**A managed model** is for everyone without a spare GPU box, which is
most people. Paste an API key and the AI features work. Refusing to
support that does not make people self-host; it makes them use
something else, or get a budget with no categorisation at all.

#### One provider covers nearly everything

Most hosted APIs speak OpenAI's shape, so a single
`base_url` + `api_key` + `model` provider reaches OpenAI, **Gemini**
(Google ships an OpenAI-compatible endpoint), **Claude** via
OpenRouter, and local servers like vLLM, llama.cpp and LM Studio.
Anthropic's own API is not OpenAI-shaped; direct Claude support would
be a second provider, and is not needed first.

What actually has to be mapped is the four Ollama specifics this app
leans on:

| Used today | On a hosted model |
|---|---|
| `format` (JSON schema) | `response_format: json_schema`. The load-bearing one — every categorisation depends on it |
| `tools` | maps cleanly |
| `think` | no equivalent; dropped |
| `num_ctx` / `num_predict` | `num_ctx` is meaningless; `num_predict` becomes `max_tokens` |
| `prompt_eval_count` | `prompt_tokens`, so the Ask screen's context meter survives |

With a managed model the wake machinery, the GPU gate and quiet hours
all become meaningless. They should be **hidden, not silently
ignored**: a setting that is visible and has no effect is worse than
one that is absent.

#### Say plainly what the choice costs

At the point of choosing, not in a document nobody reads:

> **Self-hosted** — your transactions never leave your network.
> **A managed model** — merchant names, amounts and the goals you have
> written are sent to that company, and you pay per use.

Both are legitimate. Only one of them needs saying out loud, and a
finance app is the wrong place to be coy about it.

#### Bring your own wake

Self-hosting on standby hardware only works if the server can get that
hardware up on demand, and everyone's will be different — Wake-on-LAN,
a relay across the power switch (what this deployment uses), a smart
plug, a hypervisor API, a script on another machine.

So the app should not ship one mechanism. It should accept **a URL it
can call to wake the host**, and document the contract that URL must
honour. The deployment here already works this way; it is two
endpoints and a bearer token.

**And the contract should be served by the app itself**, as developer
documentation available over the API — because the person wiring up a
smart plug or a GPIO relay will very likely have an assistant helping
them, and that assistant should be able to fetch and read the exact
expected shape rather than guess from a README it cannot see. The same
instinct behind the MCP work: anything a person can learn from this
server, a machine helping them should be able to learn too.

Sketch of what gets documented and served:

```
POST {wake_url}          Authorization: Bearer {token}
  -> 200 once the host is reachable, or when the attempt has started
  -> 409 if it is already up
  -> 429 if asked again too soon

GET  {wake_url}/status   Authorization: Bearer {token}
  -> {"reachable": bool, "last_attempt_at": iso8601|null,
      "cooldown_seconds": int}
```

`deploy/pi/` is the working example of exactly this, and becomes the
reference implementation rather than a requirement.

### Phase 2 — done, 2026-11-18

- **`INSTALL.md`** — eight steps, no domain or certificate required.
  Verified by following it literally against a torn-down instance: 21
  seconds to start, `{"status":"ok"}`, the portal offering to claim
  it, 17 migrations applied without being asked.
- **`docs/plaid-setup.md`** — sandbox first, then Trial, never
  production, with the lifetime allowance stated before anything else
  and a table of what each action costs.
- **`docs/home-ai-server.md`** — one working AI host described as an
  example rather than a requirement, including why it runs Vulkan
  instead of ROCm, and the wake contract someone's own hardware would
  need to honour.
- **`docs/example-deployment.md`** — the shape of a real installation,
  including the two reverse-proxy traps: a prefix match on the webhook
  path, and the catch-all 404 that stops scanners at the edge.

### Phase 2 — the setup experience

- `INSTALL.md`: Docker Compose, Postgres, reverse proxy, HTTPS, the
  public webhook URL, generating keys.
- **`docs/plaid-setup.md`**, with the warning up front:
  1. Everything in **sandbox** first. It is free and unlimited.
  2. Only once the whole loop works — link, sync, webhook, categorise —
     apply for the **Trial** plan.
  3. **Do not apply for production.**
  4. Trial gives a small, *lifetime* number of connections. Every
     attempt against a real bank spends one, and removing an Item does
     **not** give it back. A typo in the Plaid dashboard costs a
     connection permanently.
- **`docs/example-deployment.md`**: a sanitised account of this
  deployment — Pi, Docker, Caddy, DuckDNS — so people can see one real
  shape rather than a hypothetical.
- **`docs/home-ai-server.md`**: how the inference host works here and
  what someone would need to build their own. Explicitly an example,
  not a requirement: AI is optional (ADR 0003) and the budget is fully
  usable without it.

### Phase 2.5 — migrations must run themselves

The container starts uvicorn and nothing else; `alembic upgrade head`
has always been run by hand. That is survivable for one instance whose
owner wrote the migrations, and a trap for everyone else: pull a new
image, start it, and the app runs against a schema it no longer
matches. Self-hosted software is expected to migrate on boot.

Needs doing before anyone else installs this, and before any schema
change in the phases above reaches a second machine.

### Phase 3 — an API surface worth connecting to

The point is that this is *your* spending data on *your* hardware, so
getting at it with your own tools should be the easy path, not a
reverse-engineering exercise.

- **A documented, stable HTTP API.** FastAPI already generates an
  OpenAPI schema; what is missing is treating it as a contract —
  versioning, stable field names, and not breaking it casually.
- **An MCP server**, so someone can point their own assistant at their
  budget and ask it things, or have it categorise and reconcile.
- **Admin and setup through the same surface.** Connecting a bank,
  creating an account, changing the sync interval — if an assistant can
  walk someone through setup, the hardest part of self-hosting gets
  much easier. This is the part that could matter most for adoption,
  and it only works if the admin portal is an API with a UI on top
  rather than a UI with logic inside it. **Worth keeping in mind while
  building the portal, even though it is built later.**

### Distribution: a published client

The app will go to Google Play and the App Store, so people download a
client and point it at their own server rather than building Flutter
themselves.

That is already the shape the app is built for — it has no server
baked in, asks on first run, and one build works against any instance
— but publishing adds obligations worth planning for rather than
discovering during review:

- **A privacy policy**, and an honest one. Both stores require it, and
  this app touches bank data. The honest version is short: the app
  stores nothing and sends nothing anywhere except the server the user
  chose; what that server does with it is the user's own decision,
  including whether they send anything to a hosted model.
- **Store review needs a working instance.** Reviewers will not stand
  up a server, so a demo instance with sandbox data and credentials in
  the review notes is the practical route.
- **Account deletion**, which Apple requires where accounts exist. The
  honest answer here is unusual and worth stating: accounts live on
  the user's own server, so the app can request deletion but the
  server owner holds the data.
- **A release build identity.** The current APK is debug-signed, which
  is fine for sideloading and not for a store. Signing keys must be
  generated and kept somewhere they cannot be lost; losing an Android
  upload key is not recoverable without Google's help.
- **The server picker must be forgiving**, because it is now the first
  screen a stranger sees. It already probes before saving and refuses
  to pretend a bad address is a bad password.

None of this blocks publishing the source.

### Phase 4 — widen the door, if wanted

- CSV / OFX import, for people who will never use an aggregator.
- A second aggregator for non-US users.
- **A browser version of the app itself.** Flutter already compiles to
  web, so the screens, models and API client that exist are most of
  it — far cheaper than a second frontend, and one codebase rather
  than two. For the people who would rather sit down at a desk to do
  their finances.

None of these is needed to publish.

## Risks

- **The Trial connection cap is a footgun.** Finite, lifetime, and not
  refunded by deleting an Item. Mitigated by documentation and by
  showing the count in the admin portal.
- **Open registration must be closed before anyone exposes an
  instance.** Today it is not.
- **Support burden.** Publishing invites questions about other people's
  hardware, banks and networks. Worth deciding in advance how much of
  that to take on.
- **Plaid's terms** should be read before telling strangers to sign up.

## What deliberately does not change

The budget engine, the append-only ledger, integer cents, the two
taxonomies, and the rule that the engine divides money rather than the
model. Those are the product. Everything above is packaging.
