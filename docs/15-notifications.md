# 15 — Notifications (planned, not built)

Deferred deliberately on 2026-11-05. Written down because the reasoning
is easy to lose and the wrong approach is tempting.

## What we want notified

Only the first is about a paycheck. The rest are why this needs a
server, and why the quick local-only route is a dead end.

- A paycheck has arrived and is waiting to be reviewed.
- An envelope has been drawn down past a threshold the user set
  ("tell me when Groceries drops under $50").
- An envelope is overspent.
- A bank connection has broken and needs re-authenticating
  (`ITEM_LOGIN_REQUIRED`) — silent today, and everything goes stale
  until someone notices.
- A recurring charge is due, or landed at a different amount.

## Why local notifications are not enough

Neil has shipped Android apps with notifications and no Firebase, which
is true and worth being precise about: **a local notification needs the
app to be running code at the moment it fires.** Scheduled reminders
work because Android's AlarmManager wakes the app at a time the app
itself chose — the clock is the trigger, and Android owns the clock.

Every event above is triggered by something that happens on the
server. The phone has no way to know a paycheck landed. So the options
are:

| | How | Latency | Cost |
|---|---|---|---|
| **WorkManager poll** | app wakes every ~15 min, asks the API, raises a local notification | 15–60 min (Doze stretches it) | none |
| **FCM push** | server pushes, phone wakes immediately | seconds | a Firebase project |

A 15-minute floor is fine for "you got paid". It is poor for
"Groceries just went negative", which is only useful at the moment of
the purchase.

## Decision

**Do FCM.** The threshold alerts are the ones that make the feature
worth having, and they need to be prompt. Polling would have to be
built and then thrown away.

Deferred for now, not rejected.

## What it will take

- A Firebase project, and `google-services.json` in `app/android/app/`.
  Needs Neil in the Firebase console; nothing else can be done first.
- A `device_tokens` table: user, token, platform, last seen. Tokens
  rotate, so registering on every launch and pruning on send failure
  is required, not optional.
- A send path on the Pi (`firebase-admin`, service-account JSON kept
  out of the repo like `TOKEN_ENCRYPTION_KEY`).
- Trigger points. The threshold ones belong where the ledger is
  written, not on a timer: `post()` in `app/budget/ledger.py` already
  recomputes category balances and is the one place that knows a
  balance crossed a line.
- A per-category threshold column already exists (`alert_at_cents`),
  so the data model is mostly there.

## iOS

Needs an APNs key from Neil's Apple developer account. Android first —
it is the real target device.
