# One real deployment

A description of the setup this was built on, written so you can see
the shape of a working installation rather than infer it. Nothing here
is required; it is one set of choices among several that would work.

## What runs where

| | |
|---|---|
| **The server** | Raspberry Pi 4, always on. API, Postgres, Plaid sync, reverse proxy |
| **The AI host** | A desktop with a graphics card, asleep by default, woken on demand |
| **The edge** | Caddy on the Pi, HTTPS on a free DuckDNS subdomain |
| **The clients** | The Flutter app on two phones |

The Pi does the work that must never stop: receiving webhooks, syncing
transactions, serving the app. It draws a couple of watts. The
desktop does the work that only happens occasionally and is asleep the
rest of the time. See [home-ai-server.md](home-ai-server.md).

## Why a subdomain rather than a path

The Pi already ran other things on its public hostname, one of which
owned `/api/*`. Rather than rename this application's API, it got its
own hostname — `budget.example.org` alongside `example.org` — and
Caddy routes by `Host` header on the same already-forwarded port 443.

No new port forwarding, no DNS records to create: DuckDNS wildcards
every subdomain to the same address.

## The reverse proxy

Two routes matter, and the second one has a trap in it.

```
budget.example.org {
    tls { dns duckdns {$DUCKDNS_TOKEN} }

    handle /plaid/webhook* {
        rewrite * /api{uri}
        reverse_proxy dollarden-api:8081 { header_up X-Forwarded-Proto https }
    }

    handle /api/* {
        reverse_proxy dollarden-api:8081 { header_up X-Forwarded-Proto https }
    }

    handle { respond "Not Found" 404 }
}
```

**The webhook rule is a prefix match**, not an exact one. Accounts
belong to a Plaid environment and the webhook path names it —
`/plaid/webhook/production`, `/plaid/webhook/sandbox` — because the
receiver has to know which environment sent a request before it can
verify the signature. An exact match 404s all of those. That failure
is invisible from Plaid's side: the connection stays healthy, the
dashboard still shows a webhook registered, and transactions simply
stop arriving in real time. It showed up here only as a run of 404s in
the proxy's access log.

**The catch-all 404 is not decoration.** Certificate Transparency logs
are public, and scanners were probing this hostname for `/wp-json/`
and `/xmlrpc.php` within five seconds of the certificate being
issued. Over a month, around two hundred such requests were stopped at
the edge rather than becoming backend round trips.

**The admin portal is deliberately not proxied.** It is where Plaid
credentials live. It is reachable from the home network and over a
WireGuard tunnel, and returns 404 from the internet.

## Backups

A nightly `pg_dump` on a systemd timer, kept on the Pi, plus the
encryption key in a password manager. The second matters more than the
first: a database without the key cannot be read, and re-linking banks
spends connections that are never returned.

## Scale, for context

After a month of real use:

| | |
|---|---|
| Transactions | ~330 |
| Ledger entries | ~170 |
| Categories | ~20 |
| Database size | A few megabytes |
| Backend | ~15,000 lines of Python |

This is a small amount of data being looked after carefully. The Pi is
never the bottleneck; the only slow thing in the system is the model,
and that is asleep most of the time.
