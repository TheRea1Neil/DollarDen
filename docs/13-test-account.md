# Test account

A throwaway account on the live API for UI and end-to-end testing, so
Neil's real budget is never used as a scratchpad.

    email     nnf-uitest@example.com
    password  ui-test-UbS6Lcrbh4ncNarjb66kvcIu

Created 2026-10-31.

**Reuse it** rather than registering a new one each time. Registration
is rate limited, and a pile of half-populated accounts makes the
real-data checks (docs/09) harder to read — several already exist from
earlier sessions and they are noise.

## Why this is in the repo

It is a test login for a single-user personal app, with no real money
and no real bank connection behind it. The repo is private.

The thing to keep straight is the separation it buys: `nnf-uitest@example.com`
is for poking at the UI and may be wiped at any time, while
**`neil@nonsensefinance.org`** holds the real budget, is the account
signed in on the Android phone, and must never be used for destructive
tests.

Real secrets — the Plaid keys, the JWT signing key, the token
encryption key, the Wake-on-LAN and GPU probe tokens — stay in
`.env`, which is gitignored. Nothing here changes that.
