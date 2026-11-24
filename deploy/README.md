# deploy

## Which compose file

**`docker-compose.selfhost.yml`** is the one to use. It stands on
nothing that was already there: its own database, its own volumes, its
own network, published on a port you choose. See `../INSTALL.md`.

```bash
cp ../.env.example .env     # then fill in the three secrets
docker compose -f docker-compose.selfhost.yml up -d
```

There used to be a `docker-compose.yml` here as well. It attached to a
Caddy network that exists on exactly one Raspberry Pi, so a fresh
`docker compose up` failed before starting anything — it worked there
and nowhere else, which is invisible until somebody else tries it. It
now lives with the rest of that deployment's particulars, in a private
repository, and `docker-compose.selfhost.yml` is what the project
ships.

## What else is here

| | |
|---|---|
| `Caddyfile.fragment` | An example reverse-proxy block, including the two traps: a prefix match on the webhook path, and a catch-all 404 |
| `backup.sh`, `restore.sh`, `dollarden-backup.service` | Nightly `pg_dump` and the restore that goes with it |
| `go-production.sh` | Moving an instance from Plaid sandbox to real banking |
| `pi/` | A reference implementation of the wake contract — a relay across a power switch, driven by a GPIO pin. An example, not a requirement: see `GET /api/docs/wake-contract` |
| `deck/` | A reference GPU probe, so AI work defers while a game is running |
