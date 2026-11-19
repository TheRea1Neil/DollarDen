# Installing

You need a machine that runs Docker and about fifteen minutes. A
Raspberry Pi 4 is enough — that is what this was built on.

Nothing here requires a domain name, a certificate, or opening a port
to the internet. Those are worth having later and are covered at the
end.

## 1. Get the files

```bash
git clone <this repo> no-nonsense-finance
cd no-nonsense-finance/deploy
cp ../.env.example .env
```

## 2. Fill in three secrets

Open `.env`. Three values must be set before anything will start:

```bash
POSTGRES_PASSWORD=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
JWT_SIGNING_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
TOKEN_ENCRYPTION_KEY=$(python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")
```

> ### Back up `TOKEN_ENCRYPTION_KEY` now, before you connect a bank
>
> It encrypts your stored bank connections. Without it they cannot be
> read, and the only way back is to link every bank again — which
> spends your Plaid connections, which are finite and not refunded.
>
> Put it somewhere that survives this machine dying. A password
> manager is ideal. This is the one irreplaceable thing here: the
> database can be restored from a backup, and the key cannot be
> restored from anything.

Everything else in `.env` has a working default.

## 3. Start it

```bash
docker compose -f docker-compose.selfhost.yml up -d
```

It builds, starts Postgres, waits for it to be healthy, migrates its
own database and begins serving. A fresh install takes about twenty
seconds on a Pi.

Check it:

```bash
curl http://localhost:21821/api/health
```

If that answers, you are running.

## 4. Claim it

Open **`http://<your-server>:21821/admin/`** in a browser.

It will offer to create the owner account. That account owns the
server and is the only one that can change settings or create other
accounts — **nobody can sign themselves up**, which is why there is no
registration form anywhere in the app.

There are no default credentials, and there is no password reset. If
you lose the password, you set a new one from the machine itself:

```bash
docker compose -f docker-compose.selfhost.yml exec api \
    python -m app.password you@example.com
```

> Finish setup promptly if the server is reachable by anyone else. An
> instance with no accounts belongs to whoever opens it first.

## 5. Connect a bank

See **[docs/plaid-setup.md](docs/plaid-setup.md)**. Read it before you
sign up for anything: one of the steps spends a resource you cannot
get back, and the order matters.

The short version: do everything in Plaid's free sandbox first, then
apply for their **Trial** plan — not production.

## 6. Get the app

The phone app has no server built into it. On first run it asks for
your server's address, then signs you in.

Build it yourself:

```bash
cd app
flutter build apk --release          # Android
```

To save typing the address on every device, bake in a default:

```bash
flutter build apk --release --dart-define=NNF_SERVER_URL=https://budget.example.org
```

## 7. Optional: AI

Entirely optional. With it off, the budget works exactly as it does
otherwise and anything that would have been categorised automatically
waits in the review queue for you.

Set it up in the admin portal, under **AI**:

- **`managed`** — your own [Ollama](https://ollama.com) host, which
  the server may wake on demand. Your transactions never leave your
  network. See [docs/home-ai-server.md](docs/home-ai-server.md).
- **`custom`** — your own Ollama host that is always on.
- **`openai`** — a hosted service. Works with OpenAI, Gemini (through
  its OpenAI-compatible endpoint), anything on OpenRouter including
  Claude, and local servers like vLLM. **Merchant names, amounts and
  the goals you write are sent to that company, and you pay per use.**

## 8. Optional: reaching it from outside

Everything above works on your own network over plain HTTP. Two things
change if you want more.

**Transactions arrive faster.** Plaid will not send webhooks to an
unencrypted address, so without HTTPS the server pulls on a schedule
instead — hourly by default. Plaid only reads most banks a few times a
day, so hourly is close to what webhooks would have told you anyway.

**You can use the app away from home.**

The app does not terminate TLS, and should not: certificates belong to
a reverse proxy. Pick whichever suits you —

| | |
|---|---|
| **Caddy** | A domain pointing at your IP, and Caddy gets a certificate by itself. Two lines of config |
| **Cloudflare Tunnel** | No port forwarding, no static IP |
| **Tailscale** | No public exposure at all; reachable from your own devices |

Then set `PLAID_WEBHOOK_URL` in the admin portal to
`https://your-address/plaid/webhook` and press **re-point webhooks**,
which moves banks you already connected without re-linking them. The
environment is appended automatically.

Keep the admin portal off the public internet if you can. It is where
your Plaid credentials live, and reaching it over a tunnel or from
your own network costs nothing.

## Upgrading

```bash
git pull
docker compose -f docker-compose.selfhost.yml up -d --build
```

Migrations run automatically on start. Take a database backup first if
you have anything you would miss:

```bash
docker compose -f docker-compose.selfhost.yml exec db \
    pg_dump -U nnf nnf > backup-$(date +%F).sql
```

## When something is wrong

**Everything returns 500 on a fresh install.** The database did not
migrate. Check `docker compose logs api` for the alembic output.

**The app says it cannot reach the server.** Check the address in the
app. The server picker tests the address before saving it, so a
mistyped one says so rather than looking like a wrong password.

**Transactions are not arriving.** Open the admin portal's overview.
It shows when the bank last reached you and whether the scheduled pull
is on. No webhook address means pulls only.

**The AI is not answering.** The overview says whether it is
reachable. Nothing else is affected when it is not — the budget keeps
working and transactions wait for you.
